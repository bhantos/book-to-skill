"""Remote Docling options, kept separate from the legacy positional parser."""

from __future__ import annotations

import argparse
import os

from book_to_skill.parsers.docling_remote import RemoteDoclingOptions


def parse_docling_options(argv: list[str]) -> tuple[list[str], RemoteDoclingOptions | None]:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--docling-url")
    parser.add_argument("--docling-timeout")
    parser.add_argument("--docling-async", action=argparse.BooleanOptionalAction, default=None)
    args, remaining = parser.parse_known_args(argv[1:])
    url = args.docling_url if args.docling_url is not None else os.environ.get("BOOK_TO_SKILL_DOCLING_URL")
    if url is None or (not url and args.docling_url is None):
        if args.docling_timeout is not None or args.docling_async is True:
            raise ValueError("Remote Docling options require --docling-url or BOOK_TO_SKILL_DOCLING_URL")
        return [argv[0], *remaining], None
    raw_timeout = args.docling_timeout if args.docling_timeout is not None else os.environ.get("BOOK_TO_SKILL_DOCLING_TIMEOUT", "300")
    try:
        timeout = float(raw_timeout)
    except ValueError:
        raise ValueError("Docling timeout must be a positive finite number of seconds") from None
    use_async = args.docling_async
    if use_async is None:
        raw_async = os.environ.get("BOOK_TO_SKILL_DOCLING_ASYNC", "false").lower()
        if raw_async not in {"true", "false", "1", "0"}:
            raise ValueError("BOOK_TO_SKILL_DOCLING_ASYNC must be true, false, 1, or 0")
        use_async = raw_async in {"true", "1"}
    options = RemoteDoclingOptions(url, timeout, os.environ.get("BOOK_TO_SKILL_DOCLING_API_KEY"), use_async)
    return [argv[0], *remaining], options
