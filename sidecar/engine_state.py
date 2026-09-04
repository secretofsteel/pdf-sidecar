"""The two process-wide PyMuPDF settings pymupdf4llm mutates, and the state
every read endpoint runs in.

``import pymupdf4llm`` runs ``pymupdf.TOOLS.unset_quad_corrections(True)`` at
module level — three times over, in ``helpers/pymupdf_rag.py``,
``helpers/multi_column.py`` and ``helpers/document_layout.py`` (0.2.9) — and
``to_markdown`` reassigns ``pymupdf.table.FLAGS`` on every call to
``TEXTFLAGS_TEXT | TEXT_COLLECT_STYLES | TEXT_MEDIABOX_CLIP`` (dropping
``TEXT_ACCURATE_BBOXES`` from PyMuPDF's default).  Both are process globals,
and both change what ``find_tables().extract()`` returns for the same page.

**Which state is "today's call"?**  The pre-migration code ran in one
long-lived worker process that imported pymupdf4llm the first time it served
a markdown upload and never changed back, so for nearly all of its life the
library extractor ran at (skipped=True, FLAGS=to_markdown's).  The
pre-migration parity capture (669 documents, 2026-09-03) was taken the same
way — document 1 at PyMuPDF's defaults, documents 2–669 and every search and
render after its own markdown leg's import.  That state is the baseline here.
It is also the better output on every page probed (2026-09-04): spaces
between words kept, underscores in URLs kept inline, a merged cell that the
defaults lose.

The two other states were both shipped, briefly, and are both wrong:

* ``(True, PyMuPDF's default FLAGS)`` — what ``import pymupdf4llm`` leaves
  behind before any ``to_markdown``: cells lose their spaces and URLs their
  dots (``VesselName:``, ``tramitesprefecturanavalgobar``).  v0.1.0 on prod:
  193 of 669 documents.
* ``(False, PyMuPDF's default FLAGS)`` — PyMuPDF's own defaults, which
  v0.1.1/v0.1.2 called the baseline: spaces kept, but underscores displaced
  to the end of the cell (``c 1/okhotsk anl … _``) and a merged cell lost;
  50 of 669 documents differed from the reference.

Rules, pinned by ``tests/test_engine_state.py``:

* ``restore_baseline()`` runs once at import, after pymupdf4llm has been
  imported, and again after every ``to_markdown`` — which sets exactly this
  state itself, so the restore is a guard against a future pymupdf4llm
  changing what it sets, and against anything else touching the globals.
* The constants are derived from the same PyMuPDF flags pymupdf4llm uses, and
  a fresh-interpreter test proves a real ``to_markdown`` leaves the process
  exactly here.
"""

from __future__ import annotations

import contextlib
from typing import Iterator

import pymupdf
import pymupdf.table

# The state a pymupdf4llm 0.2.9 to_markdown call leaves behind.
QUAD_CORRECTIONS_SKIPPED = True
TABLE_FLAGS = (
    0
    | pymupdf.TEXTFLAGS_TEXT
    | pymupdf.TEXT_COLLECT_STYLES
    | pymupdf.TEXT_MEDIABOX_CLIP
)  # pymupdf4llm/helpers/pymupdf_rag.py, to_markdown's textflags block

# PyMuPDF's own default (pymupdf/table.py, 1.28.2).  NOT the baseline; kept
# so the tests can name the two wrong states precisely.
PYMUPDF_DEFAULT_TABLE_FLAGS = TABLE_FLAGS | pymupdf.TEXT_ACCURATE_BBOXES


def snapshot() -> tuple[bool, int]:
    """The live pair: (quad corrections skipped?, table.FLAGS)."""
    return bool(pymupdf.TOOLS.unset_quad_corrections()), int(pymupdf.table.FLAGS)


def at_baseline() -> bool:
    return snapshot() == (QUAD_CORRECTIONS_SKIPPED, TABLE_FLAGS)


def restore_baseline() -> None:
    """Put the process where the pre-migration worker spent its life."""
    pymupdf.TOOLS.unset_quad_corrections(QUAD_CORRECTIONS_SKIPPED)
    pymupdf.table.FLAGS = TABLE_FLAGS


@contextlib.contextmanager
def markdown_state() -> Iterator[None]:
    """Around ``to_markdown``: it may do what it likes to the globals; the
    exit restores the baseline regardless, including when the call raised.
    Enter with FITZ_LOCK held."""
    try:
        yield
    finally:
        restore_baseline()
