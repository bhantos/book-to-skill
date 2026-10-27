# Security Policy

## Scope

book-to-skill uses local extraction by default. It reads document files you point
it at and writes skill files to your skills directory. Explicitly configuring
`--docling-url` or `BOOK_TO_SKILL_DOCLING_URL` uploads technical PDFs to that
docling-serve server. Only configure a server you trust with the documents;
use HTTPS for remote connections. The client verifies certificates and refuses
redirects. It does not phone home or run a network service. The main security surface is:

- the Python extraction code (parsing untrusted document files), and
- the optional remote Docling connection (document uploads and returned Markdown), and
- the optional dependencies it can install on request (`pip install …` when you
  choose `--install-missing yes`).

## Supported versions

The latest released `1.x` version receives fixes. Please reproduce issues against
the most recent tag before reporting.

## Reporting a vulnerability

Please **do not** open a public issue for a security problem. Instead use GitHub's
private vulnerability reporting:

- Go to the repository's **Security** tab → **Report a vulnerability**.

Include: affected version, a minimal reproduction (ideally a small sample file or
crafted input), and the impact you observed. We aim to acknowledge within a few days.

## Good practices for users

- Run `python3 scripts/extract.py --check` to see exactly which extractors are in
  use; install dependencies yourself if you prefer to control what is added.
- Only convert documents you trust and have the right to process (see the README's
  Copyright & fair-use section).
