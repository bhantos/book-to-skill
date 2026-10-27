"""Exercise the CLI through mocked HTTP responses; no Docling server required."""

import base64
import io
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from book_to_skill import cli, pdf_inspector_integration, utils
from book_to_skill.parsers import docling_remote as remote


BASE_URL = "https://docling.example/proxy"
API_KEY = "mock-api-key"
MARKDOWN = "# Chapter 1\n\nSynthetic\u200b text.\n\n| Item | Count |\n| --- | --- |\n| Alpha | 3 |\n| Beta | 7 |"


def response(value):
    return io.BytesIO(json.dumps(value).encode())


@pytest.fixture
def mocked_cli(tmp_path, monkeypatch):
    pdf = tmp_path / "synthetic.pdf"
    pdf.write_bytes(b"%PDF-1.4\nsynthetic fixture")
    output = tmp_path / "output"
    for suffix in ("URL", "TIMEOUT", "API_KEY", "ASYNC"):
        monkeypatch.delenv("BOOK_TO_SKILL_DOCLING_" + suffix, raising=False)
    monkeypatch.setenv("BOOK_TO_SKILL_DOCLING_API_KEY", API_KEY)
    monkeypatch.setattr(utils, "OUTPUT_DIR", output)
    monkeypatch.setattr(utils, "OUTPUT_TEXT", output / "full_text.txt")
    monkeypatch.setattr(utils, "OUTPUT_META", output / "metadata.json")
    monkeypatch.setattr(utils, "looks_image_only", lambda *_: False)
    monkeypatch.setattr(utils, "count_pages", lambda *_: 1)
    # Restore the extractor when cli.main installs its wrapper; isolate metadata.
    monkeypatch.setattr(utils, "extract_single_file", utils.extract_single_file)
    monkeypatch.setattr(pdf_inspector_integration, "_INSPECTIONS", {})

    def forbidden(*_args):
        pytest.fail("Remote mode must not install or run a local PDF extractor")

    for name in ("prepare_dependencies", "extract_with_docling", "extract_with_pdftotext",
                 "extract_with_pypdf", "extract_with_pdfminer"):
        monkeypatch.setattr(utils, name, forbidden)
    opener = Mock()
    monkeypatch.setattr(remote, "build_opener", Mock(return_value=opener))
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(remote, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))

    def run(use_async):
        argv = ["extract.py", str(pdf), "--mode", "technical", "--docling-url", BASE_URL,
                "--docling-timeout", "10", "--install-missing", "yes"]
        if use_async:
            argv.append("--docling-async")
        monkeypatch.setattr(sys, "argv", argv)
        cli.main()
        return json.loads((output / "metadata.json").read_text(encoding="utf-8"))

    return run, opener, pdf, output


@pytest.mark.parametrize("use_async, transient_error", [
    pytest.param(False, False, id="sync"),
    pytest.param(True, False, id="async"),
    pytest.param(True, True, id="async-poll-503"),
])
def test_mocked_cli_preserves_tables_and_records_remote_backend(mocked_cli, capsys, use_async, transient_error):
    run, opener, pdf, output = mocked_cli
    result = {"status": "success", "document": {"md_content": MARKDOWN}}
    paths = ["/v1/convert/source"]
    replies = [response(result)]
    if use_async:
        paths = ["/v1/convert/source/async", "/v1/status/poll/mock-task",
                 "/v1/status/poll/mock-task", "/v1/result/mock-task"]
        replies = [response({"task_id": "mock-task", "task_status": status})
                   for status in ("pending", "started", "success")] + [response(result)]
        if transient_error:
            paths.insert(1, "/v1/status/poll/mock-task")
            replies.insert(1, HTTPError(BASE_URL + paths[1], 503, "busy", {}, io.BytesIO()))
    opener.open.side_effect = replies
    metadata = run(use_async)
    calls = opener.open.call_args_list
    assert [call.args[0].full_url for call in calls] == [BASE_URL + path for path in paths]
    assert [call.args[0].method for call in calls] == ["POST"] + ["GET"] * (len(paths) - 1)
    assert all(call.args[0].get_header("X-api-key") == API_KEY for call in calls)
    payload = json.loads(calls[0].args[0].data)
    assert base64.b64decode(payload["sources"][0]["base64_string"]) == pdf.read_bytes()
    assert payload["options"]["do_table_structure"] is True
    text = (output / "full_text.txt").read_text(encoding="utf-8")
    assert MARKDOWN.replace("\u200b", "") in text
    assert metadata["extraction_method"] == "docling_remote"
    assert metadata["sources"][0]["fallback_reason"] is None
    captured = capsys.readouterr()
    assert API_KEY not in captured.out + captured.err + json.dumps(metadata)


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_mocked_auth_failure_records_fallback_without_leaking_credentials(mocked_cli, monkeypatch, capsys, use_async):
    run, opener, _, output = mocked_cli
    opener.open.side_effect = HTTPError(
        BASE_URL, 401, "unauthorized", {}, io.BytesIO(API_KEY.encode()),
    )
    monkeypatch.setattr(utils, "extract_with_pdftotext", lambda *_: "Installed text fallback")
    metadata = run(use_async)
    assert opener.open.call_count == 1
    assert opener.open.call_args.args[0].get_header("X-api-key") == API_KEY
    assert metadata["extraction_method"] == "pdftotext"
    assert metadata["sources"][0]["fallback_reason"] == "docling_remote_failed"
    assert "Installed text fallback" in (output / "full_text.txt").read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert "HTTP 401" in captured.err
    assert API_KEY not in captured.out + captured.err + json.dumps(metadata)
