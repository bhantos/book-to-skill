"""Dependency-free client for the docling-serve v1 conversion API."""

from __future__ import annotations

import base64
import json
import math
import re
import time
from dataclasses import dataclass, field
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


@dataclass(frozen=True)
class RemoteDoclingOptions:
    base_url: str
    timeout: float = 300
    api_key: str | None = field(default=None, repr=False)
    use_async: bool = False

    def __post_init__(self):
        try:
            url = urlsplit(self.base_url)
            valid = (
                url.scheme in {"http", "https"} and url.hostname and url.port != 0
                and not url.username and not url.password
                and not url.query and not url.fragment
                and not any(c.isspace() or ord(c) < 32 for c in self.base_url)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Docling URL must be an HTTP(S) base URL without credentials, query, or fragment")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Docling timeout must be a positive finite number of seconds")
        if self.api_key and any(ord(c) < 32 or ord(c) > 126 for c in self.api_key):
            raise ValueError("Docling API key must contain only printable ASCII characters")


class RemoteDoclingError(RuntimeError):
    """A safe diagnostic: never includes response bodies or credentials."""


class _TransientRemoteDoclingError(RemoteDoclingError):
    """A polling GET may be retried without submitting another conversion."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward document contents or API keys to another endpoint.
        return None


def _request_json(path, options, payload=None, timeout=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if options.api_key:
        headers["X-Api-Key"] = options.api_key
    request = Request(
        options.base_url.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers=headers, method="POST" if payload is not None else "GET",
    )
    try:
        with build_opener(_NoRedirects()).open(request, timeout=timeout or options.timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        code = exc.code
        exc.close()
        error = _TransientRemoteDoclingError if code in {429, 502, 503, 504} else RemoteDoclingError
        raise error(f"Remote Docling returned HTTP {code}") from None
    except (URLError, OSError, HTTPException):
        raise _TransientRemoteDoclingError("Remote Docling connection failed or timed out") from None
    except (ValueError, UnicodeError):
        raise RemoteDoclingError("Remote Docling returned invalid JSON") from None


def _async_result(payload, options):
    deadline = time.monotonic() + options.timeout

    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise RemoteDoclingError("Remote Docling async conversion timed out")
        return budget

    def request(path, data=None):
        result = _request_json(path, options, data, min(30, remaining()))
        remaining()
        return result

    # Never retry submission: the server may already have accepted the PDF.
    task = request("/v1/convert/source/async", payload)
    task_id = task.get("task_id") if isinstance(task, dict) else None
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,255}", task_id):
        raise RemoteDoclingError("Remote Docling returned an invalid task identifier")
    while True:
        status = task.get("task_status") if isinstance(task, dict) else None
        if status == "success":
            return request(f"/v1/result/{task_id}")
        if not isinstance(status, str) or status not in {"pending", "started"}:
            raise RemoteDoclingError("Remote Docling async task failed or returned an invalid status")
        time.sleep(min(1, remaining()))
        try:
            polled = request(f"/v1/status/poll/{task_id}")
        except _TransientRemoteDoclingError:
            # Polling is read-only. Result retrieval may be single-use and is
            # deliberately not retried, nor are authentication/schema errors.
            continue
        if not isinstance(polled, dict) or polled.get("task_id") != task_id:
            raise RemoteDoclingError("Remote Docling returned inconsistent task status")
        task = polled


def extract_with_remote_docling(pdf_path: str, options: RemoteDoclingOptions) -> str:
    """Upload one PDF and return Markdown, matching the local Docling options."""
    path = Path(pdf_path)
    payload = {
        "sources": [{
            "kind": "file", "filename": path.name,
            "base64_string": base64.b64encode(path.read_bytes()).decode("ascii"),
        }],
        "options": {
            "to_formats": ["md"], "do_ocr": False,
            "do_table_structure": True, "image_export_mode": "placeholder",
        },
    }
    result = (
        _async_result(payload, options) if options.use_async
        else _request_json("/v1/convert/source", options, payload)
    )
    if not isinstance(result, dict) or result.get("status") != "success":
        raise RemoteDoclingError("Remote Docling conversion did not complete successfully")
    document = result.get("document")
    markdown = document.get("md_content") if isinstance(document, dict) else None
    if not isinstance(markdown, str) or not markdown.strip():
        raise RemoteDoclingError("Remote Docling returned no Markdown")
    return markdown
