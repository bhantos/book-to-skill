"""Remote Docling options, kept separate from the legacy positional parser."""

from __future__ import annotations

import argparse
import os

from book_to_skill.parsers.docling_remote import RemoteDoclingOptions


def parse_docling_options(argv: list[str]) -> tuple[list[str], RemoteDoclingOptions | None]:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--docling-url")
    parser.add_argument("--docling-timeout")
    args, remaining = parser.parse_known_args(argv[1:])
    url = args.docling_url if args.docling_url is not None else os.environ.get("BOOK_TO_SKILL_DOCLING_URL")
    if url is None or (not url and args.docling_url is None):
        if args.docling_timeout is not None:
            raise ValueError("--docling-timeout requires --docling-url or BOOK_TO_SKILL_DOCLING_URL")
        return [argv[0], *remaining], None
    raw_timeout = args.docling_timeout if args.docling_timeout is not None else os.environ.get("BOOK_TO_SKILL_DOCLING_TIMEOUT", "300")
    try:
        timeout = float(raw_timeout)
    except ValueError:
        raise ValueError("Docling timeout must be a positive finite number of seconds") from None
    options = RemoteDoclingOptions(url, timeout, os.environ.get("BOOK_TO_SKILL_DOCLING_API_KEY"))
    return [argv[0], *remaining], options
