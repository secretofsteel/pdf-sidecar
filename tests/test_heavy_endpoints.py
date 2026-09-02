"""/doc/page-data, /doc/render, /doc/to-markdown, /doc/annotate."""

from __future__ import annotations

import io

import pymupdf
import pytest

ALL_PARTS = [
    "text",
    "blocks",
    "dict_blocks",
    "tables",
    "clusters",
    "drawings",
    "images",
    "scan",
]


def _page_data(client, path, pages, parts=ALL_PARTS):
    return client.post(
        "/doc/page-data",
        json={"path": str(path), "pages": pages, "parts": parts},
    )


# ---------------------------------------------------------- /doc/page-data


def test_page_data_returns_a_bare_list_one_row_per_page(client, two_page):
    """Deliberately not the {"rows": ...} envelope the cite endpoints use.

    The cite endpoints were re-wrapped when they gained per-page error rows;
    the ingestion payload was left alone so its shape stays comparable against
    the in-process output it replaces.
    """
    body = _page_data(client, two_page, [0, 1]).json()
    assert isinstance(body, list)
    assert [row["page"] for row in body] == [0, 1]
    assert all(len(row["page_rect"]) == 4 for row in body)


def test_page_data_blocks_are_seven_element_rows(client, two_page):
    """Element 5 keys a text-repair lookup downstream.

    A 5-tuple payload would pass every type check and silently disable that
    repair, so the arity is asserted rather than assumed.
    """
    row = _page_data(client, two_page, [0], ["blocks"]).json()[0]
    assert row["blocks"]
    for block in row["blocks"]:
        assert len(block) == 7
        assert all(isinstance(v, float) for v in block[:4])
        assert isinstance(block[4], str)
        assert isinstance(block[5], int) and isinstance(block[6], int)


def test_page_data_blocks_match_the_engine(client, two_page):
    row = _page_data(client, two_page, [0], ["blocks"]).json()[0]
    doc = pymupdf.open(str(two_page))
    try:
        assert row["blocks"] == [list(b) for b in doc[0].get_text("blocks")]
    finally:
        doc.close()


def test_dict_blocks_index_enumerates_images_too(client, spans_fixture):
    """The index is the raw enumeration over ALL dict blocks.

    Its consumer keys a lookup off it and has a known off-by-one on
    image-before-text pages. Preserving the raw enumeration preserves today's
    output, which is what the parity test compares; fixing it belongs upstream.
    """
    row = _page_data(client, spans_fixture, [0], ["dict_blocks"]).json()[0]
    blocks = row["dict_blocks"]
    assert [b["index"] for b in blocks] == list(range(len(blocks)))
    assert any(b["type"] == 1 for b in blocks), "image block must survive default flags"


def test_dict_blocks_never_serialise_image_bytes(client, spans_fixture):
    row = _page_data(client, spans_fixture, [0], ["dict_blocks"]).json()[0]
    for block in row["dict_blocks"]:
        assert set(block) == {"index", "type", "bbox", "lines"}


def test_dict_blocks_line_text_has_no_separator(client, spans_fixture):
    row = _page_data(client, spans_fixture, [0], ["dict_blocks"]).json()[0]
    joined = "".join(
        line["text"] for block in row["dict_blocks"] for line in block["lines"]
    )
    assert "Hyphenation" in joined


def test_page_data_tables_carry_bbox_and_extracted_rows(client, table_fixture):
    row = _page_data(client, table_fixture, [0], ["tables"]).json()[0]
    assert row["tables"], "ruled grid should be detected"
    table = row["tables"][0]
    assert len(table["bbox"]) == 4
    assert table["rows"] and isinstance(table["rows"][0], list)
    # cells was struck from the contract — its only consumer was a fold that
    # cannot be reached on this engine.
    assert "cells" not in table


def test_page_data_scan_part_is_raw_counts(client, two_page):
    """Counts, never a ratio, and never the verdict.

    Two of the caller's gates are integer comparisons on these counts;
    reconstructing them from a ratio disagrees at the boundaries.
    """
    row = _page_data(client, two_page, [0], ["scan"]).json()[0]
    scan = row["scan"]
    assert set(scan) == {"total_chars", "invisible_chars", "max_image_area_ratio"}
    assert isinstance(scan["total_chars"], int)
    assert isinstance(scan["invisible_chars"], int)
    assert scan["total_chars"] > 0
    assert "is_scanned" not in row and "verdict" not in row


def test_page_data_scan_ratio_is_a_fraction_of_page_area(client, spans_fixture):
    row = _page_data(client, spans_fixture, [0], ["scan"]).json()[0]
    ratio = row["scan"]["max_image_area_ratio"]
    assert 0.0 < ratio < 1.0


def test_page_data_only_returns_requested_parts(client, two_page):
    row = _page_data(client, two_page, [0], ["text"]).json()[0]
    assert "text" in row
    assert "blocks" not in row and "tables" not in row
    # page and page_rect are unconditional.
    assert "page" in row and "page_rect" in row


def test_page_data_page_fault_nulls_every_part(client, mixed_pages):
    """Fault rule (b): the caller turns this into a whole-document fallback."""
    body = _page_data(client, mixed_pages, [0, 1], ["text", "blocks"]).json()
    good, bad = body[0], body[1]
    assert good["errors"] == {} and good["text"]
    assert "page" in bad["errors"]
    assert bad["text"] is None and bad["blocks"] is None


def test_page_data_clean_page_has_an_empty_errors_map(client, two_page):
    row = _page_data(client, two_page, [0]).json()[0]
    assert row["errors"] == {}


def test_page_data_rejects_more_than_the_window(client, two_page):
    assert _page_data(client, two_page, list(range(33)), ["text"]).status_code == 422


def test_page_data_rejects_an_unknown_part(client, two_page):
    assert _page_data(client, two_page, [0], ["nonsense"]).status_code == 422


# -------------------------------------------------------------- /doc/render


@pytest.mark.parametrize(
    "extra",
    [
        {"matrix": [2, 0, 0, 2, 0, 0]},                       # embedded-image crop
        {"dpi": 200},                                          # table transcription
        {"dpi": 220},                                          # formula region
        {"matrix": [2, 0, 0, 2, 0, 0], "clip": [72, 60, 300, 160]},
    ],
)
def test_render_serves_each_live_call_shape(client, two_page, extra):
    response = client.post(
        "/doc/render", json={"path": str(two_page), "page": 0, **extra}
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_render_pixels_match_the_engine(client, two_page):
    response = client.post(
        "/doc/render", json={"path": str(two_page), "page": 0, "dpi": 200}
    )
    doc = pymupdf.open(str(two_page))
    try:
        expected = doc[0].get_pixmap(dpi=200)
    finally:
        doc.close()
    served = pymupdf.Pixmap(io.BytesIO(response.content))
    assert (served.width, served.height) == (expected.width, expected.height)
    assert served.samples == expected.samples


@pytest.mark.parametrize(
    "extra", [{}, {"matrix": [2, 0, 0, 2, 0, 0], "dpi": 200}]
)
def test_render_requires_exactly_one_of_matrix_and_dpi(client, two_page, extra):
    """get_pixmap accepts both and lets dpi win silently.

    Without this rule a caller sending both would get a rendering it did not
    ask for, with nothing anywhere reporting a problem.
    """
    response = client.post(
        "/doc/render", json={"path": str(two_page), "page": 0, **extra}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "contract"


def test_render_page_fault_is_a_document_fault(client, mixed_pages):
    assert (
        client.post(
            "/doc/render", json={"path": str(mixed_pages), "page": 1, "dpi": 200}
        ).status_code
        == 400
    )


# --------------------------------------------------------- /doc/to-markdown


def _md_body(path, image_path, **overrides):
    body = {
        "path": str(path),
        "write_images": False,
        "image_path": str(image_path),
        "image_format": "png",
        "dpi": 200,
        "image_size_limit": 0.05,
        "force_text": True,
        "table_strategy": "lines",
        "page_separators": False,
    }
    body.update(overrides)
    return body


def test_to_markdown_returns_markdown(client, two_page, root):
    body = client.post(
        "/doc/to-markdown", json=_md_body(two_page, root)
    ).json()
    assert "NEEDLE" in body["markdown"]


def test_to_markdown_rejects_an_unknown_kwarg(client, two_page, root):
    """The engine swallows unknown kwargs with a stdout warning.

    It then returns silently different markdown, so refusing here is the only
    way a caller's typo becomes visible.
    """
    payload = _md_body(two_page, root)
    payload["bogus_kwarg"] = True
    assert client.post("/doc/to-markdown", json=payload).status_code == 422


def test_to_markdown_rejects_a_missing_kwarg(client, two_page, root):
    payload = _md_body(two_page, root)
    del payload["table_strategy"]
    assert client.post("/doc/to-markdown", json=payload).status_code == 422


def test_to_markdown_writes_images_into_the_given_directory(client, spans_fixture, root):
    out = root / "md_images"
    out.mkdir(exist_ok=True)
    client.post(
        "/doc/to-markdown",
        json=_md_body(spans_fixture, out, write_images=True, image_size_limit=0.0),
    )
    written = list(out.glob("*.png"))
    assert written, "write_images=True must emit PNGs into image_path"
    # The stem convention the caller parses back into page/index.
    assert all(".pdf-" in p.name for p in written)


# ------------------------------------------------------------ /doc/annotate


def _quads_for(path, needle, page=0):
    doc = pymupdf.open(str(path))
    try:
        return [
            [q.ul.x, q.ul.y, q.ur.x, q.ur.y, q.ll.x, q.ll.y, q.lr.x, q.lr.y]
            for q in doc[page].search_for(needle, quads=True)
        ]
    finally:
        doc.close()


def test_annotate_returns_a_pdf_with_one_highlight(client, two_page):
    response = client.post(
        "/doc/annotate",
        json={
            "path": str(two_page),
            "annotations": [
                {"page": 0, "quads": _quads_for(two_page, "NEEDLE")}
            ],
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"

    doc = pymupdf.open(stream=response.content, filetype="pdf")
    try:
        # Read attributes inside the iteration: pymupdf unbinds an annotation
        # from its page once the generator advances past it.
        kinds = [annot.type[1] for annot in doc[0].annots()]
        assert kinds == ["Highlight"]
    finally:
        doc.close()


def test_annotate_does_not_accumulate_across_calls(client, two_page):
    """The reason mutating endpoints must not touch the handle cache.

    On a reused handle the second response carries the first call's highlight
    as well, and the third carries both — a wrong document with nothing
    failing.
    """
    payload = {
        "path": str(two_page),
        "annotations": [{"page": 0, "quads": _quads_for(two_page, "NEEDLE")}],
    }
    counts = []
    for _ in range(3):
        response = client.post("/doc/annotate", json=payload)
        doc = pymupdf.open(stream=response.content, filetype="pdf")
        try:
            counts.append(len(list(doc[0].annots())))
        finally:
            doc.close()
    assert counts == [1, 1, 1]


def test_annotate_sets_colour_on_stroke_and_applies_opacity(client, two_page):
    response = client.post(
        "/doc/annotate",
        json={
            "path": str(two_page),
            "annotations": [
                {
                    "page": 0,
                    "quads": _quads_for(two_page, "NEEDLE"),
                    "color": [1.0, 0.92, 0.23],
                    "opacity": 0.4,
                }
            ],
        },
    )
    doc = pymupdf.open(stream=response.content, filetype="pdf")
    try:
        annot = next(iter(doc[0].annots()))
        # fill= renders as default yellow; the colour has to go on stroke.
        assert annot.colors["stroke"] == pytest.approx([1.0, 0.92, 0.23], abs=1e-6)
        assert annot.opacity == pytest.approx(0.4, abs=1e-6)
    finally:
        doc.close()


def test_annotate_accepts_rects_as_well_as_quads(client, two_page):
    response = client.post(
        "/doc/annotate",
        json={
            "path": str(two_page),
            "annotations": [{"page": 0, "rects": [[72.0, 84.0, 200.0, 104.0]]}],
        },
    )
    assert response.status_code == 200


def test_annotate_refuses_both_or_neither_geometry(client, two_page):
    for annotation in (
        {"page": 0},
        {"page": 0, "quads": _quads_for(two_page, "NEEDLE"), "rects": [[1, 2, 3, 4]]},
        {"page": 0, "quads": []},
    ):
        response = client.post(
            "/doc/annotate",
            json={"path": str(two_page), "annotations": [annotation]},
        )
        assert response.status_code == 422


def test_annotate_page_fault_is_a_document_fault(client, mixed_pages):
    assert (
        client.post(
            "/doc/annotate",
            json={
                "path": str(mixed_pages),
                "annotations": [{"page": 1, "rects": [[10, 10, 20, 20]]}],
            },
        ).status_code
        == 400
    )
