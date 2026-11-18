"""Lone UTF-16 surrogates in MuPDF's character output never break a JSON answer.

The shape is a real one: a plan whose formula is set in Cambria Math maps each
mathematical letter to its surrogate pair split over two glyphs (``U+D835`` on
one, ``U+DCxx`` on the next), and on page 84 also a ``U+D835`` with no partner.
MuPDF's own text modes put ``U+FFFD`` there; ``find_tables`` cell text and
``pymupdf4llm`` markdown, built from the characters, kept the surrogates, and
``/doc/page-data`` (tables) and ``/doc/to-markdown`` answered 500.

The fixture is synthetic: Helvetica with a ToUnicode CMap that sends ``A`` to
``<D835>`` and ``B`` to ``<DC75>``, written inside a ruled 2 x 2 table and in a
plain line. No tenant file is involved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest

from sidecar.utf8 import REPLACEMENT, utf8_safe

HIGH, LOW = chr(0xD835), chr(0xDC75)

_CMAP = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<00> <FF>
endcodespacerange
2 beginbfchar
<41> <D835>
<42> <DC75>
endbfchar
endcmap
CMapName currentdict /CMap defineresource pop
end
end
"""


def _has_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(c) <= 0xDFFF for c in text)


@pytest.fixture(scope="session")
def surrogate_pdf(root: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 80), "Formula AB and A alone", fontname="helv")
    # A ruled 2 x 2 table, so find_tables finds it with the "lines" strategy.
    x0, y0, w, h = 72, 120, 200, 30
    for i in range(3):
        page.draw_line((x0, y0 + i * h), (x0 + 2 * w, y0 + i * h))
        page.draw_line((x0 + i * w, y0), (x0 + i * w, y0 + 2 * h))
    for r, row in enumerate([["Item", "Value"], ["Mass AB", "x A y"]]):
        for c, cell in enumerate(row):
            page.insert_text((x0 + c * w + 6, y0 + r * h + 20), cell, fontname="helv")
    xref = page.get_fonts()[0][0]
    cmap = doc.get_new_xref()
    doc.update_object(cmap, "<<>>")
    doc.update_stream(cmap, _CMAP)
    doc.xref_set_key(xref, "ToUnicode", f"{cmap} 0 R")
    path = root / "lone_surrogates.pdf"
    doc.save(str(path))
    doc.close()
    return path


def test_the_fixture_has_the_real_shape(surrogate_pdf):
    """Precondition: MuPDF's characters and table cells carry the surrogates, its text modes do not."""
    page = pymupdf.open(str(surrogate_pdf))[0]
    chars = [
        ch["c"]
        for b in page.get_text("rawdict")["blocks"]
        for line in b.get("lines", [])
        for s in line["spans"]
        for ch in s["chars"]
    ]
    assert HIGH in chars and LOW in chars
    cells = [c for t in page.find_tables().tables for row in t.extract() for c in row if c]
    assert any(_has_surrogate(c) for c in cells)
    assert not _has_surrogate(page.get_text("text"))
    assert REPLACEMENT in page.get_text("text")


def _ok_and_clean(response):
    assert response.status_code == 200, response.text[:300]
    raw = response.content.decode("utf-8")  # strict: an unencodable answer cannot get here
    assert not _has_surrogate(json.loads(raw).__repr__())
    return json.loads(raw)


def test_page_data_tables_answers_with_the_replacement_character(client, surrogate_pdf):
    rows = _ok_and_clean(
        client.post(
            "/doc/page-data",
            json={"path": str(surrogate_pdf), "pages": [0],
                  "parts": ["text", "blocks", "dict_blocks", "tables"]},
        )
    )
    row = rows[0]
    assert row["errors"] == {}
    cells = json.dumps(row["tables"], ensure_ascii=False)
    assert REPLACEMENT in cells
    assert not _has_surrogate(cells)
    # The cell text agrees with MuPDF's own text mode: each code unit is one U+FFFD.
    assert "Mass " + REPLACEMENT * 2 in cells
    assert "Mass " + REPLACEMENT * 2 in row["text"]


def test_to_markdown_answers_with_the_replacement_character(client, surrogate_pdf, root):
    body = {
        "path": str(surrogate_pdf), "write_images": False, "image_path": str(root),
        "image_format": "png", "dpi": 200, "image_size_limit": 0.05, "force_text": True,
        "table_strategy": "lines", "page_separators": False,
    }
    md = _ok_and_clean(client.post("/doc/to-markdown", json=body))["markdown"]
    assert REPLACEMENT in md and not _has_surrogate(md)


@pytest.mark.parametrize(
    "endpoint,extra",
    [
        ("/doc/text-pages", {"pages": [0]}),
        ("/doc/page-words", {"page": 0}),
        ("/doc/page-blocks", {"page": 0}),
        ("/doc/search", {"needles": ["Formula"], "page_order": [0]}),
    ],
)
def test_every_other_text_endpoint_answers_clean(client, surrogate_pdf, endpoint, extra):
    _ok_and_clean(client.post(endpoint, json={"path": str(surrogate_pdf), **extra}))


def test_utf8_safe_replaces_each_surrogate_unit_and_nothing_else():
    assert utf8_safe("plain") == "plain"
    assert utf8_safe("é ∑ " + chr(0x1D475)) == "é ∑ " + chr(0x1D475)   # a real astral letter stays
    assert utf8_safe("a" + HIGH + "b") == "a" + REPLACEMENT + "b"
    assert utf8_safe(HIGH + LOW) == REPLACEMENT * 2                     # as MuPDF's text mode does
    assert utf8_safe({"k": [("x" + LOW, 1.5, None)], "n": 3}) == {"k": [["x" + REPLACEMENT, 1.5, None]], "n": 3}
