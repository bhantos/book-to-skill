from types import SimpleNamespace

import pytest

from book_to_skill.docling_options import parse_docling_options
from book_to_skill.parsers import docling_remote as remote


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(remote, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
    return now, sleeps


def task(status="pending", task_id="test-task"):
    return {"task_id": task_id, "task_status": status}


def run_sequence(tmp_path, monkeypatch, responses, timeout=10):
    calls = []
    sequence = iter(responses)

    def request(path, options, payload=None, timeout=None):
        calls.append((path, payload, timeout))
        response = next(sequence)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(remote, "_request_json", request)
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"synthetic PDF")
    options = remote.RemoteDoclingOptions("https://host/prefix", timeout=timeout, use_async=True)
    return lambda: remote.extract_with_remote_docling(str(pdf), options), calls


def test_queued_started_success_then_markdown(tmp_path, monkeypatch, clock):
    run, calls = run_sequence(tmp_path, monkeypatch, [
        task(), task("started"), task("success"),
        {"status": "success", "document": {"md_content": "# Async book"}},
    ])
    assert run() == "# Async book"
    assert [c[0] for c in calls] == [
        "/v1/convert/source/async", "/v1/status/poll/test-task",
        "/v1/status/poll/test-task", "/v1/result/test-task",
    ]
    assert calls[0][1]["sources"][0]["kind"] == "file"
    assert all(c[1] is None for c in calls[1:])
    assert [c[2] for c in calls] == [10, 9, 8, 8]


@pytest.mark.parametrize("response, message", [
    ([], "invalid task identifier"),
    (task(task_id="../../other"), "invalid task identifier"),
    (task(task_id="https://other"), "invalid task identifier"),
    (task(task_id=""), "invalid task identifier"),
    (task("failure"), "task failed"),
    (task("cancelled"), "task failed"),
    (task([]), "invalid status"),
])
def test_invalid_or_failed_submission_stops(tmp_path, monkeypatch, clock, response, message):
    run, calls = run_sequence(tmp_path, monkeypatch, [response])
    with pytest.raises(remote.RemoteDoclingError, match=message):
        run()
    assert len(calls) == 1


@pytest.mark.parametrize("response", [task("success", "other-task"), [], {}])
def test_malformed_or_mismatched_poll_stops(tmp_path, monkeypatch, clock, response):
    run, calls = run_sequence(tmp_path, monkeypatch, [task(), response])
    with pytest.raises(remote.RemoteDoclingError, match="inconsistent task status"):
        run()
    assert len(calls) == 2


def test_timeout_budget_is_shared_across_polls(tmp_path, monkeypatch, clock):
    run, calls = run_sequence(tmp_path, monkeypatch, [task(), task(), task()], timeout=2.5)
    with pytest.raises(remote.RemoteDoclingError, match="async conversion timed out"):
        run()
    assert len(calls) == 3
    assert clock[1] == [1, 1, 0.5]
    assert all("result" not in c[0] for c in calls)


def test_poll_connection_failures_retry_without_resubmitting(tmp_path, monkeypatch, clock):
    run, calls = run_sequence(tmp_path, monkeypatch, [
        task(), remote._TransientRemoteDoclingError("connection failed"),
        task("success"), {"status": "success", "document": {"md_content": "Text"}},
    ])
    assert run() == "Text"
    assert sum(c[1] is not None for c in calls) == 1


@pytest.mark.parametrize("responses", [
    [remote._TransientRemoteDoclingError("submission timeout")],
    [task(), remote.RemoteDoclingError("HTTP 401")],
    [task("success"), remote._TransientRemoteDoclingError("result timeout")],
    [task("success"), {"status": "partial_success", "document": {"md_content": "Incomplete"}}],
])
def test_submission_result_and_permanent_errors_are_not_retried(tmp_path, monkeypatch, clock, responses):
    run, calls = run_sequence(tmp_path, monkeypatch, responses)
    with pytest.raises(remote.RemoteDoclingError):
        run()
    assert len(calls) == len(responses)


def test_async_environment_and_cli_precedence(monkeypatch):
    monkeypatch.setenv("BOOK_TO_SKILL_DOCLING_URL", "https://host")
    monkeypatch.setenv("BOOK_TO_SKILL_DOCLING_ASYNC", "true")
    _, options = parse_docling_options(["extract.py", "book.pdf"])
    assert options.use_async
    _, options = parse_docling_options(["extract.py", "book.pdf", "--no-docling-async"])
    assert not options.use_async
    monkeypatch.setenv("BOOK_TO_SKILL_DOCLING_ASYNC", "invalid")
    with pytest.raises(ValueError, match="ASYNC"):
        parse_docling_options(["extract.py", "book.pdf"])
    _, options = parse_docling_options(["extract.py", "book.pdf", "--docling-async"])
    assert options.use_async


def test_async_flag_requires_url(monkeypatch):
    monkeypatch.delenv("BOOK_TO_SKILL_DOCLING_URL", raising=False)
    with pytest.raises(ValueError, match="require"):
        parse_docling_options(["extract.py", "book.pdf", "--docling-async"])
