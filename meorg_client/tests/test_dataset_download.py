"""Unit tests for experiment dataset downloads."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests
from click.testing import CliRunner

import meorg_client.cli as cli
from meorg_client.client import Client
from meorg_client.exceptions import DownloadException


def _response(status_code=200, content=b"", headers=None, json_body=None):
    """Build a requests.Response with its body already consumed.

    Parameters
    ----------
    status_code : int, optional
        HTTP status code, by default 200.
    content : bytes, optional
        Response body, by default b"".
    headers : dict, optional
        Response headers, by default None.
    json_body : dict, optional
        Body to encode as JSON instead of `content`, by default None.

    Returns
    -------
    requests.Response
        Response object.
    """
    response = requests.Response()
    response.status_code = status_code
    response.headers.update(headers or {})
    if json_body is not None:
        content = json.dumps(json_body).encode("utf-8")
        response.headers["Content-Type"] = "application/json"
    response._content = content
    response._content_consumed = True
    response.encoding = "utf-8"
    return response


def _manifest(files, url_suffix=""):
    """Build a manifest that wraps a list of file entries.

    Parameters
    ----------
    files : list
        File entries, as built by `_file`.
    url_suffix : str, optional
        Text to append to each signed URL, by default "".

    Returns
    -------
    dict
        Manifest as the server would return it.
    """
    now = datetime.now(timezone.utc)
    return {
        "experimentId": "experiment-1",
        "generatedAt": now.isoformat(),
        "urlsExpireAt": (now + timedelta(hours=1)).isoformat(),
        "fileCount": len(files),
        "totalBytes": sum(file_info["size"] for file_info in files),
        "files": [
            {**file_info, "url": file_info["url"] + url_suffix} for file_info in files
        ],
    }


def _file(file_id, relative_path, content, url=None):
    """Build one manifest file entry.

    Parameters
    ----------
    file_id : str
        Manifest file ID.
    relative_path : str
        Path below the output directory.
    content : bytes
        File content, used to set the declared size.
    url : str, optional
        Signed URL, by default derived from `file_id`.

    Returns
    -------
    dict
        Manifest file entry.
    """
    return {
        "fileId": file_id,
        "datasetId": "dataset-1",
        "versionId": "version-1",
        "name": Path(relative_path).name,
        "relativePath": relative_path,
        "size": len(content),
        "contentType": "application/x-netcdf",
        "url": url or f"https://objects.example/{file_id}",
    }


def test_get_experiment_dataset_manifest_uses_authenticated_endpoint():
    """Test the manifest request uses the right URL and auth headers."""
    manifest = _manifest([])
    client = Client()
    client.base_url = "https://meorg.example/api"
    client.headers.update({"X-User-Id": "user-1", "X-Auth-Token": "secret-token"})

    with patch(
        "meorg_client.client.requests.get",
        return_value=_response(json_body=manifest),
    ) as get:
        assert client.get_experiment_dataset_manifest("experiment-1") == manifest

    args, kwargs = get.call_args
    assert args[0] == (
        "https://meorg.example/api/experiment/experiment-1/datasets/manifest"
    )
    assert kwargs["headers"]["X-User-Id"] == "user-1"
    assert kwargs["headers"]["X-Auth-Token"] == "secret-token"


def test_downloads_manifest_files_in_parallel_layout(tmp_path):
    """Test files land under the output directory using their relative paths."""
    contents = {"file-1": b"first", "file-2": b"second"}
    files = [
        _file("file-1", "datasets/dataset-1/first.nc", contents["file-1"]),
        _file("file-2", "datasets/dataset-1/second.nc", contents["file-2"]),
    ]
    client = Client()
    client.get_experiment_dataset_manifest = Mock(return_value=_manifest(files))

    def get_object(url, **kwargs):
        return _response(content=contents[url.rsplit("/", 1)[-1]])

    with patch("meorg_client.downloads.requests.get", side_effect=get_object):
        summary = client.download_experiment_datasets(
            "experiment-1", tmp_path, n=2, progress=False
        )

    assert (tmp_path / "datasets/dataset-1/first.nc").read_bytes() == b"first"
    assert (tmp_path / "datasets/dataset-1/second.nc").read_bytes() == b"second"
    assert summary["fileCount"] == 2
    assert summary["totalBytes"] == 11
    assert len(summary["downloaded"]) == 2
    assert summary["skipped"] == []


def test_resumes_partial_file_with_range_request(tmp_path):
    """Test a partial file resumes from its current offset."""
    content = b"abcdefghij"
    file_info = _file("file-1", "datasets/dataset-1/forcing.nc", content)
    partial = tmp_path / "datasets/dataset-1/forcing.nc.file-1.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(content[:4])
    client = Client()
    client.get_experiment_dataset_manifest = Mock(return_value=_manifest([file_info]))

    def get_object(url, headers, **kwargs):
        assert headers == {"Range": "bytes=4-"}
        return _response(
            status_code=206,
            content=content[4:],
            headers={"Content-Range": "bytes 4-9/10"},
        )

    with patch("meorg_client.downloads.requests.get", side_effect=get_object):
        client.download_experiment_datasets(
            "experiment-1", tmp_path, n=1, progress=False
        )

    assert (tmp_path / "datasets/dataset-1/forcing.nc").read_bytes() == content
    assert not partial.exists()


def test_restarts_partial_file_when_range_is_ignored(tmp_path):
    """Test the file restarts when the object store ignores the Range header."""
    content = b"abcdefghij"
    file_info = _file("file-1", "datasets/dataset-1/forcing.nc", content)
    partial = tmp_path / "datasets/dataset-1/forcing.nc.file-1.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(content[:4])
    client = Client()
    client.get_experiment_dataset_manifest = Mock(return_value=_manifest([file_info]))

    with patch(
        "meorg_client.downloads.requests.get",
        return_value=_response(content=content),
    ) as get:
        client.download_experiment_datasets(
            "experiment-1", tmp_path, n=1, progress=False
        )

    assert get.call_args.kwargs["headers"] == {"Range": "bytes=4-"}
    assert (tmp_path / "datasets/dataset-1/forcing.nc").read_bytes() == content


def test_no_resume_restarts_each_transfer_attempt(tmp_path):
    """Test --no-resume sends no Range header on any attempt."""
    content = b"abcdefghij"
    file_info = _file("file-1", "datasets/dataset-1/forcing.nc", content)
    partial = tmp_path / "datasets/dataset-1/forcing.nc.file-1.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"old")
    client = Client()
    client.get_experiment_dataset_manifest = Mock(return_value=_manifest([file_info]))
    responses = [_response(content=b"short"), _response(content=content)]

    with patch("meorg_client.downloads.requests.get", side_effect=responses) as get:
        client.download_experiment_datasets(
            "experiment-1", tmp_path, n=1, progress=False, resume=False
        )

    assert [call.kwargs["headers"] for call in get.call_args_list] == [{}, {}]
    assert (tmp_path / "datasets/dataset-1/forcing.nc").read_bytes() == content


def test_ignores_a_partial_left_by_a_different_file(tmp_path):
    """A .part from another version must never be appended to.

    The manifest carries no checksum, so resuming a stale partial whose length
    happened to reach the new size would silently produce a file that never
    existed on the server.
    """
    content = b"abcdefghij"
    file_info = _file("file-2", "datasets/dataset-1/forcing.nc", content)
    stale = tmp_path / "datasets/dataset-1/forcing.nc.file-1.part"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"OLD")
    client = Client()
    client.get_experiment_dataset_manifest = Mock(return_value=_manifest([file_info]))

    with patch(
        "meorg_client.downloads.requests.get",
        return_value=_response(content=content),
    ) as get:
        client.download_experiment_datasets(
            "experiment-1", tmp_path, n=1, progress=False
        )

    # No Range header: the stale partial was not treated as resumable.
    assert get.call_args.kwargs["headers"] == {}
    assert (tmp_path / "datasets/dataset-1/forcing.nc").read_bytes() == content
    assert stale.read_bytes() == b"OLD"


def test_refreshes_manifest_after_signed_url_is_rejected(tmp_path):
    """Test a rejected URL triggers one manifest refresh and a retry."""
    content = b"forcing"
    old_file = _file(
        "file-1",
        "datasets/dataset-1/forcing.nc",
        content,
        url="https://objects.example/old",
    )
    new_file = {**old_file, "url": "https://objects.example/new"}
    client = Client()
    client.get_experiment_dataset_manifest = Mock(
        side_effect=[_manifest([old_file]), _manifest([new_file])]
    )

    def get_object(url, **kwargs):
        if url.endswith("/old"):
            return _response(status_code=403, content=b"expired")
        return _response(content=content)

    with patch("meorg_client.downloads.requests.get", side_effect=get_object):
        summary = client.download_experiment_datasets(
            "experiment-1", tmp_path, n=1, progress=False
        )

    assert client.get_experiment_dataset_manifest.call_count == 2
    assert len(summary["downloaded"]) == 1
    assert (tmp_path / "datasets/dataset-1/forcing.nc").read_bytes() == content


def test_refreshes_manifest_when_declared_expiry_is_in_the_past(tmp_path):
    """Test an already expired manifest is requested again before any transfer."""
    content = b"forcing"
    file_info = _file("file-1", "datasets/dataset-1/forcing.nc", content)
    expired_manifest = _manifest([file_info])
    expired_manifest["urlsExpireAt"] = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).isoformat()
    client = Client()
    client.get_experiment_dataset_manifest = Mock(
        side_effect=[expired_manifest, _manifest([file_info])]
    )

    with patch(
        "meorg_client.downloads.requests.get",
        return_value=_response(content=content),
    ):
        client.download_experiment_datasets(
            "experiment-1", tmp_path, n=1, progress=False
        )

    assert client.get_experiment_dataset_manifest.call_count == 2


def test_skips_file_that_already_has_the_manifest_size(tmp_path):
    """Test a complete file is not downloaded again."""
    content = b"complete"
    file_info = _file("file-1", "datasets/dataset-1/forcing.nc", content)
    target = tmp_path / file_info["relativePath"]
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    client = Client()
    client.get_experiment_dataset_manifest = Mock(return_value=_manifest([file_info]))

    with patch("meorg_client.downloads.requests.get") as get:
        summary = client.download_experiment_datasets(
            "experiment-1", tmp_path, n=1, progress=False
        )

    get.assert_not_called()
    assert summary["downloaded"] == []
    assert summary["skipped"] == [str(target.resolve())]


def test_rejects_path_that_escapes_output_directory(tmp_path):
    """Test a relative path that escapes the output directory is rejected."""
    file_info = _file("file-1", "../outside.nc", b"data")
    client = Client()
    client.get_experiment_dataset_manifest = Mock(return_value=_manifest([file_info]))

    with pytest.raises(DownloadException, match="unsafe path"):
        client.download_experiment_datasets(
            "experiment-1", tmp_path, n=1, progress=False
        )


def test_rejects_duplicate_output_paths(tmp_path):
    """Test two files that resolve to one path are rejected."""
    files = [
        _file("file-1", "datasets/dataset-1/forcing.nc", b"first"),
        _file("file-2", "datasets/dataset-1/forcing.nc", b"second"),
    ]
    client = Client()
    client.get_experiment_dataset_manifest = Mock(return_value=_manifest(files))

    with pytest.raises(DownloadException, match="duplicate path"):
        client.download_experiment_datasets(
            "experiment-1", tmp_path, n=1, progress=False
        )


def test_wraps_transport_error_for_cli_compatibility(tmp_path):
    """Test a transport error becomes a DownloadException with no URL in it."""
    file_info = _file("file-1", "datasets/dataset-1/forcing.nc", b"data")
    client = Client()
    client.get_experiment_dataset_manifest = Mock(return_value=_manifest([file_info]))

    with (
        patch(
            "meorg_client.downloads.requests.get",
            side_effect=requests.exceptions.ConnectionError("signed URL is secret"),
        ),
        pytest.raises(DownloadException) as error,
    ):
        client.download_experiment_datasets(
            "experiment-1", tmp_path, n=1, progress=False
        )

    assert error.value.msg == (
        "The transfer failed twice for datasets/dataset-1/forcing.nc."
    )
    assert "secret" not in error.value.msg


def test_retries_and_wraps_object_store_http_error(tmp_path):
    """Test an object store error is retried once and reported without its body."""
    file_info = _file("file-1", "datasets/dataset-1/forcing.nc", b"data")
    client = Client()
    client.get_experiment_dataset_manifest = Mock(return_value=_manifest([file_info]))
    error_body = b"https://objects.example/file-1?X-Amz-Signature=secret"

    with (
        patch(
            "meorg_client.downloads.requests.get",
            return_value=_response(status_code=500, content=error_body),
        ) as get,
        pytest.raises(DownloadException) as error,
    ):
        client.download_experiment_datasets(
            "experiment-1", tmp_path, n=1, progress=False
        )

    assert get.call_count == 2
    assert error.value.msg == (
        "The transfer failed twice for datasets/dataset-1/forcing.nc."
    )
    assert "secret" not in error.value.msg
    assert "objects.example" not in error.value.msg


def test_rejects_file_with_wrong_downloaded_size(tmp_path):
    """Test a short transfer is rejected against the manifest size."""
    file_info = _file("file-1", "datasets/dataset-1/forcing.nc", b"expected")
    client = Client()
    client.get_experiment_dataset_manifest = Mock(return_value=_manifest([file_info]))

    with (
        patch(
            "meorg_client.downloads.requests.get",
            return_value=_response(content=b"short"),
        ),
        pytest.raises(DownloadException, match="ended before all bytes arrived"),
    ):
        client.download_experiment_datasets(
            "experiment-1", tmp_path, n=1, progress=False
        )


def test_dataset_download_cli_reports_summary(tmp_path, monkeypatch):
    """Test the CLI forwards its options and prints the summary."""
    summary = {
        "experimentId": "experiment-1",
        "outputDir": str(tmp_path),
        "fileCount": 1,
        "totalBytes": 10,
        "downloaded": [str(tmp_path / "forcing.nc")],
        "skipped": [],
    }
    client = Mock()
    client.download_experiment_datasets.return_value = summary
    monkeypatch.setattr(cli, "_get_client", lambda: client)

    result = CliRunner().invoke(
        cli.cli,
        [
            "dataset",
            "download",
            "experiment-1",
            "--output-dir",
            str(tmp_path),
            "--threads",
            "3",
        ],
    )

    assert result.exit_code == 0
    assert "Downloaded files: 1. Already complete: 0." in result.output
    client.download_experiment_datasets.assert_called_once_with(
        experiment_id="experiment-1",
        output_dir=tmp_path,
        n=3,
        resume=True,
        progress=True,
    )
