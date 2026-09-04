"""/doc/page-words-ocr, the startup capability probe, and the word cache.

The OCR tests skip by name when this environment has no working Tesseract —
the probe answers that, and a skip that does not say WHY is how a suite
quietly stops testing the thing it is named after. Run them for real with
`TESSDATA_PREFIX` pointing AT a directory holding `eng.traineddata`.
"""

from __future__ import annotations

import os

import pymupdf
import pytest

from sidecar.handles import clear_ocr_cache, ocr_cache_info
from sidecar.main import OCR_AVAILABLE

requires_tessdata = pytest.mark.skipif(
    not OCR_AVAILABLE,
    reason=(
        "no working Tesseract in this environment: the 1x1 startup probe "
        "failed and TESSDATA_PREFIX is "
        f"{os.environ.get('TESSDATA_PREFIX') or 'unset'}. Re-run with it "
        "pointing AT a directory holding eng.traineddata."
    ),
)

# Three well-separated single lines: the visual line count is then a fact of
# the fixture rather than a guess about how a textbox wrapped.
SCAN_LINES = (
    "Before any fuel transfer commences the Chief Engineer",
    "shall verify that all scuppers are plugged and that",
    "the SOPEP equipment is staged on deck.",
)


@pytest.fixture(scope="session")
def pure_scan(root):
    """Text RENDERED TO AN IMAGE, on a page with an EMPTY glyph layer.

    Not a grey rectangle and not a dual-layer scan: this is the cohort the
    endpoint exists for, and it is the only fixture shape that can prove the
    words came from Tesseract — a page with glyphs would answer the same way
    through `/doc/page-words`, which is not a test of anything.
    """
    typeset = pymupdf.open()
    page = typeset.new_page(width=595, height=842)
    for index, line in enumerate(SCAN_LINES):
        page.insert_text((60, 120 + index * 40), line, fontsize=16)
    pixmap = typeset[0].get_pixmap(dpi=200)

    scan = pymupdf.open()
    scanned_page = scan.new_page(
        width=typeset[0].rect.width, height=typeset[0].rect.height
    )
    scanned_page.insert_image(scanned_page.rect, pixmap=pixmap)
    path = root / "pure_scan.pdf"
    scan.save(str(path))
    scan.close()
    typeset.close()
    return path


def _ocr(client, path, page=0, **overrides):
    body = {"path": str(path), "page": page, "dpi": 300, "language": "eng"}
    body.update(overrides)
    return client.post("/doc/page-words-ocr", json=body)


def test_the_fixture_really_has_no_glyphs(client, pure_scan):
    """Rule 5's guard: a fixture that still carries glyphs proves nothing.

    If this ever starts returning words, every assertion below could be
    satisfied by the glyph layer and the endpoint would be untested.
    """
    words = client.post(
        "/doc/page-words", json={"path": str(pure_scan), "page": 0}
    ).json()["words"]
    assert words == []


@requires_tessdata
def test_ocr_words_are_page_words_rows_from_a_glyphless_page(client, pure_scan):
    """T-B1 — the shape, the geometry, and the block structure.

    One OCR block per visual line is the structural fact the caller's line
    grouping depends on: it unions word rects per (block, line) to build the
    stacked bars a highlight is drawn from, so three lines that came back as
    one block would render as one bar across the whole paragraph.
    """
    response = _ocr(client, pure_scan)
    assert response.status_code == 200
    words = response.json()["words"]
    assert words, "a page of rendered text must yield words"

    for row in words:
        assert len(row) == 8
        assert all(isinstance(value, float) for value in row[:4])
        assert isinstance(row[4], str)
        assert all(isinstance(value, int) for value in row[5:])

    # Recognisable, not merely present.
    recovered = " ".join(row[4] for row in words)
    assert "scuppers" in recovered
    assert "SOPEP" in recovered

    # Rects in MuPDF's own coordinate space, inside the page.
    page_rect = pymupdf.open(str(pure_scan))[0].rect
    for row in words:
        assert page_rect.x0 - 1 <= row[0] and row[2] <= page_rect.x1 + 1
        assert page_rect.y0 - 1 <= row[1] and row[3] <= page_rect.y1 + 1

    assert len({row[5] for row in words}) == len(SCAN_LINES)
    assert len({(row[5], row[6]) for row in words}) == len(SCAN_LINES)


@requires_tessdata
def test_the_word_cache_hits_on_the_second_identical_call(client, pure_scan):
    """T-B9 / R12(b) — both calls in ONE test, so the hit is the assertion.

    Split across two tests this would only prove that a cache exists
    somewhere; the point is that the second ask inside one worker does not
    pay the 0.5-3.5 s again.
    """
    clear_ocr_cache()
    first = _ocr(client, pure_scan)
    after_first = ocr_cache_info()
    second = _ocr(client, pure_scan)
    after_second = ocr_cache_info()

    assert first.json() == second.json()
    assert (after_first.hits, after_first.misses) == (0, 1)
    assert (after_second.hits, after_second.misses) == (1, 1)

    # A different page is a different key — a cache that answered everything
    # from one entry would also report a hit.
    _ocr(client, pure_scan, dpi=150)
    assert ocr_cache_info().misses == 2


@requires_tessdata
def test_a_blank_page_is_two_hundred_and_no_words(client, root):
    """The fault posture's quiet case: nothing to read is not a failure."""
    doc = pymupdf.open()
    doc.new_page()
    path = root / "blank_for_ocr.pdf"
    doc.save(str(path))
    doc.close()

    response = _ocr(client, path)
    assert response.status_code == 200
    assert response.json()["words"] == []


def test_a_page_that_will_not_load_is_a_document_fault(client, mixed_pages):
    """400 — the caller degrades on this exactly as it does on any bad page."""
    response = _ocr(client, mixed_pages, page=1)
    assert response.status_code == 400
    assert response.json()["error"] == "document"


def test_an_engine_failure_is_five_hundred_and_never_four_hundred(
    client, pure_scan, monkeypatch
):
    """The line the whole fault posture turns on.

    A Tesseract that cannot run means the SERVICE is misconfigured, and the
    caller's rule is to degrade quietly on 400. If a missing traineddata file
    came back as 400 the app would silently stop OCR-ing every document and
    nothing anywhere would say so.
    """
    from fastapi.testclient import TestClient

    import sidecar.main as main

    clear_ocr_cache()  # a cached list would answer before the stub is reached

    def boom(self, *args, **kwargs):
        raise RuntimeError("tesseract failed")

    monkeypatch.setattr(pymupdf.Page, "get_textpage_ocr", boom)
    with TestClient(main.app, raise_server_exceptions=False) as unwrapped:
        response = _ocr(unwrapped, pure_scan)
    assert response.status_code == 500
    assert response.json()["error"] == "internal"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"dpi": 71}, id="dpi-below-the-floor"),
        pytest.param({"dpi": 601}, id="dpi-above-the-ceiling"),
        pytest.param({"language": "deu"}, id="unshipped-language"),
        pytest.param({"page": -1}, id="negative-page"),
    ],
)
def test_out_of_contract_requests_are_refused_before_the_engine(
    client, pure_scan, overrides
):
    """1200 dpi on an A4 page is a 1.75 GB pixmap, and a language whose file is
    absent is indistinguishable from a mistyped one — both are refused here."""
    response = _ocr(client, pure_scan, **overrides)
    assert response.status_code == 422
    assert response.json()["error"] == "contract"


def test_health_reports_the_ocr_capability(client):
    body = client.get("/health").json()
    assert isinstance(body["ocr"], bool)
    assert body["ocr"] is OCR_AVAILABLE


@requires_tessdata
def test_health_reports_ocr_true_where_tesseract_actually_works(client):
    """The positive half: a field that is only ever observed false is not
    evidence that the probe can succeed."""
    assert client.get("/health").json()["ocr"] is True
