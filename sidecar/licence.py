"""R5 — the licence canary.

PyMuPDF's optional `pymupdf-layout` package (Polyform-Noncommercial / Artifex)
is NOT licensed for this project.  Importing `pymupdf.layout` — directly, or
indirectly via `pymupdf4llm.layout`, which is a one-line re-export of it —
sets `pymupdf._get_layout` process-wide and import-order-sensitively, which
silently rebinds `pymupdf4llm.to_markdown` to the commercial parser.

So the flag is checked twice: once at startup, where a positive result refuses
to boot, and again immediately before every `to_markdown` call, because the
hazard can be introduced after startup by any import anywhere in the process.
"""

from __future__ import annotations

import importlib.util
import logging

import pymupdf

LOGGER = logging.getLogger("sidecar.licence")


class LayoutLicenceError(RuntimeError):
    """The commercial PyMuPDF-Layout parser is reachable in this process."""


def layout_canary() -> str | None:
    """The LIVE value of ``pymupdf._get_layout``, re-read on every call.

    ``None`` means the free legacy parser is bound.  Anything else means the
    commercial parser has been pulled in and this process must not serve
    ``/doc/to-markdown``.
    """
    value = getattr(pymupdf, "_get_layout", None)
    return None if value is None else repr(value)


def assert_free_layout() -> None:
    """Refuse to start unless all three legs are clean.

    1. ``pymupdf._get_layout is None``      — the real flag.  Note that
       ``fitz._get_layout`` never existed; a canary written against it is
       vacuous, which is how this went unnoticed for months.
    2. ``pymupdf.layout`` is not importable — the commercial package installs
       itself *into* the pymupdf namespace.
    3. ``pymupdf_layout`` is not importable — its distribution name.

    ``pymupdf4llm.layout`` is deliberately NOT a fourth leg: it ships with
    pymupdf4llm 0.2.9 on every clean install, so asserting its absence would
    refuse to boot always.  Its `__init__.py` is the single line
    `import pymupdf.layout`, so leg 2 already covers what it would do — and it
    can never be tested by importing it, because that import IS the hazard.
    It is guarded instead by the source grep in tests/test_licence.py.
    """
    failures: list[str] = []

    if pymupdf._get_layout is not None:
        failures.append(
            f"pymupdf._get_layout is {pymupdf._get_layout!r}, expected None — "
            "the commercial PyMuPDF-Layout parser is bound in this process"
        )
    for module in ("pymupdf.layout", "pymupdf_layout"):
        try:
            spec = importlib.util.find_spec(module)
        except (ImportError, ValueError):  # a parent package that cannot load
            spec = None
        if spec is not None:
            failures.append(
                f"{module} is importable — uninstall pymupdf-layout from this venv"
            )

    if failures:
        raise LayoutLicenceError(
            "refusing to start: no PyMuPDF-Layout licence is held. "
            + "; ".join(failures)
        )

    LOGGER.info("licence canary clean: free legacy parser bound")
