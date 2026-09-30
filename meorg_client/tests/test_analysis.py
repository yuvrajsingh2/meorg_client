"""Unit tests for `meorg analysis input` and `meorg analysis submit-result`.

Every HTTP call is mocked. No server is needed.
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests
from click.testing import CliRunner

import meorg_client.analysis as mea
import meorg_client.cli as cli
from meorg_client.client import Client
from meorg_client.exceptions import AnalysisException, RequestException

MO_ID = "mo-1"
EXP_ID = "exp-1"
SECRET = "X-Amz-Signature=secret"


def _response(status_code=200, content=b"", headers=None, json_body=None):
    """Build a requests.Response with its body already consumed."""
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


def _entry(type_, number, key, content, filename=None, **extra):
    """One analysis-input file entry."""
    return {
        "type": type_,
        "number": number,
        "name": f"{type_}-{number}",
        "setId": f"set-{type_}-{number}",
        "setModified": "2026-09-01T00:00:00.000Z",
        "filename": filename or f"{key.rsplit('/', 1)[-1]}.nc",
        "size": len(content),
        "key": key,
        "url": f"https://objects.example/{key}?{SECRET}",
        **extra,
    }


# Object key -> bytes held by the fake object store
OBJECTS = {
    "pals/data/ds1": b"dataset one",
    "pals/data/ds2": b"dataset two!",
    "pals/data/bm1": b"benchmark",
    "pals/data/mo1": b"model output one",
}


def _input_response(files=None):
    """analysis-input response for the default experiment."""
    if files is None:
        files = [
            _entry("DataSet", 1, "pals/data/ds1", OBJECTS["pals/data/ds1"]),
            _entry("DataSet", 2, "pals/data/ds2", OBJECTS["pals/data/ds2"]),
            _entry("Benchmark", 1, "pals/data/bm1", OBJECTS["pals/data/bm1"]),
            _entry(
                "ModelOutput",
                1,
                "pals/data/mo1",
                OBJECTS["pals/data/mo1"],
                modelName="CABLE",
            ),
            # The benchmark again, as model output 2, with the same key
            _entry(
                "ModelOutput",
                2,
                "pals/data/bm1",
                OBJECTS["pals/data/bm1"],
                modelName="CABLE",
            ),
        ]
    return {
        "status": "success",
        "data": {
            "experimentId": EXP_ID,
            "modelOutput": {
                "id": MO_ID,
                "name": "my-output",
                "modified": "2026-09-30T00:00:00.000Z",
                "modelName": "CABLE",
            },
            "config": None,
            "urlsExpireAt": "2099-01-01T00:00:00.000Z",
            "files": files,
        },
    }


def _object_get(calls):
    """Fake object store GET that records each requested key."""

    def get(url, **kwargs):
        key = url.split("objects.example/", 1)[1].split("?", 1)[0]
        calls.append(key)
        return _response(content=OBJECTS[key])

    return get


def _client(response=None):
    client = Client()
    client.get_analysis_input = Mock(return_value=response or _input_response())
    return client


def _prepare(client, tmp_path, **kwargs):
    kwargs.setdefault("cache", tmp_path / "cache")
    return mea.prepare_analysis_input(
        client, MO_ID, EXP_ID, run_id="run-1", n=2, **kwargs
    )


def _seed(root: Path, key: str, content: bytes):
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# analysis input
# ---------------------------------------------------------------------------


def test_get_analysis_input_uses_authenticated_endpoint():
    """The request goes to the analysis-input route with the auth headers."""
    client = Client()
    client.base_url = "https://meorg.example/api"
    client.headers.update({"X-User-Id": "user-1", "X-Auth-Token": "token"})
    with patch(
        "meorg_client.client.requests.get",
        return_value=_response(json_body=_input_response()),
    ) as get:
        client.get_analysis_input(MO_ID, EXP_ID)

    args, kwargs = get.call_args
    assert args[0] == f"https://meorg.example/api/modeloutput/{MO_ID}/{EXP_ID}/analysis-input"
    assert kwargs["headers"]["X-Auth-Token"] == "token"


def test_cache_miss_downloads_each_key_once(tmp_path):
    """A cold cache downloads every object once, even when two entries share a key."""
    calls = []
    with patch("meorg_client.downloads.requests.get", side_effect=_object_get(calls)):
        summary = _prepare(_client(), tmp_path)

    assert sorted(calls) == sorted(OBJECTS)
    assert summary["downloaded"] == 4
    assert summary["cached"] == 0
    for item in summary["input"]["files"]:
        path = Path(item["path"])
        assert path == tmp_path / "cache" / item["key"]
        assert path.read_bytes() == OBJECTS[item["key"]]


def test_cache_hit_makes_no_download(tmp_path):
    """A second run finds every object in the cache."""
    with patch("meorg_client.downloads.requests.get", side_effect=_object_get([])):
        _prepare(_client(), tmp_path)

    with patch("meorg_client.downloads.requests.get") as get:
        summary = _prepare(_client(), tmp_path)

    get.assert_not_called()
    assert summary["downloaded"] == 0
    assert summary["cached"] == 4


def test_wrong_size_file_is_downloaded_again(tmp_path):
    """A cached file with the wrong size is a miss and is replaced."""
    cache = tmp_path / "cache"
    _seed(cache, "pals/data/ds1", b"truncated")
    for key in ("pals/data/ds2", "pals/data/bm1", "pals/data/mo1"):
        _seed(cache, key, OBJECTS[key])

    calls = []
    with patch("meorg_client.downloads.requests.get", side_effect=_object_get(calls)):
        summary = _prepare(_client(), tmp_path)

    assert calls == ["pals/data/ds1"]
    assert (cache / "pals/data/ds1").read_bytes() == OBJECTS["pals/data/ds1"]
    assert summary["cached"] == 3


def test_partial_download_is_never_visible_at_the_key(tmp_path):
    """Bytes go to <cache>/.tmp first. A short transfer never reaches the key."""
    cache = tmp_path / "cache"
    seen_during_transfer = []

    def get(url, **kwargs):
        key = url.split("objects.example/", 1)[1].split("?", 1)[0]
        seen_during_transfer.append((cache / key).exists())
        # Always one byte short, so the transfer never completes
        return _response(content=OBJECTS[key][:-1])

    with patch("meorg_client.downloads.requests.get", side_effect=get):
        with pytest.raises(Exception) as info:
            _prepare(_client(), tmp_path)

    assert SECRET not in str(info.value)
    assert not any(seen_during_transfer)
    for key in OBJECTS:
        assert not (cache / key).exists()
    # Failed partial files are removed
    assert list((cache / ".tmp").iterdir()) == []


def test_download_goes_through_tmp_then_rename(tmp_path):
    """The partial file lives in <cache>/.tmp and is renamed onto the key."""
    cache = tmp_path / "cache"
    partial_dirs = []
    real_transfer = mea.med._transfer_file

    def transfer(file_info, partial, bar):
        partial_dirs.append(Path(partial).parent)
        return real_transfer(file_info, partial, bar)

    with patch("meorg_client.downloads.requests.get", side_effect=_object_get([])), patch(
        "meorg_client.downloads._transfer_file", side_effect=transfer
    ):
        _prepare(_client(), tmp_path)

    assert set(partial_dirs) == {cache / ".tmp"}
    assert list((cache / ".tmp").iterdir()) == []


def test_read_only_roots_are_searched_first_and_never_written(tmp_path):
    """A hit in a read-only root wins. Misses go to the writable cache only."""
    ro1 = tmp_path / "ro1"
    ro2 = tmp_path / "ro2"
    cache = tmp_path / "cache"
    _seed(ro1, "pals/data/ds1", OBJECTS["pals/data/ds1"])
    _seed(ro2, "pals/data/ds1", OBJECTS["pals/data/ds1"])  # second root loses
    _seed(cache, "pals/data/ds1", OBJECTS["pals/data/ds1"])  # writable loses
    _seed(ro2, "pals/data/ds2", b"wrong size in ro2")  # skipped, then miss
    _seed(ro2, "pals/data/bm1", OBJECTS["pals/data/bm1"])
    before = {p: p.stat().st_mtime_ns for p in list(ro1.rglob("*")) + list(ro2.rglob("*"))}

    calls = []
    with patch("meorg_client.downloads.requests.get", side_effect=_object_get(calls)):
        summary = _prepare(_client(), tmp_path, cache_ro=[ro1, ro2])

    paths = {item["key"]: Path(item["path"]) for item in summary["input"]["files"]}
    assert paths["pals/data/ds1"] == ro1 / "pals/data/ds1"
    assert paths["pals/data/bm1"] == ro2 / "pals/data/bm1"
    assert paths["pals/data/ds2"] == cache / "pals/data/ds2"
    assert sorted(calls) == ["pals/data/ds2", "pals/data/mo1"]

    after = {p: p.stat().st_mtime_ns for p in list(ro1.rglob("*")) + list(ro2.rglob("*"))}
    assert after == before
    assert (ro2 / "pals/data/ds2").read_bytes() == b"wrong size in ro2"


@pytest.mark.parametrize(
    "key",
    ["/pals/data/x", "pals/../../etc/passwd", "..", "", "pals\\data\\x", ".tmp/abc"],
)
def test_bad_keys_are_rejected(tmp_path, key):
    """Absolute, escaping or empty keys stop the command before any download."""
    files = [_entry("DataSet", 1, "pals/data/ds1", b"x"), _entry("ModelOutput", 1, "pals/data/mo1", b"y")]
    files[0]["key"] = key
    with patch("meorg_client.downloads.requests.get") as get:
        with pytest.raises(AnalysisException):
            _prepare(_client(_input_response(files)), tmp_path)
    get.assert_not_called()


def test_local_model_output_files_replace_number_one(tmp_path):
    """Local files become model output 1, first among the ModelOutput entries."""
    local = tmp_path / "outputs"
    local.mkdir()
    (local / "b_out.nc").write_bytes(b"bbbb")
    (local / "a_out.nc").write_bytes(b"aa")

    calls = []
    with patch("meorg_client.downloads.requests.get", side_effect=_object_get(calls)):
        summary = _prepare(
            _client(),
            tmp_path,
            model_output_files=[local / "b_out.nc", local / "a_out.nc"],
        )

    # The server's model output 1 object is not downloaded
    assert "pals/data/mo1" not in calls
    files = summary["input"]["files"]
    assert [(f["type"], f["number"]) for f in files] == [
        ("DataSet", 1),
        ("DataSet", 2),
        ("Benchmark", 1),
        ("ModelOutput", 1),
        ("ModelOutput", 1),
        ("ModelOutput", 2),
    ]
    first = files[3]
    assert first == {
        "type": "ModelOutput",
        "number": 1,
        "name": "my-output",
        "setId": MO_ID,
        "setModified": "2026-09-30T00:00:00.000Z",
        "modelName": "CABLE",
        "filename": "b_out.nc",
        "size": 4,
        "path": str(local / "b_out.nc"),
    }
    assert files[4]["filename"] == "a_out.nc"
    assert files[4]["size"] == 2


def test_empty_trigger_needs_local_files(tmp_path):
    """No server model output 1 and no local files is an error."""
    files = [_entry("DataSet", 1, "pals/data/ds1", OBJECTS["pals/data/ds1"])]
    with pytest.raises(AnalysisException, match="No model output files"):
        _prepare(_client(_input_response(files)), tmp_path)


def test_empty_trigger_with_local_files(tmp_path):
    """An empty model output record works with local files."""
    files = [
        _entry("DataSet", 1, "pals/data/ds1", OBJECTS["pals/data/ds1"]),
        _entry("ModelOutput", 2, "pals/data/bm1", OBJECTS["pals/data/bm1"]),
    ]
    local = tmp_path / "out.nc"
    local.write_bytes(b"local")
    with patch("meorg_client.downloads.requests.get", side_effect=_object_get([])):
        summary = _prepare(
            _client(_input_response(files)), tmp_path, model_output_files=[local]
        )
    assert [(f["type"], f["number"]) for f in summary["input"]["files"]] == [
        ("DataSet", 1),
        ("ModelOutput", 1),
        ("ModelOutput", 2),
    ]


def test_duplicate_local_basenames_are_rejected(tmp_path):
    """ME.org matches uploads by basename, so two files cannot share one."""
    for sub in ("a", "b"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "out.nc").write_bytes(b"x")
    with pytest.raises(AnalysisException, match="same name"):
        _prepare(
            _client(),
            tmp_path,
            model_output_files=[tmp_path / "a/out.nc", tmp_path / "b/out.nc"],
        )


def test_url_is_never_written(tmp_path):
    """input.json has no url field and no signature text."""
    with patch("meorg_client.downloads.requests.get", side_effect=_object_get([])):
        summary = _prepare(_client(), tmp_path)
    output = mea.write_input_json(summary["input"], tmp_path / "run" / "input.json")

    text = output.read_text()
    assert "url" not in json.loads(text)["files"][0]
    assert SECRET not in text
    assert "objects.example" not in text
    document = json.loads(text)
    assert document["_id"] == "run-1"
    assert document["config"] is None
    assert document["provenance"]["client"] == "meorg_client"
    assert document["provenance"]["modelOutputId"] == MO_ID
    assert document["provenance"]["experimentId"] == EXP_ID
    # Extra server fields are kept
    assert document["files"][0]["setId"] == "set-DataSet-1"


def test_expired_url_is_refreshed_once(tmp_path):
    """A 403 from the object store asks for new URLs and retries."""
    responses = [_input_response(), _input_response()]
    client = Client()
    client.get_analysis_input = Mock(side_effect=responses)
    state = {"first": True}

    def get(url, **kwargs):
        key = url.split("objects.example/", 1)[1].split("?", 1)[0]
        if key == "pals/data/ds1" and state["first"]:
            state["first"] = False
            return _response(status_code=403)
        return _response(content=OBJECTS[key])

    with patch("meorg_client.downloads.requests.get", side_effect=get):
        summary = _prepare(client, tmp_path)

    assert client.get_analysis_input.call_count == 2
    assert summary["downloaded"] == 4


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (409, {"status": "error", "message": "An input file is not in the object store yet"}, "upload has not finished"),
        (400, {"status": "fail", "data": {"experiment": "Experiment not found"}}, "Experiment not found"),
    ],
)
def test_server_errors_are_clear(tmp_path, status, body, expected):
    """409 and 400 from analysis-input become clear messages."""
    client = Client()
    client.get_analysis_input = Mock(
        side_effect=RequestException(status, json.dumps(body))
    )
    with pytest.raises(AnalysisException, match=expected):
        _prepare(client, tmp_path)


def test_input_cli_writes_input_json(tmp_path, monkeypatch):
    """The CLI accepts --model-output-files FILE FILE and prints the path."""
    local = tmp_path / "outputs"
    local.mkdir()
    for name in ("a.nc", "b.nc"):
        (local / name).write_bytes(b"data")
    client = _client()
    monkeypatch.setattr(cli, "_get_client", lambda: client)

    with patch("meorg_client.downloads.requests.get", side_effect=_object_get([])):
        result = CliRunner().invoke(
            cli.cli,
            [
                "analysis", "input", MO_ID, EXP_ID,
                "--run-id", "run-1",
                "--cache", str(tmp_path / "cache"),
                "--model-output-files", str(local / "a.nc"), str(local / "b.nc"),
                "-o", str(tmp_path / "run" / "input.json"),
                "-n", "2",
            ],
        )

    assert result.exit_code == 0, result.output
    # click 8.1 mixes stderr into stdout, so read the last line
    assert result.stdout.strip().splitlines()[-1] == str(tmp_path / "run" / "input.json")
    document = json.loads((tmp_path / "run" / "input.json").read_text())
    names = [f["filename"] for f in document["files"] if f["number"] == 1 and f["type"] == "ModelOutput"]
    assert names == ["a.nc", "b.nc"]
    assert SECRET not in result.output


def test_input_cli_rejects_stray_arguments(tmp_path, monkeypatch):
    """A file without --model-output-files is a usage error."""
    monkeypatch.setattr(cli, "_get_client", lambda: _client())
    result = CliRunner().invoke(
        cli.cli,
        ["analysis", "input", MO_ID, EXP_ID, "stray.nc", "--run-id", "r", "--cache", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "--model-output-files" in result.output


def test_input_cli_exits_non_zero_on_409(tmp_path, monkeypatch):
    """A 409 exits 1 with a message and no traceback."""
    client = Client()
    client.get_analysis_input = Mock(side_effect=RequestException(409, '{"status":"error","message":"x"}'))
    monkeypatch.setattr(cli, "_get_client", lambda: client)
    result = CliRunner().invoke(
        cli.cli,
        ["analysis", "input", MO_ID, EXP_ID, "--run-id", "r", "--cache", str(tmp_path)],
    )
    assert result.exit_code == 1
    assert "upload has not finished" in result.output


# ---------------------------------------------------------------------------
# analysis submit-result
# ---------------------------------------------------------------------------


def _run_dir(tmp_path, status="success", with_input=True, stderr=False):
    run = tmp_path / "run"
    (run / "output").mkdir(parents=True)
    record = {
        "schema_version": 1,
        "externalRunId": "run-1",
        "status": status,
        "exitCode": 0 if status == "success" else 1,
        "error": None if status == "success" else "boom",
    }
    (run / "run.json").write_text(json.dumps(record))
    if status == "success":
        (run / "output" / "plot1.png").write_bytes(b"png1")
        (run / "output" / "stats.json").write_text("{}")
        (run / "output.json").write_text(
            json.dumps(
                {
                    "files": [
                        {"type": "image", "filename": "output/plot1.png"},
                        {"type": "json", "filename": "output/stats.json", "error": "ok"},
                        {"type": "image", "filename": None, "error": "no data"},
                        {"type": "image", "filename": "output/failed.png", "error": "plot failed"},
                    ]
                }
            )
        )
    (run / "output" / "PALS.log").write_text("log")
    if stderr:
        (run / "r-stderr.log").write_text("stderr")
    if with_input:
        (run / "input.json").write_text('{"files": []}')
    return run


def _names(files):
    return [name for name, _ in files]


def test_success_part_list(tmp_path):
    """A success sends output.json, its files, the log, run.json and input.json."""
    run = _run_dir(tmp_path)
    plan = mea.plan_result_submission(run, orchestrator="benchcab")
    assert plan["outcome"] == "success"
    assert plan["externalRunId"] == "run-1"
    assert _names(plan["files"]) == [
        "output.json",
        "plot1.png",
        "stats.json",
        "PALS.log",
        "run.json",
        "input.json",
    ]
    metadata = plan["metadata"]
    assert metadata["status"] == "success"
    assert metadata["client"] == "meorg_client"
    assert metadata["meorgClientVersion"]
    assert metadata["orchestrator"] == "benchcab"


def test_failure_part_list(tmp_path):
    """A failure sends only the logs, run.json and input.json that exist."""
    run = _run_dir(tmp_path, status="failure", stderr=True)
    plan = mea.plan_result_submission(run)
    assert plan["outcome"] == "failure"
    assert _names(plan["files"]) == ["PALS.log", "r-stderr.log", "run.json", "input.json"]
    assert "orchestrator" not in plan["metadata"]


def test_any_other_status_is_a_failure(tmp_path):
    run = _run_dir(tmp_path, status="failure")
    record = json.loads((run / "run.json").read_text())
    record["status"] = "cancelled"
    (run / "run.json").write_text(json.dumps(record))
    assert mea.plan_result_submission(run)["outcome"] == "failure"


def test_input_option_is_sent_as_input_json(tmp_path):
    """--input is sent under the part name input.json."""
    run = _run_dir(tmp_path, with_input=False)
    other = tmp_path / "elsewhere.json"
    other.write_text("{}")
    plan = mea.plan_result_submission(run, input_path=other)
    assert ("input.json", other) in plan["files"]


def test_missing_default_input_warns(tmp_path):
    run = _run_dir(tmp_path, with_input=False)
    plan = mea.plan_result_submission(run)
    assert "input.json" not in _names(plan["files"])
    assert plan["warnings"]


def test_missing_explicit_input_fails(tmp_path):
    run = _run_dir(tmp_path)
    with pytest.raises(AnalysisException):
        mea.plan_result_submission(run, input_path=tmp_path / "nope.json")


def test_missing_listed_file_fails(tmp_path):
    run = _run_dir(tmp_path)
    (run / "output" / "plot1.png").unlink()
    with pytest.raises(AnalysisException, match="missing file"):
        mea.plan_result_submission(run)


def test_post_analysis_result_sends_multipart(tmp_path):
    """The POST carries the form fields and one file part per file, by basename."""
    run = _run_dir(tmp_path)
    client = Client()
    client.base_url = "https://meorg.example/api"
    body = {"status": "success", "data": {"analysisId": "an-1", "status": "Complete", "created": True}}
    with patch("meorg_client.client.requests.post", return_value=_response(json_body=body)) as post:
        result = mea.submit_analysis_result(client, MO_ID, EXP_ID, run, orchestrator="benchcab")

    assert result["analysisId"] == "an-1"
    args, kwargs = post.call_args
    assert args[0] == f"https://meorg.example/api/modeloutput/{MO_ID}/{EXP_ID}/analysis-result"
    assert kwargs["data"]["outcome"] == "success"
    assert kwargs["data"]["externalRunId"] == "run-1"
    assert kwargs["data"]["runner"] == "gadi"
    assert json.loads(kwargs["data"]["metadata"])["orchestrator"] == "benchcab"
    parts = kwargs["files"]
    assert all(field == "file" for field, _ in parts)
    assert [spec[0] for _, spec in parts] == [
        "output.json", "plot1.png", "stats.json", "PALS.log", "run.json", "input.json",
    ]
    assert dict((spec[0], spec[1]) for _, spec in parts)["plot1.png"] == b"png1"


def _submit(client_post, tmp_path, sleeps):
    run = _run_dir(tmp_path)
    client = Client()
    client.post_analysis_result = client_post
    return mea.submit_analysis_result(
        client, MO_ID, EXP_ID, run, retries=3, backoff=1, sleep=sleeps.append
    )


OK = {"status": "success", "data": {"analysisId": "an-1", "status": "Complete", "created": False}}


def test_created_false_is_success(tmp_path):
    sleeps = []
    result = _submit(Mock(return_value=OK), tmp_path, sleeps)
    assert result["analysisId"] == "an-1"
    assert result["created"] is False
    assert sleeps == []


@pytest.mark.parametrize("status", [400, 401, 404, 409])
def test_client_errors_are_not_retried(tmp_path, status):
    post = Mock(side_effect=RequestException(status, '{"status":"error","message":"no"}'))
    sleeps = []
    with pytest.raises(AnalysisException):
        _submit(post, tmp_path, sleeps)
    assert post.call_count == 1
    assert sleeps == []


def test_server_errors_are_retried_three_times(tmp_path):
    post = Mock(side_effect=RequestException(502, "bad gateway"))
    sleeps = []
    with pytest.raises(AnalysisException, match="502"):
        _submit(post, tmp_path, sleeps)
    assert post.call_count == 4
    assert sleeps == [1, 2, 4]


def test_network_error_then_success(tmp_path):
    post = Mock(side_effect=[requests.exceptions.ConnectionError("down"), RequestException(503, ""), OK])
    sleeps = []
    result = _submit(post, tmp_path, sleeps)
    assert result["analysisId"] == "an-1"
    assert post.call_count == 3
    assert sleeps == [1, 2]


def test_submit_cli_prints_analysis_id(tmp_path, monkeypatch):
    run = _run_dir(tmp_path)
    client = Client()
    client.post_analysis_result = Mock(return_value=OK)
    monkeypatch.setattr(cli, "_get_client", lambda: client)
    result = CliRunner().invoke(
        cli.cli,
        ["analysis", "submit-result", MO_ID, EXP_ID, str(run), "--orchestrator", "benchcab"],
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.strip().splitlines()[-1] == "an-1"


def test_submit_cli_exits_non_zero_on_409(tmp_path, monkeypatch):
    run = _run_dir(tmp_path)
    client = Client()
    client.post_analysis_result = Mock(
        side_effect=RequestException(
            409,
            '{"status":"error","message":"externalRunId already holds a different terminal result"}',
        )
    )
    monkeypatch.setattr(cli, "_get_client", lambda: client)
    result = CliRunner().invoke(cli.cli, ["analysis", "submit-result", MO_ID, EXP_ID, str(run)])
    assert result.exit_code == 1
    assert "different terminal result" in result.output
    assert client.post_analysis_result.call_count == 1
