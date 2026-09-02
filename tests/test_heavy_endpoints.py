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
    assert response.json()["error"] == "contract"


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


def test_dict_index_and_block_no_diverge_on_image_first_pages(client, image_first):
    """The payload must expose both numberings, unreconciled.

    dict mode counts the image; blocks mode does not. A consumer keys one by the
    other and is off by one on exactly this cohort — a known defect that the
    parity test compares against, so the sidecar must not quietly fix it.
    """
    row = _page_data(client, image_first, [0], ["dict_blocks", "blocks"]).json()[0]

    dict_types = [b["type"] for b in row["dict_blocks"]]
    assert dict_types[0] == 1, "the image must be dict block 0 on this fixture"
    assert [b["index"] for b in row["dict_blocks"]] == list(range(len(dict_types)))

    # blocks mode omits the image entirely, so its numbering starts at the text.
    block_numbers = [b[5] for b in row["blocks"]]
    assert block_numbers == [0, 1]
    text_dict_indices = [b["index"] for b in row["dict_blocks"] if b["type"] == 0]
    assert text_dict_indices == [1, 2]
    assert text_dict_indices != block_numbers  # the divergence, preserved


def test_page_blocks_actually_passes_the_reduced_flag_set(client, spans_fixture, monkeypatch):
    """Assert the call, not just the output.

    On many pages the two flag sets produce identical text, so comparing output
    cannot prove which one was used. Capture the kwarg instead.
    """
    seen: list[object] = []
    original = pymupdf.Page.get_text

    def spy(self, option="text", **kwargs):
        seen.append((option, kwargs.get("flags")))
        return original(self, option, **kwargs)

    monkeypatch.setattr(pymupdf.Page, "get_text", spy)
    client.post("/doc/page-blocks", json={"path": str(spans_fixture), "page": 0})
    assert seen == [("dict", pymupdf.TEXT_PRESERVE_WHITESPACE)]


def test_page_data_dict_blocks_passes_no_flags(client, spans_fixture, monkeypatch):
    """Its sibling must use the DEFAULT set — the difference is the point."""
    seen: list[object] = []
    original = pymupdf.Page.get_text

    def spy(self, option="text", **kwargs):
        seen.append((option, kwargs.get("flags")))
        return original(self, option, **kwargs)

    monkeypatch.setattr(pymupdf.Page, "get_text", spy)
    _page_data(client, spans_fixture, [0], ["dict_blocks"])
    assert seen == [("dict", None)]


@pytest.mark.parametrize(
    "extra",
    [
        {"matrix": [2, 0, 0, 2, 0, 0], "clip": [72.0, 60.0, 200.0, 140.0]},
        {"matrix": [2, 0, 0, 2, 0, 0], "clip": [400.0, 50.0, 460.0, 110.0]},
        {"clip": [72.0, 60.0, 300.0, 160.0], "dpi": 200},
        {"clip": [72.0, 60.0, 300.0, 160.0], "dpi": 220},
    ],
)
def test_render_matches_the_engine_pixel_for_pixel(client, spans_fixture, extra):
    """The four live call shapes, each compared against the engine itself.

    Parity is decoded-pixel, not byte: get_pixmap writes the dpi into the PNG
    header, so two renderings that agree on every pixel still differ in bytes.
    """
    response = client.post(
        "/doc/render", json={"path": str(spans_fixture), "page": 0, **extra}
    )
    assert response.status_code == 200

    kwargs = dict(extra)
    if "matrix" in kwargs:
        kwargs["matrix"] = pymupdf.Matrix(*kwargs["matrix"])
    doc = pymupdf.open(str(spans_fixture))
    try:
        expected = doc[0].get_pixmap(**kwargs)
    finally:
        doc.close()

    served = pymupdf.Pixmap(io.BytesIO(response.content))
    assert (served.width, served.height) == (expected.width, expected.height)
    assert served.samples == expected.samples


def test_tables_bbox_failure_becomes_errors_bbox(client, table_fixture, monkeypatch):
    """bbox is a computed property: it returns four floats or it raises.

    When it raises the item carries errors.bbox and NO bbox, and the caller
    reproduces its 1-pt strip. There is no cells fold to fall back on.
    """
    import pymupdf.table

    monkeypatch.setattr(
        pymupdf.table.Table,
        "bbox",
        property(lambda self: (_ for _ in ()).throw(ValueError("boom"))),
    )
    row = _page_data(client, table_fixture, [0], ["tables"]).json()[0]
    assert row["tables"]
    table = row["tables"][0]
    assert "bbox" not in table
    assert "bbox" in table["errors"]
    assert table["rows"] is not None  # extraction still succeeded


def test_table_extract_failure_is_a_per_item_fault(client, table_fixture, monkeypatch):
    """Rule (c): the caller skips exactly that table, not the page.

    Stubbed at the finder rather than by patching Table.extract, because
    find_tables() calls extract() itself during header detection — patching the
    method would break enumeration and exercise the part-level path instead of
    the item-level one this test is about.
    """

    class _FailingTable:
        bbox = (72.0, 200.0, 372.0, 300.0)

        def extract(self):
            raise RuntimeError("extract failed")

    class _Finder:
        tables = [_FailingTable()]

    monkeypatch.setattr(pymupdf.Page, "find_tables", lambda self, *a, **kw: _Finder())
    row = _page_data(client, table_fixture, [0], ["tables"]).json()[0]
    table = row["tables"][0]
    assert table["rows"] is None and "error" in table
    assert list(table["bbox"]) == [72.0, 200.0, 372.0, 300.0]
    assert row["errors"] == {}  # the PART did not fail, one item did


def test_tables_enumeration_failure_is_a_part_fault(client, table_fixture, monkeypatch):
    def boom(self, *a, **kw):
        raise RuntimeError("find_tables failed")

    monkeypatch.setattr(pymupdf.Page, "find_tables", boom)
    row = _page_data(client, table_fixture, [0], ["tables"]).json()[0]
    assert row["tables"] == []
    assert "tables" in row["errors"]


def test_scan_texttrace_failure_degrades_to_zero_counts(client, two_page, monkeypatch):
    """Today's `tt = []`, reproduced — and the part is still emitted."""

    def boom(self, *a, **kw):
        raise RuntimeError("texttrace failed")

    monkeypatch.setattr(pymupdf.Page, "get_texttrace", boom)
    row = _page_data(client, two_page, [0], ["scan"]).json()[0]
    assert row["scan"]["total_chars"] == 0
    assert row["scan"]["invisible_chars"] == 0
    assert "scan" in row["errors"]  # informational, not suppressing


def test_scan_image_info_failure_nulls_the_ratio(client, two_page, monkeypatch):
    """null is the value on which the caller declines to call a page scanned."""

    def boom(self, *a, **kw):
        raise RuntimeError("image_info failed")

    monkeypatch.setattr(pymupdf.Page, "get_image_info", boom)
    row = _page_data(client, two_page, [0], ["scan"]).json()[0]
    assert row["scan"]["max_image_area_ratio"] is None
    assert row["scan"]["total_chars"] > 0  # the other leg is unaffected


def test_annotate_accepts_an_empty_annotation_list(client, two_page):
    """The shape every unresolved-anchor request sends.

    The caller re-saves unconditionally after annotating, so "nothing to
    highlight" must come back as a valid PDF, not as an error.
    """
    response = client.post(
        "/doc/annotate", json={"path": str(two_page), "annotations": []}
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"

    doc = pymupdf.open(stream=response.content, filetype="pdf")
    try:
        assert list(doc[0].annots()) == []
        assert doc.page_count == 2
    finally:
        doc.close()
