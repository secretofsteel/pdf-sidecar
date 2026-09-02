"""Process configuration: the path allowlist (R12) and the worker count.

Read once at import.  The allowlist is the service's only security boundary
beyond the loopback bind, so it is deliberately strict and deliberately loud
about being empty.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

LOGGER = logging.getLogger("sidecar.config")

# Dev convenience: prod passes the same keys through systemd's EnvironmentFile,
# where this call finds nothing and changes nothing.
load_dotenv()


def _load_allowed_roots() -> list[Path]:
    """Parse PDF_SIDECAR_ALLOWED_ROOTS — a JSON array of absolute paths.

    JSON rather than a separator-joined string because a Windows dev root
    (``C:/dev/...``) would split on ``:``.

    A missing or malformed value yields an EMPTY allowlist plus one ERROR line,
    and the service still starts (Q-L).  Every request then 403s until it is
    fixed, which is a loud, self-diagnosing failure — as against refusing to
    boot, which on three `Restart=always` units composes into a crash loop.
    """
    raw = os.getenv("PDF_SIDECAR_ALLOWED_ROOTS")
    if not raw:
        LOGGER.error(
            "PDF_SIDECAR_ALLOWED_ROOTS is unset — the allowlist is EMPTY and "
            "every request will be refused with 403 until it is set"
        )
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        LOGGER.error(
            "PDF_SIDECAR_ALLOWED_ROOTS is not valid JSON (%s) — the allowlist "
            "is EMPTY and every request will be refused with 403", exc,
        )
        return []
    if not isinstance(parsed, list) or not all(isinstance(r, str) for r in parsed):
        LOGGER.error(
            "PDF_SIDECAR_ALLOWED_ROOTS must be a JSON array of strings, got %r "
            "— the allowlist is EMPTY and every request will be refused with 403",
            parsed,
        )
        return []

    roots: list[Path] = []
    for entry in parsed:
        root = Path(entry)
        if not root.is_absolute():
            LOGGER.error("allowlist root %r is not absolute — ignored", entry)
            continue
        # Resolve on THIS side too: the app resolves its own paths, and a
        # symlinked root (a pytest basetemp, commonly) would otherwise fail
        # containment against an unresolved root.
        roots.append(root.resolve())
    if not roots:
        LOGGER.error("allowlist resolved to no usable roots — every request 403s")
    else:
        LOGGER.info("allowlist: %s", ", ".join(str(r) for r in roots))
    return roots


ALLOWED_ROOTS: list[Path] = _load_allowed_roots()

# Reported by /health so the deploy gate can assert the app's roots are covered.
WORKERS: int = int(os.getenv("PDF_SIDECAR_WORKERS", "4"))
