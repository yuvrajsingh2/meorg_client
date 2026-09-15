"""Methods for downloading experiment dataset files."""

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

import requests
from tqdm import tqdm

import meorg_client.constants as mcc
import meorg_client.exceptions as mx
from meorg_client.exceptions import RequestException


class Progress:
    """An aggregate byte-level progress bar shared by the download threads.

    Bytes are recorded per file so that a restarted transfer can withdraw the
    bytes it had already reported, which keeps the total honest when the object
    store ignores a Range request.
    """

    def __init__(self, total_bytes: int, total_files: int, enabled: bool = True):
        """Create the progress bar.

        Parameters
        ----------
        total_bytes : int
            Total size of every file in the manifest.
        total_files : int
            Number of files in the manifest.
        enabled : bool, optional
            Display the bar, by default True.
        """
        self._lock = threading.Lock()
        self._recorded = dict()
        self._files_done = 0
        self._total_files = total_files
        self._bar = tqdm(
            total=total_bytes,
            disable=not enabled,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
        )
        self._update_postfix()

    def set_bytes(self, file_id: str, value: int):
        """Record the absolute number of bytes held for one file.

        Parameters
        ----------
        file_id : str
            Manifest file ID.
        value : int
            Bytes currently on disk for that file.
        """
        with self._lock:
            delta = value - self._recorded.get(file_id, 0)
            self._recorded[file_id] = value
            if delta:
                self._bar.update(delta)

    def add_bytes(self, file_id: str, delta: int):
        """Add newly transferred bytes for one file.

        Parameters
        ----------
        file_id : str
            Manifest file ID.
        delta : int
            Bytes transferred since the last call.
        """
        with self._lock:
            self._recorded[file_id] = self._recorded.get(file_id, 0) + delta
            self._bar.update(delta)

    def file_complete(self):
        """Record that one more file has finished."""
        with self._lock:
            self._files_done += 1
            self._update_postfix()

    def _update_postfix(self):
        """Show the file count alongside the byte totals."""
        self._bar.set_postfix_str(
            f"{self._files_done}/{self._total_files} files", refresh=False
        )

    def close(self):
        """Close the underlying bar."""
        self._bar.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def download_experiment_datasets(
    client,
    experiment_id: str,
    output_dir: Union[str, Path],
    n: int = 4,
    progress: bool = True,
    resume: bool = True,
) -> dict:
    """Download all dataset files for an experiment.

    Files are written below ``output_dir`` using each manifest ``relativePath``.
    Partial transfers use a ``.part`` suffix and resume with an HTTP Range request.

    Parameters
    ----------
    client : meorg_client.client.Client
        Authenticated client used to request the manifest.
    experiment_id : str
        Experiment instance ID.
    output_dir : path-like
        Directory that receives the manifest files.
    n : int, optional
        Number of parallel download threads, by default 4.
    progress : bool, optional
        Show a progress bar, by default True.
    resume : bool, optional
        Resume partial files, by default True.

    Returns
    -------
    dict
        Download summary with downloaded and skipped file paths.
    """
    if n < 1:
        raise ValueError("Number of threads must be greater than or equal to 1.")

    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    manifest = _get_manifest(client, experiment_id)
    entries = prepare_manifest(manifest, root)

    with Progress(
        total_bytes=sum(entry["size"] for entry in entries),
        total_files=len(entries),
        enabled=progress,
    ) as bar:
        results = _download_batch(entries, n, resume, bar)

        expired_entries = [
            entry
            for entry, result in zip(entries, results)
            if isinstance(result, mx.ManifestExpiredException)
        ]
        other_errors = [
            result
            for result in results
            if isinstance(result, Exception)
            and not isinstance(result, mx.ManifestExpiredException)
        ]
        if other_errors:
            raise other_errors[0]

        # A rejected URL only means the signature aged out. Ask for fresh URLs
        # and retry those files against whatever the new manifest now says.
        if expired_entries:
            manifest = _get_manifest(client, experiment_id)
            refreshed_by_id = {
                entry["fileId"]: entry for entry in prepare_manifest(manifest, root)
            }

            retry_entries = []
            for entry in expired_entries:
                refreshed = refreshed_by_id.get(entry["fileId"])
                if refreshed is None:
                    raise mx.DownloadException(
                        f"The refreshed manifest does not contain file {entry['fileId']}."
                    )
                retry_entries.append(refreshed)

            retry_results = _download_batch(retry_entries, n, resume, bar)
            retry_errors = [
                result for result in retry_results if isinstance(result, Exception)
            ]
            if retry_errors:
                if isinstance(retry_errors[0], mx.ManifestExpiredException):
                    raise mx.ManifestExpiredException(
                        "The manifest expired again after it was refreshed."
                    ) from retry_errors[0]
                raise retry_errors[0]

            successful_by_id = {
                entry["fileId"]: result
                for entry, result in zip(retry_entries, retry_results)
            }
            results = [
                successful_by_id.get(entry["fileId"], result)
                for entry, result in zip(entries, results)
            ]

    return {
        "experimentId": manifest.get("experimentId"),
        "outputDir": str(root),
        "fileCount": len(results),
        "totalBytes": sum(result["size"] for result in results),
        "downloaded": [result["path"] for result in results if not result["skipped"]],
        "skipped": [result["path"] for result in results if result["skipped"]],
    }


def _get_manifest(client, experiment_id: str) -> dict:
    """Get a manifest, asking once more when it arrives already expired.

    Parameters
    ----------
    client : meorg_client.client.Client
        Authenticated client.
    experiment_id : str
        Experiment instance ID.

    Returns
    -------
    dict
        A manifest whose URLs have not yet expired.
    """
    manifest = client.get_experiment_dataset_manifest(experiment_id)
    if not _has_expired(manifest):
        return manifest

    manifest = client.get_experiment_dataset_manifest(experiment_id)
    if _has_expired(manifest):
        raise mx.ManifestExpiredException(
            "The manifest is already expired after one refresh."
        )

    return manifest


def _has_expired(manifest: dict) -> bool:
    """Report whether a manifest's declared expiry time has passed."""
    if not isinstance(manifest, dict):
        raise mx.DownloadException("The server returned an invalid manifest.")

    expires_at = manifest.get("urlsExpireAt")
    if not isinstance(expires_at, str):
        raise mx.DownloadException(
            "The manifest does not contain a valid urlsExpireAt value."
        )

    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError as ex:
        raise mx.DownloadException(
            "The manifest contains an invalid urlsExpireAt value."
        ) from ex

    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    return expiry <= datetime.now(timezone.utc)


def prepare_manifest(manifest: dict, root: Path) -> list:
    """Validate manifest entries and resolve their local output paths.

    Only the fields the downloader consumes are checked. Anything else the
    server sends is carried through untouched, so an additive change to the
    manifest does not break the client.

    Parameters
    ----------
    manifest : dict
        Manifest returned by the server.
    root : Path
        Resolved output directory.

    Returns
    -------
    list
        Manifest entries with a resolved ``target`` path attached.
    """
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise mx.DownloadException("The server returned an invalid manifest.")

    entries = []
    seen_file_ids = set()
    seen_paths = set()
    for file_info in manifest["files"]:
        if not isinstance(file_info, dict) or not all(
            isinstance(file_info.get(field), str) and file_info[field]
            for field in ("fileId", "relativePath", "url")
        ):
            raise mx.DownloadException(
                "The manifest contains an incomplete file entry."
            )
        if (
            not isinstance(file_info.get("size"), int)
            or isinstance(file_info["size"], bool)
            or file_info["size"] < 0
        ):
            raise mx.DownloadException(
                f"The manifest contains an invalid size for file {file_info['fileId']}."
            )
        if file_info["fileId"] in seen_file_ids:
            raise mx.DownloadException(
                f"The manifest contains duplicate file {file_info['fileId']}."
            )
        seen_file_ids.add(file_info["fileId"])

        relative_path = Path(file_info["relativePath"])
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise mx.DownloadException(
                f"The manifest contains an unsafe path for file {file_info['fileId']}."
            )

        target = (root / relative_path).resolve()
        try:
            target.relative_to(root)
        except ValueError as ex:
            raise mx.DownloadException(
                f"The manifest contains an unsafe path for file {file_info['fileId']}."
            ) from ex

        if target in seen_paths:
            raise mx.DownloadException(
                f"The manifest contains duplicate path {file_info['relativePath']}."
            )
        seen_paths.add(target)
        entries.append({**file_info, "target": target})

    return entries


def _part_token(file_id: str) -> str:
    """Reduce a manifest file ID to characters that are safe in a filename.

    The partial file is keyed on the file ID so that a ``.part`` left over from
    a different version of the same dataset is never resumed into. Reusing one
    would append new bytes onto stale bytes, and the manifest carries no
    checksum that would catch it.

    Parameters
    ----------
    file_id : str
        Manifest file ID.

    Returns
    -------
    str
        Filename-safe token.
    """
    token = "".join(
        character for character in file_id if character.isalnum() or character in "-_"
    )
    return token or "part"


def _download_batch(entries: list, n: int, resume: bool, bar: Progress) -> list:
    """Download a batch and keep each result aligned with its manifest entry."""
    results = [None] * len(entries)
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = {
            pool.submit(_download_file, entry, resume, bar): index
            for index, entry in enumerate(entries)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as ex:
                results[index] = ex
    return results


def _download_file(file_info: dict, resume: bool, bar: Progress) -> dict:
    """Download one manifest file, with one retry for an interrupted transfer."""
    target = file_info["target"]
    target.parent.mkdir(parents=True, exist_ok=True)
    expected_size = file_info["size"]
    file_id = file_info["fileId"]

    if target.is_file() and target.stat().st_size == expected_size:
        bar.set_bytes(file_id, expected_size)
        bar.file_complete()
        return {"path": str(target), "size": expected_size, "skipped": True}

    partial = target.with_name(f"{target.name}.{_part_token(file_id)}.part")
    if not resume and partial.exists():
        partial.unlink()
    if partial.exists() and partial.stat().st_size > expected_size:
        partial.unlink()

    last_error = None
    for _ in range(2):
        if not resume and partial.exists():
            partial.unlink()
        try:
            _transfer_file(file_info, partial, bar)
            last_error = None
            break
        except mx.ManifestExpiredException:
            raise
        except (
            requests.exceptions.RequestException,
            RequestException,
            mx.DownloadException,
        ) as ex:
            last_error = ex

    if last_error is not None:
        if isinstance(
            last_error,
            (requests.exceptions.RequestException, RequestException),
        ):
            raise mx.DownloadException(
                f"The transfer failed twice for {file_info['relativePath']}."
            ) from last_error
        raise last_error

    actual_size = partial.stat().st_size
    if actual_size != expected_size:
        raise mx.DownloadException(
            f"Downloaded file {file_info['relativePath']} has size {actual_size}; expected {expected_size}."
        )

    partial.replace(target)
    bar.set_bytes(file_id, expected_size)
    bar.file_complete()
    return {"path": str(target), "size": expected_size, "skipped": False}


def _transfer_file(file_info: dict, partial: Path, bar: Progress):
    """Transfer one signed URL into its partial file."""
    file_id = file_info["fileId"]
    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    response = requests.get(
        file_info["url"],
        headers=headers,
        stream=True,
        timeout=mcc.DOWNLOAD_TIMEOUT,
    )

    if response.status_code in (401, 403):
        response.close()
        raise mx.ManifestExpiredException(
            "A download URL expired. The client will request a new manifest."
        )

    if response.status_code == 416 and offset == file_info["size"]:
        response.close()
        bar.set_bytes(file_id, offset)
        return

    if response.status_code not in mcc.HTTP_STATUS_SUCCESS_RANGE:
        status_code = response.status_code
        response_text = response.text
        response.close()
        raise RequestException(status_code, response_text)

    mode = "ab" if offset and response.status_code == 206 else "wb"
    if mode == "ab":
        content_range = response.headers.get("Content-Range", "")
        if not content_range.startswith(f"bytes {offset}-"):
            response.close()
            raise mx.DownloadException(
                f"The range response for {file_info['relativePath']} did not start at byte {offset}."
            )

    # Report the bytes this attempt starts from, withdrawing anything a previous
    # attempt reported when the object store ignored the Range request.
    bar.set_bytes(file_id, offset if mode == "ab" else 0)

    try:
        with open(partial, mode) as output:
            for chunk in response.iter_content(mcc.DOWNLOAD_CHUNK_SIZE):
                if chunk:
                    output.write(chunk)
                    bar.add_bytes(file_id, len(chunk))
    finally:
        response.close()

    actual_size = partial.stat().st_size
    if actual_size > file_info["size"]:
        partial.unlink()
        bar.set_bytes(file_id, 0)
        raise mx.DownloadException(
            f"Downloaded file {file_info['relativePath']} is larger than the manifest size."
        )
    if actual_size < file_info["size"]:
        raise mx.DownloadException(
            f"Downloaded file {file_info['relativePath']} ended before all bytes arrived."
        )
