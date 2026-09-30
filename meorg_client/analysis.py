"""Analyses that run outside the ME.org worker.

`prepare_analysis_input` turns the ME.org input list of a model output and
experiment into an ``input.json`` with local paths. Input files are cached by
object key. `submit_analysis_result` posts the run directory that ``meorg-run``
wrote back to ME.org.

Signed URLs are bearer credentials. They are never written to disk and never
put in an error message.
"""

import json
import mimetypes as mt
import os
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional, Union

import requests

import meorg_client.constants as mcc
import meorg_client.downloads as med
import meorg_client.exceptions as mx
from meorg_client import __version__
from meorg_client.exceptions import RequestException

CLIENT_NAME = "meorg_client"
INPUT_MANIFEST = "input.json"
OUTPUT_MANIFEST = "output.json"
RUN_RECORD = "run.json"
PALS_LOG = "output/PALS.log"
STDERR_LOG = "r-stderr.log"
CACHE_TMP_DIR = ".tmp"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _server_message(response_text: str) -> str:
    """Get a short message out of an ME.org error body."""
    try:
        body = json.loads(response_text)
    except (TypeError, ValueError):
        return (response_text or "").strip()[:500]

    if not isinstance(body, dict):
        return str(body)[:500]
    if isinstance(body.get("message"), str):
        return body["message"]
    data = body.get("data")
    if isinstance(data, dict):
        return "; ".join(str(value) for value in data.values())
    return json.dumps(body)[:500]


def describe_request_error(ex: RequestException, route: str) -> str:
    """Turn a failed ME.org request into a message for the user.

    Parameters
    ----------
    ex : RequestException
        The failed request.
    route : str
        Route name for the message, such as ``analysis-input``.

    Returns
    -------
    str
        Message that is safe to print.
    """
    message = _server_message(ex.response_text)
    code = ex.status_code
    if code == 400:
        return f"{route}: the server rejected the request (400): {message}"
    if code == 401:
        return f"{route}: not authenticated (401). Check your ME.org credentials."
    if code == 404:
        return f"{route}: model output not found or not accessible (404)."
    if code == 409 and route == "analysis-input":
        return (
            f"{route}: an input file is not in the object store yet, so its upload "
            f"has not finished (409). Try again later. Server: {message}"
        )
    if code == 409:
        return f"{route}: conflict (409): {message}"
    return f"{route}: request failed ({code}): {message}"


# ---------------------------------------------------------------------------
# analysis input
# ---------------------------------------------------------------------------


def cache_relative_path(key) -> PurePosixPath:
    """Check an object key and return it as a path relative to a cache root.

    Parameters
    ----------
    key : str
        Object key, such as ``pals/data/AbCdEf123``.

    Returns
    -------
    PurePosixPath
        Relative path.

    Raises
    ------
    AnalysisException
        When the key is empty, absolute, or leaves the cache root.
    """
    if not isinstance(key, str) or not key.strip():
        raise mx.AnalysisException("An input file has no object key.")

    relative = PurePosixPath(key)
    if (
        relative.is_absolute()
        or "\\" in key
        or ".." in relative.parts
        or not relative.parts
        or relative.parts[0] == CACHE_TMP_DIR
    ):
        raise mx.AnalysisException(f"The server sent an unsafe object key: {key}")
    return relative


def find_cached(relative: PurePosixPath, size: int, roots: Iterable[Path]):
    """Find a cached copy of an object.

    A hit is an existing file with the expected size, in the first root that
    has one.

    Parameters
    ----------
    relative : PurePosixPath
        Object key as a relative path.
    size : int
        Expected size in bytes.
    roots : iterable of Path
        Cache roots, in search order.

    Returns
    -------
    Path or None
        Path of the cached file, or None on a miss.
    """
    for root in roots:
        candidate = Path(root) / relative
        try:
            if candidate.is_file() and candidate.stat().st_size == size:
                return candidate
        except OSError:
            continue
    return None


def _root(path) -> Path:
    """Absolute cache root, without resolving symbolic links."""
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _is_trigger(entry: dict) -> bool:
    """True for the model output 1 entries (the model output being analysed)."""
    return entry.get("type") == "ModelOutput" and entry.get("number") == 1


def _valid_size(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_entry(entry) -> dict:
    """Check the fields of one server entry that the client uses."""
    if not isinstance(entry, dict):
        raise mx.AnalysisException("The server sent an invalid input file entry.")
    filename = entry.get("filename")
    if not isinstance(filename, str) or not filename:
        raise mx.AnalysisException("The server sent an input file with no filename.")
    if not _valid_size(entry.get("size")):
        raise mx.AnalysisException(f"The server sent an invalid size for {filename}.")
    cache_relative_path(entry.get("key"))
    return entry


def _local_model_output_entries(model_output: dict, paths: Iterable) -> list:
    """Build the model output 1 entries from local files."""
    entries = []
    seen = set()
    for raw in paths:
        path = Path(os.path.abspath(os.path.expanduser(str(raw))))
        if not path.is_file():
            raise mx.AnalysisException(f"Model output file not found: {raw}")
        if path.name in seen:
            # ME.org matches uploaded files to the result by basename.
            raise mx.AnalysisException(
                f"Two model output files have the same name: {path.name}"
            )
        seen.add(path.name)
        entries.append(
            {
                "type": "ModelOutput",
                "number": 1,
                "name": model_output.get("name"),
                "setId": model_output.get("id"),
                "setModified": model_output.get("modified"),
                "modelName": model_output.get("modelName"),
                "filename": path.name,
                "size": path.stat().st_size,
                "path": str(path),
            }
        )
    return entries


def _insert_model_output(files: list, local_entries: list) -> list:
    """Put the local model output entries first among the ModelOutput entries."""
    index = next(
        (i for i, entry in enumerate(files) if entry.get("type") == "ModelOutput"),
        len(files),
    )
    return files[:index] + local_entries + files[index:]


def _get_input_data(client, model_output_id: str, experiment_id: str) -> dict:
    """Request the input list and check its shape."""
    try:
        response = client.get_analysis_input(model_output_id, experiment_id)
    except RequestException as ex:
        raise mx.AnalysisException(describe_request_error(ex, "analysis-input")) from None

    data = response.get("data") if isinstance(response, dict) else None
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("files"), list)
        or not isinstance(data.get("modelOutput"), dict)
    ):
        raise mx.AnalysisException("analysis-input: the server sent an invalid response.")
    return data


def _download_plan(missing: dict, cache_root: Path) -> list:
    """File entries for the shared download code, one per missing object key."""
    tmp_dir = cache_root / CACHE_TMP_DIR
    tmp_dir.mkdir(parents=True, exist_ok=True)
    plan = []
    for key, entry in missing.items():
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            raise mx.AnalysisException(
                f"The server sent no download URL for {entry['filename']}."
            )
        plan.append(
            {
                "fileId": key,
                # Used in messages, so it must not be the URL.
                "relativePath": entry["filename"],
                "size": entry["size"],
                "url": url,
                "target": cache_root / cache_relative_path(key),
                # A random name keeps two processes that fill the same key apart.
                "partial": tmp_dir / uuid.uuid4().hex,
            }
        )
    return plan


def _fill_cache(
    client,
    model_output_id: str,
    experiment_id: str,
    missing: dict,
    cache_root: Path,
    n: int,
    progress: bool,
) -> dict:
    """Download every missing object into the writable cache.

    Returns
    -------
    dict
        Object key to cached path.
    """
    if not missing:
        return {}

    plan = _download_plan(missing, cache_root)
    partials = [entry["partial"] for entry in plan]
    try:
        with med.Progress(
            total_bytes=sum(entry["size"] for entry in plan),
            total_files=len(plan),
            enabled=progress,
        ) as bar:
            results = med._download_batch(plan, n, True, bar)
            expired = [
                entry
                for entry, result in zip(plan, results)
                if isinstance(result, mx.ManifestExpiredException)
            ]
            errors = [
                result
                for result in results
                if isinstance(result, Exception)
                and not isinstance(result, mx.ManifestExpiredException)
            ]
            if errors:
                raise errors[0]

            # A rejected URL means the signature aged out. Ask once for new URLs.
            if expired:
                data = _get_input_data(client, model_output_id, experiment_id)
                fresh = {
                    entry.get("key"): entry
                    for entry in data["files"]
                    if isinstance(entry, dict)
                }
                retry = {}
                for entry in expired:
                    refreshed = fresh.get(entry["fileId"])
                    if refreshed is None or refreshed.get("size") != entry["size"]:
                        raise mx.AnalysisException(
                            f"The input list changed while {entry['relativePath']} "
                            "was downloading. Run the command again."
                        )
                    retry[entry["fileId"]] = refreshed
                retry_plan = _download_plan(retry, cache_root)
                partials += [entry["partial"] for entry in retry_plan]
                retry_results = med._download_batch(retry_plan, n, True, bar)
                retry_errors = [r for r in retry_results if isinstance(r, Exception)]
                if retry_errors:
                    raise retry_errors[0]
    finally:
        # Only a failure leaves a partial file. Successful ones were renamed.
        for partial in partials:
            try:
                Path(partial).unlink()
            except OSError:
                pass

    return {entry["fileId"]: entry["target"] for entry in plan}


def _lookup_cache(entries: list, roots: list):
    """Look up each object key once in the cache roots.

    Benchmark files appear again as ModelOutput entries with the same key, so
    there is at most one lookup and one download per key.

    Returns
    -------
    tuple
        ``hits`` (key to cached path) and ``missing`` (key to server entry).
    """
    sizes = {}
    for entry in entries:
        if sizes.setdefault(entry["key"], entry["size"]) != entry["size"]:
            raise mx.AnalysisException(
                f"The server sent two sizes for the object of {entry['filename']}."
            )

    hits = {}
    missing = {}
    for entry in entries:
        key = entry["key"]
        if key in hits or key in missing:
            continue
        found = find_cached(cache_relative_path(key), entry["size"], roots)
        if found is None:
            missing[key] = entry
        else:
            hits[key] = found
    return hits, missing


def prepare_analysis_input(
    client,
    model_output_id: str,
    experiment_id: str,
    run_id: str,
    cache: Union[str, Path],
    cache_ro: Iterable = (),
    model_output_files: Iterable = (),
    n: int = 4,
    progress: bool = False,
) -> dict:
    """Build the input.json document for an external analysis.

    Parameters
    ----------
    client : meorg_client.client.Client
        Authenticated client.
    model_output_id : str
        Model output ID.
    experiment_id : str
        Experiment ID.
    run_id : str
        External run ID, written as ``_id``.
    cache : path-like
        Writable cache root.
    cache_ro : iterable of path-like, optional
        Read-only cache roots, searched first and never written.
    model_output_files : iterable of path-like, optional
        Local files for model output 1. They replace the server's entries.
    n : int, optional
        Number of parallel downloads, by default 4.
    progress : bool, optional
        Show a progress bar, by default False.

    Returns
    -------
    dict
        ``input`` (the document), ``downloaded`` and ``cached`` (object key counts).
    """
    if n < 1:
        raise ValueError("Number of threads must be greater than or equal to 1.")
    if not isinstance(run_id, str) or not run_id.strip():
        raise mx.AnalysisException("A run ID is required.")

    cache_root = _root(cache)
    ro_roots = [root for root in (_root(r) for r in cache_ro) if root != cache_root]
    model_output_files = list(model_output_files or ())

    data = _get_input_data(client, model_output_id, experiment_id)
    local_entries = (
        _local_model_output_entries(data["modelOutput"], model_output_files)
        if model_output_files
        else []
    )

    server_entries = [_check_entry(entry) for entry in data["files"]]
    if local_entries:
        server_entries = [entry for entry in server_entries if not _is_trigger(entry)]
    elif not any(_is_trigger(entry) for entry in server_entries):
        raise mx.AnalysisException(
            "No model output files: the model output has no files on ME.org. "
            "Pass the local files with --model-output-files."
        )

    # One lookup and at most one download per object key. Benchmark files
    # appear again as ModelOutput entries with the same key.
    hits, missing = _lookup_cache(server_entries, ro_roots + [cache_root])
    downloaded = _fill_cache(
        client, model_output_id, experiment_id, missing, cache_root, n, progress
    )
    paths = {**hits, **downloaded}

    files = []
    for entry in server_entries:
        item = {field: value for field, value in entry.items() if field != "url"}
        item["path"] = str(paths[entry["key"]])
        files.append(item)
    files = _insert_model_output(files, local_entries)

    # Exit 0 only if every file is present.
    for item in files:
        path = Path(item["path"])
        if not path.is_file() or path.stat().st_size != item["size"]:
            raise mx.AnalysisException(f"Input file is missing: {item['filename']}")

    document = {
        "_id": run_id,
        "provenance": {
            "client": CLIENT_NAME,
            "meorgClientVersion": __version__,
            "modelOutputId": model_output_id,
            "experimentId": experiment_id,
        },
        "config": data.get("config"),
        "files": files,
    }
    return {"input": document, "downloaded": len(downloaded), "cached": len(hits)}


def write_input_json(document: dict, output: Union[str, Path]) -> Path:
    """Write input.json atomically.

    Parameters
    ----------
    document : dict
        The input document.
    output : path-like
        Target path.

    Returns
    -------
    Path
        Absolute path of the written file.
    """
    output = Path(os.path.abspath(os.path.expanduser(str(output))))
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, output)
    finally:
        if tmp.exists():
            tmp.unlink()
    return output


# ---------------------------------------------------------------------------
# analysis submit-result
# ---------------------------------------------------------------------------


def _read_json(path: Path, label: str):
    try:
        with open(path) as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise mx.AnalysisException(f"{label} not found: {path}") from None
    except (OSError, ValueError) as ex:
        raise mx.AnalysisException(f"{label} cannot be read: {ex}") from None


def _success_files(run_dir: Path) -> list:
    """Parts of a successful run: output.json, the files it lists, the log, run.json."""
    output_path = run_dir / OUTPUT_MANIFEST
    output = _read_json(output_path, OUTPUT_MANIFEST)
    if not isinstance(output, dict) or not isinstance(output.get("files"), list):
        raise mx.AnalysisException(f"{OUTPUT_MANIFEST} must contain a files array.")

    files = [(OUTPUT_MANIFEST, output_path)]
    for entry in output["files"]:
        filename = entry.get("filename") if isinstance(entry, dict) else None
        if not isinstance(filename, str) or not filename:
            continue
        # meorg-run writes filenames relative to the run directory.
        path = run_dir / filename
        if path.is_file():
            files.append((path.name, path))
        elif entry.get("error") in (None, "", "ok"):
            # An entry with an error is not uploaded, so its file may be absent.
            raise mx.AnalysisException(
                f"{OUTPUT_MANIFEST} lists a missing file: {filename}"
            )

    if (run_dir / PALS_LOG).is_file():
        files.append(("PALS.log", run_dir / PALS_LOG))
    files.append((RUN_RECORD, run_dir / RUN_RECORD))
    return files


def plan_result_submission(
    run_dir: Union[str, Path],
    input_path: Optional[Union[str, Path]] = None,
    runner: str = "gadi",
    orchestrator: Optional[str] = None,
) -> dict:
    """Read a run directory and list what to send to analysis-result.

    Parameters
    ----------
    run_dir : path-like
        Run directory written by ``meorg-run``.
    input_path : path-like, optional
        The run's input.json, by default ``RUN_DIR/input.json``.
    runner : str, optional
        Runner name, by default ``gadi``.
    orchestrator : str, optional
        Orchestrator name added to the metadata.

    Returns
    -------
    dict
        ``outcome``, ``externalRunId``, ``runner``, ``metadata`` (dict),
        ``files`` (list of (part name, path)), and ``warnings``.
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise mx.AnalysisException(f"Run directory not found: {run_dir}")

    run = _read_json(run_dir / RUN_RECORD, RUN_RECORD)
    if not isinstance(run, dict):
        raise mx.AnalysisException(f"{RUN_RECORD} must contain a JSON object.")

    external_run_id = run.get("externalRunId")
    if not isinstance(external_run_id, str) or not external_run_id.strip():
        raise mx.AnalysisException(f"{RUN_RECORD} has no externalRunId.")

    outcome = "success" if run.get("status") == "success" else "failure"

    input_explicit = input_path is not None
    input_path = Path(input_path) if input_explicit else run_dir / INPUT_MANIFEST
    if input_explicit and not input_path.is_file():
        raise mx.AnalysisException(f"Input file not found: {input_path}")

    warnings = []
    if outcome == "success":
        files = _success_files(run_dir)
    else:
        candidates = (
            ("PALS.log", run_dir / PALS_LOG),
            (STDERR_LOG, run_dir / STDERR_LOG),
            (RUN_RECORD, run_dir / RUN_RECORD),
        )
        files = [(name, path) for name, path in candidates if path.is_file()]

    if input_path.is_file():
        files.append((INPUT_MANIFEST, input_path))
    else:
        warnings.append(
            f"No {INPUT_MANIFEST} in the run directory. ME.org will not hold worker "
            "runs of this model output until its files are uploaded."
        )

    # The same file may be named twice, for example a log listed in output.json.
    unique = []
    seen = set()
    for name, path in files:
        marker = os.path.abspath(path)
        if marker not in seen:
            seen.add(marker)
            unique.append((name, path))

    metadata = dict(run)
    metadata["client"] = CLIENT_NAME
    metadata["meorgClientVersion"] = __version__
    if orchestrator:
        metadata["orchestrator"] = orchestrator

    return {
        "outcome": outcome,
        "externalRunId": external_run_id,
        "runner": runner,
        "metadata": metadata,
        "files": unique,
        "warnings": warnings,
    }


def submit_analysis_result(
    client,
    model_output_id: str,
    experiment_id: str,
    run_dir: Union[str, Path],
    input_path: Optional[Union[str, Path]] = None,
    runner: str = "gadi",
    orchestrator: Optional[str] = None,
    retries: int = mcc.ANALYSIS_RESULT_RETRIES,
    backoff: float = mcc.ANALYSIS_RESULT_BACKOFF,
    sleep=time.sleep,
) -> dict:
    """Post a run directory to analysis-result.

    A network error or a 5xx response is retried ``retries`` times, with the
    wait doubling from ``backoff`` seconds. Any 4xx response fails at once.

    Returns
    -------
    dict
        ``analysisId``, ``status``, ``created`` and ``warnings``.
    """
    plan = plan_result_submission(run_dir, input_path, runner, orchestrator)

    attempt = 0
    while True:
        try:
            response = client.post_analysis_result(
                model_output_id,
                experiment_id,
                outcome=plan["outcome"],
                external_run_id=plan["externalRunId"],
                runner=plan["runner"],
                metadata=json.dumps(plan["metadata"]),
                files=plan["files"],
            )
            break
        except RequestException as ex:
            if ex.status_code < 500 or attempt >= retries:
                raise mx.AnalysisException(
                    describe_request_error(ex, "analysis-result")
                ) from None
        except requests.exceptions.RequestException as ex:
            if attempt >= retries:
                raise mx.AnalysisException(
                    f"analysis-result: network error after {attempt + 1} attempts "
                    f"({type(ex).__name__})."
                ) from None
        sleep(backoff * (2**attempt))
        attempt += 1

    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict) or not data.get("analysisId"):
        raise mx.AnalysisException("analysis-result: the server sent an invalid response.")

    return {
        "analysisId": data["analysisId"],
        "status": data.get("status"),
        "created": data.get("created"),
        "warnings": plan["warnings"],
    }


def guess_mimetype(name: str) -> str:
    """MIME type for a result part."""
    if name.endswith(".log"):
        return "text/plain"
    return mt.guess_type(name)[0] or "application/octet-stream"
