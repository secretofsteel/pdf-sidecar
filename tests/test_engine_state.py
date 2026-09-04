"""The read endpoints run in the state the pre-migration worker lived in.

Three process-wide states exist (``sidecar/engine_state.py``), and every one
of them shipped at some point:

* **baseline** ``(skipped=True, FLAGS=to_markdown's)`` — the state a real
  ``to_markdown`` leaves behind, which the pre-migration worker was in for
  nearly all of its life and the reference capture was taken in.
* **import-only** ``(True, PyMuPDF's default FLAGS)`` — what
  ``import pymupdf4llm`` leaves before any ``to_markdown``: cells lose their
  spaces and URLs their dots.  v0.1.0 on prod, 193 of 669 documents.
* **pymupdf-defaults** ``(False, PyMuPDF's default FLAGS)`` — what
  v0.1.1/v0.1.2 called the baseline: underscores are displaced to the end of
  the cell.  50 of 669 documents.

A synthetic ruled table with a built-in font reproduces all three, so the
states are pinned without a private document.  ``tests/fixtures/
mepc375_p2_table.pdf`` — one page of IMO resolution MEPC.375(80), cut from
the smallest prod document that showed the space loss — pins the real thing
too: cell (0, 5) reads ``Reason for using the`` at the baseline and
``Reason for usingthe`` in the import-only state.
"""

from __future__ import annotations

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
SPACELESS = "Reason for usingthe\npower reserve1"

URL = "http://www.data.jma.go.jp/c_1/okhotsk_anl.html"
NAME = "Vessel Name: MV Test"
QUERY = "pages_consultaDespacho?id=1"
CODE = "AMSA 226 (12/12)"

IMPORT_ONLY = (True, engine_state.PYMUPDF_DEFAULT_TABLE_FLAGS)
PYMUPDF_DEFAULTS = (False, engine_state.PYMUPDF_DEFAULT_TABLE_FLAGS)


def _set(state: tuple[bool, int]) -> None:
    pymupdf.TOOLS.unset_quad_corrections(state[0])
    pymupdf.table.FLAGS = state[1]


@pytest.fixture(scope="session")
def imo_table_page(root: Path) -> Path:
    target = root / "mepc375_p2_table.pdf"
    shutil.copyfile(FIXTURES / "mepc375_p2_table.pdf", target)
    return target


@pytest.fixture(scope="session")
def synthetic_table(root: Path) -> Path:
    """A ruled 2x2 grid whose four cells carry the strings that separate the
    three states: an underscore URL, a spaced name, an underscore query and
    a spaced code."""
    doc = pymupdf.open()
    page = doc.new_page()
    x0, y0, x1, y1 = 72, 200, 540, 300
    for y in (y0, (y0 + y1) / 2, y1):
        page.draw_line((x0, y), (x1, y))
    for x in (x0, (x0 + x1) / 2, x1):
        page.draw_line((x, y0), (x, y1))
    page.insert_text((80, 230), URL, fontname="helv", fontsize=9)
    page.insert_text((320, 230), NAME, fontname="helv", fontsize=9)
    page.insert_text((80, 280), QUERY, fontname="helv", fontsize=9)
    page.insert_text((320, 280), CODE, fontname="helv", fontsize=9)
    target = root / "three_states.pdf"
    doc.save(str(target))
    doc.close()
    return target


@pytest.fixture(autouse=True)
def _leave_the_process_at_the_baseline():
    """Whatever a test does to the globals, the next one starts clean."""
    yield
    engine_state.restore_baseline()


def _cells(client, path: Path) -> list[str]:
    row = client.post(
        "/doc/page-data",
        json={"path": str(path), "pages": [0], "parts": ["tables"]},
    ).json()[0]
    assert row["tables"], "the page carries one ruled table"
    return [c for table in row["tables"] for r in table["rows"] for c in r if c]


def _markdown_body(path: Path, root: Path) -> dict:
    return {
        "path": str(path),
        "write_images": False,
        "image_path": str(root),
        "image_format": "png",
        "dpi": 72,
        "image_size_limit": 0.05,
        "force_text": True,
        "table_strategy": "lines_strict",
        "page_separators": False,
    }


# ------------------------------------------------------------- the baseline


def test_a_real_to_markdown_leaves_the_process_at_the_baseline(tmp_path):
    """The constants are derived; prove them against the real thing in an
    interpreter that runs one to_markdown and nothing else.  A pymupdf4llm
    bump that changes what to_markdown sets fails here first."""
    pdf = tmp_path / "one.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 96), "one page")
    doc.save(str(pdf))
    doc.close()
    code = (
        "import os; os.environ['PYMUPDF_SUGGEST_LAYOUT_ANALYZER'] = '0'\n"
        "import pymupdf, pymupdf.table, pymupdf4llm\n"
        f"pymupdf4llm.to_markdown({str(pdf)!r})\n"
        "live = (bool(pymupdf.TOOLS.unset_quad_corrections()), int(pymupdf.table.FLAGS))\n"
        "import sidecar.engine_state as s\n"
        "assert live == (s.QUAD_CORRECTIONS_SKIPPED, s.TABLE_FLAGS), (live, s.QUAD_CORRECTIONS_SKIPPED, s.TABLE_FLAGS)\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(REPO), capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


def test_pymupdfs_own_defaults_are_not_the_baseline():
    """An interpreter that never imported pymupdf4llm sits at PyMuPDF's
    defaults — the state v0.1.1/v0.1.2 wrongly called the baseline."""
    code = (
        "import pymupdf, pymupdf.table, sys\n"
        "live = (bool(pymupdf.TOOLS.unset_quad_corrections()), int(pymupdf.table.FLAGS))\n"
        "import sidecar.engine_state as s\n"
        "assert 'pymupdf4llm' not in sys.modules\n"
        "assert live == (False, s.PYMUPDF_DEFAULT_TABLE_FLAGS), (live, s.PYMUPDF_DEFAULT_TABLE_FLAGS)\n"
        "assert live != (s.QUAD_CORRECTIONS_SKIPPED, s.TABLE_FLAGS)\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(REPO), capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_importing_the_service_leaves_the_process_at_the_baseline(client):
    """`client` has imported sidecar.main, and with it pymupdf4llm — whose
    import alone would leave the import-only state."""
    assert "pymupdf4llm" in sys.modules
    assert engine_state.at_baseline(), engine_state.snapshot()


# ------------------------------------------------ the three states, named


def test_the_baseline_keeps_spaces_dots_and_underscores(client, synthetic_table):
    assert _cells(client, synthetic_table) == [URL, NAME, QUERY, CODE]


def test_the_import_only_state_loses_spaces_and_dots(client, synthetic_table):
    """v0.1.0 on prod: `Vessel Name:` -> `VesselName:`."""
    _set(IMPORT_ONLY)
    cells = _cells(client, synthetic_table)
    assert "VesselName:MVTest" in cells, cells
    assert not any(c == URL for c in cells), cells


def test_pymupdfs_defaults_displace_underscores(client, synthetic_table):
    """v0.1.1/v0.1.2: `c_1/okhotsk_anl` -> `c 1/okhotsk anl` with the
    underscores pushed to the end of the cell."""
    _set(PYMUPDF_DEFAULTS)
    cells = _cells(client, synthetic_table)
    assert NAME in cells, "spaces survive at PyMuPDF's defaults"
    assert not any(c == URL for c in cells), cells
    assert any(c.startswith("http://www.data.jma.go.jp/c 1/okhotsk anl.html") for c in cells), cells


# ---------------------------------------------------- the prod page, both ways


def test_the_prod_page_keeps_its_cell_spaces_at_the_baseline(client, imo_table_page):
    row = client.post(
        "/doc/page-data",
        json={"path": str(imo_table_page), "pages": [0], "parts": ["tables"]},
    ).json()[0]
    assert row["tables"][0]["rows"][0][5] == CLEAN


def test_the_same_page_loses_them_in_the_import_only_state(client, imo_table_page):
    """Negative control on the real page: without it the positive test above
    could pass on an insensitive page."""
    _set(IMPORT_ONLY)
    row = client.post(
        "/doc/page-data",
        json={"path": str(imo_table_page), "pages": [0], "parts": ["tables"]},
    ).json()[0]
    assert row["tables"][0]["rows"][0][5] == SPACELESS


# --------------------------------------------------------- around markdown


def test_to_markdown_is_called_at_the_baseline_and_the_globals_are_restored(
    client, imo_table_page, root, monkeypatch
):
    seen: dict[str, object] = {}

    def spy(path, **kwargs):
        seen["state"] = engine_state.snapshot()
        # Something a future pymupdf4llm might do: leave the globals elsewhere.
        pymupdf.TOOLS.unset_quad_corrections(False)
        pymupdf.table.FLAGS = 0
        return "# spy"

    monkeypatch.setattr(pymupdf4llm, "to_markdown", spy)
    response = client.post("/doc/to-markdown", json=_markdown_body(imo_table_page, root))
    assert response.status_code == 200, response.text
    assert seen["state"] == (engine_state.QUAD_CORRECTIONS_SKIPPED, engine_state.TABLE_FLAGS)
    assert engine_state.at_baseline(), engine_state.snapshot()


def test_to_markdown_restores_the_baseline_when_it_raises(
    client, imo_table_page, root, monkeypatch
):
    def boom(path, **kwargs):
        pymupdf.table.FLAGS = 0
        raise RuntimeError("engine fault")

    monkeypatch.setattr(pymupdf4llm, "to_markdown", boom)
    response = client.post("/doc/to-markdown", json=_markdown_body(imo_table_page, root))
    assert response.status_code == 400, response.text
    assert engine_state.at_baseline(), engine_state.snapshot()


def test_tables_after_a_real_markdown_request_are_unchanged(
    client, synthetic_table, root
):
    """The prod sequence end to end: a worker serves markdown, then tables."""
    response = client.post("/doc/to-markdown", json=_markdown_body(synthetic_table, root))
    assert response.status_code == 200, response.text
    assert response.json()["markdown"]
    assert engine_state.at_baseline(), engine_state.snapshot()
    assert _cells(client, synthetic_table) == [URL, NAME, QUERY, CODE]
