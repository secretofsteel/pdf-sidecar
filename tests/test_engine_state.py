"""The read endpoints run at PyMuPDF's own defaults, whatever pymupdf4llm did.

Found on prod 2026-09-03 (the story is in ``sidecar/engine_state.py``): with
pymupdf4llm imported at startup, every worker extracted tables with quad
corrections skipped until it had served one ``/doc/to-markdown`` request, and
193 of 669 documents lost the spaces inside their table cells.

``tests/fixtures/mepc375_p2_table.pdf`` is one page of IMO resolution
MEPC.375(80), cut from the smallest prod document that showed the defect and
re-checked on its own.  Its table cell (0, 5) reads ``Reason for using the``
at the baseline and ``Reason for usingthe`` with quad corrections skipped —
so a green run of the positive test means something only because the
negative control below proves the page is sensitive.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

import pymupdf
import pymupdf.table
import pymupdf4llm
import pytest

from sidecar import engine_state

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parent.parent

CLEAN = "Reason for using the\npower reserve1"
TAINTED = "Reason for usingthe\npower reserve1"


@pytest.fixture(scope="session")
def imo_table_page(root: Path) -> Path:
    target = root / "mepc375_p2_table.pdf"
    shutil.copyfile(FIXTURES / "mepc375_p2_table.pdf", target)
    return target


@pytest.fixture(autouse=True)
def _leave_the_process_at_the_baseline():
    """Whatever a test does to the globals, the next one starts clean."""
    yield
    engine_state.restore_baseline()


def _cell_0_5(client, path: Path) -> str:
    row = client.post(
        "/doc/page-data",
        json={"path": str(path), "pages": [0], "parts": ["tables"]},
    ).json()[0]
    assert row["tables"], "the fixture page carries one ruled table"
    return row["tables"][0]["rows"][0][5]


# ------------------------------------------------------------- the baseline


def test_the_baseline_constants_are_pymupdfs_own_defaults():
    """Derived, not read — so prove the derivation in an interpreter that has
    never imported pymupdf4llm.  A PyMuPDF bump that changes either value
    changes what every read endpoint returns, and must fail here first."""
    code = (
        "import pymupdf, pymupdf.table\n"
        "live = (bool(pymupdf.TOOLS.unset_quad_corrections()), int(pymupdf.table.FLAGS))\n"
        "import sidecar.engine_state as s\n"
        "assert live == (s.QUAD_CORRECTIONS_SKIPPED, s.TABLE_FLAGS), (live, s.QUAD_CORRECTIONS_SKIPPED, s.TABLE_FLAGS)\n"
        "assert 'pymupdf4llm' not in __import__('sys').modules\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(REPO), capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_importing_the_service_leaves_the_process_at_the_baseline(client):
    """`client` has imported sidecar.main, and with it pymupdf4llm."""
    assert "pymupdf4llm" in sys.modules
    assert engine_state.at_baseline(), engine_state.snapshot()


def test_pymupdf4llm_still_flips_the_flag_at_import():
    """The hazard this module exists for, pinned in the pinned version.

    If this fails after a pymupdf4llm bump, the import-time flip may be gone
    from ``helpers/pymupdf_rag.py`` — check ``multi_column.py`` and
    ``document_layout.py`` too before concluding anything, and keep the
    restore either way: ``to_markdown`` still mutates ``table.FLAGS``.
    """
    pymupdf.TOOLS.unset_quad_corrections(False)
    rag = importlib.import_module("pymupdf4llm.helpers.pymupdf_rag")
    importlib.reload(rag)  # re-runs the module-level call
    assert pymupdf.TOOLS.unset_quad_corrections() is True


# ------------------------------------------------- the prod page, both ways


def test_the_prod_page_keeps_its_cell_spaces_at_the_baseline(client, imo_table_page):
    assert _cell_0_5(client, imo_table_page) == CLEAN


def test_the_same_page_loses_them_with_quad_corrections_skipped(client, imo_table_page):
    """Negative control: the state pymupdf4llm's import leaves behind, applied
    by hand, reproduces prod's defect on this page through the real endpoint.
    Without this the positive test above could pass on an insensitive page."""
    pymupdf.TOOLS.unset_quad_corrections(True)
    assert _cell_0_5(client, imo_table_page) == TAINTED


# --------------------------------------------------------- around markdown


def test_to_markdown_runs_with_quad_corrections_skipped_and_restores_both(
    client, imo_table_page, root, monkeypatch
):
    seen: dict[str, object] = {}

    def spy(path, **kwargs):
        seen["state"] = engine_state.snapshot()
        # What the real call does at call time (pymupdf_rag.py's textflags block).
        pymupdf.table.FLAGS = engine_state.TABLE_FLAGS | pymupdf.TEXT_COLLECT_VECTORS
        return "# spy"

    monkeypatch.setattr(pymupdf4llm, "to_markdown", spy)
    response = client.post(
        "/doc/to-markdown",
        json={
            "path": str(imo_table_page),
            "write_images": False,
            "image_path": str(root),
            "image_format": "png",
            "dpi": 72,
            "image_size_limit": 0.05,
            "force_text": True,
            "table_strategy": "lines_strict",
            "page_separators": False,
        },
    )
    assert response.status_code == 200, response.text
    assert seen["state"] == (True, engine_state.TABLE_FLAGS), seen
    assert engine_state.at_baseline(), engine_state.snapshot()


def test_to_markdown_restores_the_baseline_when_it_raises(
    client, imo_table_page, root, monkeypatch
):
    def boom(path, **kwargs):
        pymupdf.table.FLAGS = 0
        raise RuntimeError("engine fault")

    monkeypatch.setattr(pymupdf4llm, "to_markdown", boom)
    response = client.post(
        "/doc/to-markdown",
        json={
            "path": str(imo_table_page),
            "write_images": False,
            "image_path": str(root),
            "image_format": "png",
            "dpi": 72,
            "image_size_limit": 0.05,
            "force_text": True,
            "table_strategy": "lines_strict",
            "page_separators": False,
        },
    )
    assert response.status_code == 400, response.text
    assert engine_state.at_baseline(), engine_state.snapshot()


def test_tables_after_a_real_markdown_request_still_carry_spaces(
    client, imo_table_page, root
):
    """The prod sequence end to end: a worker serves markdown, then tables."""
    response = client.post(
        "/doc/to-markdown",
        json={
            "path": str(imo_table_page),
            "write_images": False,
            "image_path": str(root),
            "image_format": "png",
            "dpi": 72,
            "image_size_limit": 0.05,
            "force_text": True,
            "table_strategy": "lines_strict",
            "page_separators": False,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["markdown"]
    assert engine_state.at_baseline(), engine_state.snapshot()
    assert _cell_0_5(client, imo_table_page) == CLEAN
