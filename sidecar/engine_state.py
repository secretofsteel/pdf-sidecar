"""The two process-wide PyMuPDF settings that pymupdf4llm mutates.

``import pymupdf4llm`` runs ``pymupdf.TOOLS.unset_quad_corrections(True)`` at
module level — three times over, in ``helpers/pymupdf_rag.py``,
``helpers/multi_column.py`` and ``helpers/document_layout.py`` (0.2.9) — and
``to_markdown`` reassigns ``pymupdf.table.FLAGS`` every time it is called
(``pymupdf_rag.py``, its ``textflags`` block).  Both are process globals, and
both change what ``find_tables().extract()`` returns for the same page.

The read endpoints port in-process code that never imported pymupdf4llm, so
their acceptance test is PyMuPDF's own defaults.  ``main.py`` imports
pymupdf4llm at startup for ``/doc/to-markdown``, which is how every worker on
prod (2026-09-03) extracted tables with quad corrections skipped until it had
served one markdown request: 193 of 669 documents lost the spaces inside table
cells (``Vessel Name:`` became ``VesselName:``), every changed line a table
row.  The dev corpus of 139 documents had no such page, so the dev gate read
139/139 — byte-stability read as correctness.

Two rules, both enforced here and pinned by ``tests/test_engine_state.py``:

* Every engine call that is not ``to_markdown`` runs at the baseline.
  ``restore_baseline()`` is called once at import, after pymupdf4llm has been
  imported, and again after every ``to_markdown`` call.
* ``to_markdown`` runs in the state its own import established (quad
  corrections skipped) and may do what it likes to ``table.FLAGS``; the
  ``finally`` puts both back.  Under ``FITZ_LOCK`` this is race-free within a
  worker, because no other engine call can interleave.
"""

from __future__ import annotations

import contextlib
from typing import Iterator

import pymupdf
import pymupdf.table

# PyMuPDF's own defaults, DERIVED rather than read at import: reading the live
# values would freeze whatever state the process happened to be in, and this
# module could be imported after a to_markdown call has already run.  The
# expression is pymupdf/table.py's own (1.28.2); a PyMuPDF bump that changes
# it is a parity event, and the fresh-interpreter test says so.
QUAD_CORRECTIONS_SKIPPED = False
TABLE_FLAGS = (
    0
    | pymupdf.TEXTFLAGS_TEXT
    | pymupdf.TEXT_COLLECT_STYLES
    | pymupdf.TEXT_ACCURATE_BBOXES
    | pymupdf.TEXT_MEDIABOX_CLIP
)


def snapshot() -> tuple[bool, int]:
    """The live pair: (quad corrections skipped?, table.FLAGS)."""
    return bool(pymupdf.TOOLS.unset_quad_corrections()), int(pymupdf.table.FLAGS)


def at_baseline() -> bool:
    return snapshot() == (QUAD_CORRECTIONS_SKIPPED, TABLE_FLAGS)


def restore_baseline() -> None:
    """Put the process back where an interpreter that never imported
    pymupdf4llm would be."""
    pymupdf.TOOLS.unset_quad_corrections(QUAD_CORRECTIONS_SKIPPED)
    pymupdf.table.FLAGS = TABLE_FLAGS


@contextlib.contextmanager
def markdown_state() -> Iterator[None]:
    """The state pymupdf4llm's import established, restored on the way out.

    Enter with FITZ_LOCK held.  ``to_markdown`` sets ``table.FLAGS`` itself;
    nothing here anticipates it — the exit restores both regardless of what
    the call did, including when it raised.
    """
    pymupdf.TOOLS.unset_quad_corrections(True)
    try:
        yield
    finally:
        restore_baseline()
