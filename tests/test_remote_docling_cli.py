import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from book_to_skill import utils
from book_to_skill.dependencies import run_dependency_check
from book_to_skill.docling_options import parse_docling_options
from book_to_skill.parsers.docling_remote import RemoteDoclingError, RemoteDoclingOptions


@pytest.fixture(autouse=True)
def clean_remote_environment(monkeypatch):
    for suffix in ("URL", "TIMEOUT", "API_KEY"):
        monkeypatch.delenv("BOOK_TO_SKILL_DOCLING_" + suffix, raising=False)


def test_local_default_leaves_legacy_arguments_unchanged():
    argv = ["extract.py", "a.pdf", "notes.txt", "--mode", "technical"]
    remaining, options = parse_docling_options(argv)
    assert remaining == argv
    assert options is None


def test_cli_overrides_environment_and_preserves_input_paths(monkeypatch):
    monkeypatch.setenv("BOOK_TO_SKILL_DOCLING_URL", "https://env.example")
    monkeypatch.setenv("BOOK_TO_SKILL_DOCLING_TIMEOUT", "99")
    monkeypatch.setenv("BOOK_TO_SKILL_DOCLING_API_KEY", "secret")
    argv, options = parse_docling_options([
        "extract.py", "a.pdf", "--docling-url", "https://cli.example",
        "b.pdf", "--mode", "technical", "--docling-timeout=42",
    ])
    assert utils.parse_arguments(argv) == (["a.pdf", "b.pdf"], "technical", "ask")
    assert options == RemoteDoclingOptions("https://cli.example", 42, "secret")


def test_environment_configuration(monkeypatch):
    monkeypatch.setenv("BOOK_TO_SKILL_DOCLING_URL", "http://localhost:5001")
    monkeypatch.setenv("BOOK_TO_SKILL_DOCLING_TIMEOUT", "12.5")
    _, options = parse_docling_options(["extract.py", "a.pdf"])
    assert options == RemoteDoclingOptions("http://localhost:5001", 12.5)


@pytest.mark.parametrize("args", [
    ["--docling-url", ""], ["--docling-url", "file:///tmp/service"],
    ["--docling-timeout", "10"],
    *[["--docling-url", "https://host", "--docling-timeout", value]
      for value in ("0", "-1", "nan", "inf", "nope")],
])
def test_bad_configuration_fails_before_extraction(args):
    with pytest.raises(ValueError):
        parse_docling_options(["extract.py", *args, "a.pdf"])


@pytest.mark.parametrize("flag", ["--docling-url", "--docling-timeout"])
def test_missing_option_value(flag):
    with pytest.raises(SystemExit) as exc:
        parse_docling_options(["extract.py", "a.pdf", flag])
    assert exc.value.code == 2


@pytest.fixture
def pdf(tmp_path, monkeypatch):
    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF-1.4\nsynthetic")
    monkeypatch.setattr(utils, "looks_image_only", lambda *_: False)
    monkeypatch.setattr(utils, "count_pages", lambda *_: 1)
    return path


def test_remote_skips_local_dependency_installation(pdf, monkeypatch):
    def forbidden(*_args):
        pytest.fail("Remote extraction must not install or run local Docling")

    monkeypatch.setattr(utils, "prepare_dependencies", forbidden)
    monkeypatch.setattr(utils, "extract_with_docling", forbidden)
    monkeypatch.setattr(utils, "extract_with_remote_docling", lambda *_: "# Synthetic\n\nText")
    result = utils.extract_single_file(pdf, "technical", "yes", RemoteDoclingOptions("https://host"))
    assert result["extraction_method"] == "docling_remote"
    assert result["fallback_reason"] is None


def test_remote_failure_reports_text_fallback(pdf, monkeypatch, capsys):
    def fail(*_args):
        raise RemoteDoclingError("Remote Docling returned HTTP 503")

    monkeypatch.setattr(utils, "extract_with_remote_docling", fail)
    monkeypatch.setattr(utils, "extract_with_pdftotext", lambda *_: "Fallback text")
    result = utils.extract_single_file(pdf, "technical", "no", RemoteDoclingOptions("https://host"))
    assert result["extraction_method"] == "pdftotext"
    assert result["fallback_reason"] == "docling_remote_failed"
    assert "Remote Docling failed" in capsys.readouterr().err


def test_text_mode_never_uploads(pdf, monkeypatch):
    def forbidden(*_args):
        pytest.fail("Text extraction must not upload the document")

    monkeypatch.setattr(utils, "extract_with_remote_docling", forbidden)
    monkeypatch.setattr(utils, "prepare_dependencies", lambda *_: None)
    monkeypatch.setattr(utils, "extract_with_pdftotext", lambda *_: "Local text")
    result = utils.extract_single_file(pdf, "text", "no", RemoteDoclingOptions("https://host"))
    assert result["extraction_method"] == "pdftotext"


def test_remote_check_does_not_recommend_installing_docling(monkeypatch, capsys):
    from book_to_skill import dependencies

    monkeypatch.setattr(dependencies, "python_module_available", lambda *_: False)
    run_dependency_check(remote_docling=True)
    out = capsys.readouterr().out
    assert "remote configured (not contacted)" in out
    assert "python: docling" not in out
    assert " docling" not in out.split("-m pip install", 1)[-1]


def test_remote_failure_without_local_fallback_fails_the_source(pdf, monkeypatch):
    from book_to_skill.exceptions import ExtractionError

    def fail(*_args):
        raise RemoteDoclingError("Remote Docling connection failed or timed out")

    monkeypatch.setattr(utils, "extract_with_remote_docling", fail)
    for name in ("extract_with_pdftotext", "extract_with_pypdf", "extract_with_pdfminer"):
        monkeypatch.setattr(utils, name, lambda *_: None)
    with pytest.raises(ExtractionError, match="Could not extract text from PDF"):
        utils.extract_single_file(pdf, "technical", "no", RemoteDoclingOptions("https://host"))


def test_other_formats_remain_local_with_remote_options(tmp_path, monkeypatch):
    path = tmp_path / "notes.txt"
    path.write_text("Synthetic local notes")

    def forbidden(*_args):
        pytest.fail("Other formats must not upload documents")

    monkeypatch.setattr(utils, "extract_with_remote_docling", forbidden)
    result = utils.extract_single_file(path, "technical", "no", RemoteDoclingOptions("https://host"))
    assert result["extraction_method"] == "plain-text"


def test_pdf_inspector_forwards_remote_options(pdf, monkeypatch):
    from types import SimpleNamespace
    from book_to_skill import pdf_inspector_integration as integration

    calls = []
    options = RemoteDoclingOptions("https://host")

    def original(*args):
        calls.append(args)
        return {"extraction_method": "docling_remote"}

    def forbidden(*_args):
        pytest.fail("Explicit remote technical extraction should bypass local inspection")

    monkeypatch.setattr(integration, "inspect_pdf", forbidden)
    fake_utils = SimpleNamespace(extract_single_file=original)
    integration.install_pdf_inspector_hook(fake_utils)
    assert fake_utils.extract_single_file(pdf, "technical", "no", options)["extraction_method"] == "docling_remote"
    assert calls == [(pdf, "technical", "no", options)]


def test_cli_smoke_uploads_without_local_docling_and_sanitizes(tmp_path):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.headers["X-Api-Key"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "success",
                "document": {"md_content": "# Chapter 1\n\nSynthetic\u200b text.\n\n| A | B |\n| --- | --- |\n| 1 | 2 |"},
            }).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    pdf = tmp_path / "synthetic.pdf"
    pdf.write_bytes(b"%PDF-1.4\nsynthetic")
    output = tmp_path / "output"
    env = dict(os.environ, BOOK_SKILL_WORKDIR=str(output), BOOK_TO_SKILL_DOCLING_API_KEY="test-secret")
    try:
        result = subprocess.run([
            sys.executable, "scripts/extract.py", str(pdf), "--mode", "technical",
            "--docling-url", f"http://127.0.0.1:{server.server_port}", "--install-missing", "yes",
        ], cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(received) == 1
        assert received[0][0:2] == ("/v1/convert/source", "test-secret")
        assert received[0][2]["sources"][0]["filename"] == "synthetic.pdf"
        text = (output / "full_text.txt").read_text()
        assert "| A | B |" in text and "\u200b" not in text
        metadata_text = (output / "metadata.json").read_text()
        metadata = json.loads(metadata_text)
        assert metadata["extraction_method"] == "docling_remote"
        assert metadata["sources"][0]["fallback_reason"] is None
        assert "test-secret" not in result.stdout + result.stderr + metadata_text
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
