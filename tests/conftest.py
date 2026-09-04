"""Fixtures for the sidecar's own suite.

The allowlist is read once at import, so the root has to exist and be exported
BEFORE anything under ``sidecar`` is imported.  Everything the tests write
lives under that root, which makes the happy path the positive R12 evidence:
if containment were broken, every test here would 403.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pymupdf
import pytest

# Resolve: a macOS/pytest temp dir is commonly a symlink, and the service
# resolves incoming paths, so an unresolved root would fail containment.
_ROOT = Path(tempfile.mkdtemp(prefix="sidecar-tests-")).resolve()
os.environ["PDF_SIDECAR_ALLOWED_ROOTS"] = f'["{_ROOT.as_posix()}"]'
os.environ.setdefault("PDF_SIDECAR_WORKERS", "4")

from fastapi.testclient import TestClient  # noqa: E402

from sidecar.handles import clear_cache, clear_ocr_cache  # noqa: E402
from sidecar.main import app  # noqa: E402


@pytest.fixture(scope="session")
def root() -> Path:
    return _ROOT


@pytest.fixture
def client() -> TestClient:
    # Handles and OCR word lists are both process-global; a stale entry would
    # let an earlier test's document satisfy a later test's request, and a
    # stale OCR list would let a test that stubs the engine still get words.
    clear_cache()
    clear_ocr_cache()
    with TestClient(app) as c:
        yield c
    clear_cache()
    clear_ocr_cache()


def _write(doc: pymupdf.Document, path: Path) -> Path:
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture(scope="session")
def two_page(root: Path) -> Path:
    """Two text pages; page 0 carries the needle, page 1 a distinct string."""
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 96), "NEEDLE on page zero.")
    doc.new_page().insert_text((72, 96), "Second page body text.")
    return _write(doc, root / "two_page.pdf")


@pytest.fixture(scope="session")
def tier_fixture(root: Path) -> Path:
    """A fragment early and the full anchor late — the page-major regression.

    Page-major returns page 1 with the fragment; needle-major would return
    page 3 with the full anchor. The two disagree about the resulting quality
    tier, which is why this fixture exists.
    """
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 96), "Cover page.")
    doc.new_page().insert_text((72, 96), "The vessel shall maintain")
    doc.new_page().insert_text((72, 96), "Filler.")
    doc.new_page().insert_text(
        (72, 96), "The vessel shall maintain a valid certificate"
    )
    return _write(doc, root / "tier.pdf")


@pytest.fixture(scope="session")
def spans_fixture(root: Path) -> Path:
    """A word split across two spans, plus an image block.

    Both halves matter: the split word pins the no-separator join rule, and the
    image pins that page-blocks' reduced flag set excludes image blocks while
    page-data's default set includes them.
    """
    doc = pymupdf.open()
    page = doc.new_page()
    # Two insert_text calls at adjoining x produce two spans in one line.
    page.insert_text((72, 96), "Hyphen")
    page.insert_text((110, 96), "ation")
    page.insert_text((72, 130), "Second  line   with runs")
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40))
    pix.set_rect(pix.irect, (255, 0, 0))
    page.insert_image(pymupdf.Rect(400, 60, 440, 100), pixmap=pix)
    return _write(doc, root / "spans.pdf")


@pytest.fixture(scope="session")
def image_first(root: Path) -> Path:
    """An image ABOVE the text — the cohort where the two indexings diverge.

    In blocks mode the text blocks are numbered 0,1; in dict mode the image
    occupies index 0 and the same text lands at 1,2. A caller that keys one by
    the other is off by one on exactly these pages, which is a known defect
    preserved deliberately — so the payload has to expose both numberings
    faithfully rather than reconciling them.
    """
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40))
    pix.set_rect(pix.irect, (0, 0, 255))
    page.insert_image(pymupdf.Rect(72, 50, 152, 130), pixmap=pix)
    page.insert_text((72, 200), "Text below the image.")
    page.insert_text((72, 240), "Second text block.")
    return _write(doc, root / "image_first.pdf")


@pytest.fixture(scope="session")
def table_fixture(root: Path) -> Path:
    """A ruled 2x2 grid — find_tables needs lines, not just aligned text."""
    doc = pymupdf.open()
    page = doc.new_page()
    x0, y0, x1, y1 = 72, 200, 372, 300
    for y in (y0, (y0 + y1) / 2, y1):
        page.draw_line((x0, y), (x1, y))
    for x in (x0, (x0 + x1) / 2, x1):
        page.draw_line((x, y0), (x, y1))
    page.insert_text((80, 220), "A1")
    page.insert_text((230, 220), "B1")
    page.insert_text((80, 270), "A2")
    page.insert_text((230, 270), "B2")
    return _write(doc, root / "table.pdf")


@pytest.fixture(scope="session")
def ten_page(root: Path) -> Path:
    """Ten distinguishable pages, one ROTATED and one CROPPED.

    Every excerpt test runs on this one document, and the two odd pages sit at
    4 and 5 — inside the {3, 5} window those tests use. A copy that silently
    normalised page geometry would still produce three pages with the right
    headers; it could not produce three pages that RENDER like their sources.
    """
    doc = pymupdf.open()
    for index in range(10):
        page = doc.new_page(width=595, height=842)
        page.insert_textbox(
            pymupdf.Rect(50, 50, 545, 780),
            "The Company shall establish procedures for reporting "
            f"non-conformities on page {index}.",
            fontsize=14,
        )
    doc[4].set_rotation(90)
    doc[5].set_cropbox(pymupdf.Rect(30, 40, 500, 700))
    return _write(doc, root / "ten_page.pdf")


@pytest.fixture(scope="session")
def encrypted(root: Path) -> Path:
    """Opens cleanly, reports needs_pass, and fails on every page load."""
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 96), "secret")
    doc.new_page().insert_text((72, 96), "more secret")
    path = root / "encrypted.pdf"
    doc.save(
        str(path),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner",
        user_pw="user",
    )
    doc.close()
    return path


@pytest.fixture(scope="session")
def mixed_pages(root: Path) -> Path:
    """page_count == 2, page 0 readable, page 1 raises.

    Built by replacing the second /Kids entry with a /Pages node carrying an
    empty /Kids and /Count 1. The alternative shape — a non-page dictionary —
    does NOT raise: MuPDF logs and yields an empty page, which would not
    exercise the error-row path at all.
    """
    import io

    import pypdf
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 96), "NEEDLE on page zero.")
    doc.new_page().insert_text((72, 96), "Page one body.")
    source = doc.tobytes()
    doc.close()

    writer = pypdf.PdfWriter()
    writer.clone_document_from_reader(pypdf.PdfReader(io.BytesIO(source)))
    pages = writer._root_object["/Pages"].get_object()
    kids = pages["/Kids"]
    node = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Pages"),
            NameObject("/Kids"): ArrayObject(),
            NameObject("/Count"): NumberObject(1),
            NameObject("/Parent"): kids[1].get_object()["/Parent"],
        }
    )
    kids[1] = writer._add_object(node)
    path = root / "mixed_pages.pdf"
    with open(path, "wb") as fh:
        writer.write(fh)
    return path


@pytest.fixture(scope="session")
def corrupt(root: Path) -> Path:
    path = root / "corrupt.pdf"
    path.write_bytes(b"%PDF-1.7\nnot actually a pdf\n")
    return path


@pytest.fixture(scope="session")
def missing(root: Path) -> Path:
    return root / "does_not_exist.pdf"
