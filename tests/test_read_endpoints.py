"""/doc/info, /doc/text-pages, /doc/search, /doc/page-words, /doc/page-blocks."""

from __future__ import annotations

import pymupdf
import pytest

# --------------------------------------------------------------- /doc/info


def test_info_reports_page_count_and_flags(client, two_page):
    body = client.post("/doc/info", json={"path": str(two_page)}).json()
    assert body == {"page_count": 2, "is_encrypted": False, "needs_pass": False}


def test_info_opens_an_encrypted_document_and_reports_needs_pass(client, encrypted):
    """The reason needs_pass is observable at all.

    MuPDF opens an encrypted document and answers page_count; only page loads
    fail. Refusing at open would make this field permanently unreachable.
    """
    body = client.post("/doc/info", json={"path": str(encrypted)}).json()
    assert body["needs_pass"] is True
    assert body["is_encrypted"] is True
    assert body["page_count"] == 2


@pytest.mark.parametrize("kind", ["missing", "corrupt"])
def test_info_open_failure_is_a_document_fault(client, request, kind):
    path = request.getfixturevalue(kind)
    response = client.post("/doc/info", json={"path": str(path)})
    assert response.status_code == 400
    assert response.json()["error"] == "document"


# --------------------------------------------------------- /doc/text-pages


def test_text_pages_returns_one_row_per_page_in_request_order(client, two_page):
    rows = client.post(
        "/doc/text-pages", json={"path": str(two_page), "pages": [1, 0]}
    ).json()["rows"]
    assert [r["page"] for r in rows] == [1, 0]
    assert "Second page" in rows[0]["text"]
    assert "NEEDLE" in rows[1]["text"]


def test_text_pages_matches_the_engine_byte_for_byte(client, two_page):
    rows = client.post(
        "/doc/text-pages", json={"path": str(two_page), "pages": [0]}
    ).json()["rows"]
    doc = pymupdf.open(str(two_page))
    try:
        assert rows[0]["text"] == doc[0].get_text("text")
    finally:
        doc.close()


def test_text_pages_emits_an_error_row_and_keeps_going(client, mixed_pages):
    """Nothing is skipped: a faulted page becomes a row, in position.

    The caller reads rows in order and decides — a match before the error row
    is today's answer; an error row first is a document fault.
    """
    rows = client.post(
        "/doc/text-pages", json={"path": str(mixed_pages), "pages": [0, 1]}
    ).json()["rows"]
    assert [r["page"] for r in rows] == [0, 1]
    assert "NEEDLE" in rows[0]["text"]
    assert "error" in rows[1] and "text" not in rows[1]


def test_text_pages_on_an_encrypted_document_is_all_error_rows(client, encrypted):
    rows = client.post(
        "/doc/text-pages", json={"path": str(encrypted), "pages": [0, 1]}
    ).json()["rows"]
    assert all("error" in r for r in rows)


def test_text_pages_open_failure_has_no_rows(client, corrupt):
    response = client.post(
        "/doc/text-pages", json={"path": str(corrupt), "pages": [0]}
    )
    assert response.status_code == 400
    assert "rows" not in response.json()


@pytest.mark.parametrize("pages", [[], list(range(33))])
def test_text_pages_rejects_an_out_of_contract_window(client, two_page, pages):
    response = client.post(
        "/doc/text-pages", json={"path": str(two_page), "pages": pages}
    )
    assert response.status_code == 422


# -------------------------------------------------------------- /doc/search


def test_search_is_page_major_not_needle_major(client, tier_fixture):
    """The single most invertible rule in the contract.

    Needle-major would return page 3 with the full anchor (needle_index 0);
    page-major returns page 1 with the fragment. The caller derives its quality
    tier from needle_index, so inverting this silently downgrades every
    citation that has a fragment on an earlier page.
    """
    rows = client.post(
        "/doc/search",
        json={
            "path": str(tier_fixture),
            "needles": [
                "The vessel shall maintain a valid certificate",  # full anchor
                "The vessel shall maintain",                      # fragment
            ],
            "page_order": [0, 1, 2, 3],
        },
    ).json()["rows"]
    assert len(rows) == 1
    assert rows[0]["page"] == 1
    assert rows[0]["needle_index"] == 1  # the fragment won, on the earlier page


def test_search_stops_at_the_first_hit(client, two_page):
    rows = client.post(
        "/doc/search",
        json={"path": str(two_page), "needles": ["page"], "page_order": [0, 1]},
    ).json()["rows"]
    assert len(rows) == 1 and rows[0]["page"] == 0


def test_search_prefers_the_earlier_needle_within_a_page(client, two_page):
    rows = client.post(
        "/doc/search",
        json={
            "path": str(two_page),
            "needles": ["NEEDLE", "page zero"],
            "page_order": [0],
        },
    ).json()["rows"]
    assert rows[0]["needle_index"] == 0


def test_search_miss_returns_no_rows(client, two_page):
    rows = client.post(
        "/doc/search",
        json={
            "path": str(two_page),
            "needles": ["absent string"],
            "page_order": [0, 1],
        },
    ).json()["rows"]
    assert rows == []


def test_search_quads_round_trip_through_the_pinned_reconstruction(client, two_page):
    """The wire encoding and its inverse, pinned together.

    A flat 8-sequence does not construct a Quad — only four point pairs do — so
    the encoding and the reconstruction have to be fixed as a pair or the
    annotate side silently receives geometry it cannot rebuild.
    """
    rows = client.post(
        "/doc/search",
        json={"path": str(two_page), "needles": ["NEEDLE"], "page_order": [0]},
    ).json()["rows"]
    flat = rows[0]["quads"][0]
    assert len(flat) == 8

    rebuilt = pymupdf.Quad(flat[0:2], flat[2:4], flat[4:6], flat[6:8])
    doc = pymupdf.open(str(two_page))
    try:
        assert rebuilt == doc[0].search_for("NEEDLE", quads=True)[0]
    finally:
        doc.close()

    with pytest.raises(ValueError):
        pymupdf.Quad(flat)  # the encoding cannot regress to a flat sequence


def test_search_can_return_rects_instead_of_quads(client, two_page):
    rows = client.post(
        "/doc/search",
        json={
            "path": str(two_page),
            "needles": ["NEEDLE"],
            "page_order": [0],
            "quads": False,
        },
    ).json()["rows"]
    assert len(rows[0]["rects"][0]) == 4
    assert "quads" not in rows[0]


def test_search_emits_an_error_row_before_a_later_hit(client, mixed_pages):
    rows = client.post(
        "/doc/search",
        json={"path": str(mixed_pages), "needles": ["NEEDLE"], "page_order": [1, 0]},
    ).json()["rows"]
    assert "error" in rows[0] and rows[0]["page"] == 1
    assert rows[1]["page"] == 0 and "needle_index" in rows[1]


def test_search_never_visits_pages_after_the_hit(client, mixed_pages):
    rows = client.post(
        "/doc/search",
        json={"path": str(mixed_pages), "needles": ["NEEDLE"], "page_order": [0, 1]},
    ).json()["rows"]
    assert len(rows) == 1 and rows[0]["page"] == 0


# --------------------------------------------------------- /doc/page-words


def test_page_words_rows_are_verbatim_eight_element_tuples(client, two_page):
    """Elements 5 and 6 are block_no and line_no.

    The caller unions word rects per (block, line) to build stacked line bars,
    so a reshaped or renumbered row changes the rendered highlight geometry.
    """
    words = client.post(
        "/doc/page-words", json={"path": str(two_page), "page": 0}
    ).json()["words"]
    assert words
    for row in words:
        assert len(row) == 8
        assert all(isinstance(v, float) for v in row[:4])
        assert isinstance(row[4], str)
        assert all(isinstance(v, int) for v in row[5:])


def test_page_words_matches_the_engine(client, two_page):
    words = client.post(
        "/doc/page-words", json={"path": str(two_page), "page": 0}
    ).json()["words"]
    doc = pymupdf.open(str(two_page))
    try:
        assert words == [list(w) for w in doc[0].get_text("words")]
    finally:
        doc.close()


def test_page_words_page_fault_is_400_not_an_error_row(client, mixed_pages):
    """The single-page endpoints have no row shape to carry a fault."""
    response = client.post(
        "/doc/page-words", json={"path": str(mixed_pages), "page": 1}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "document"


def test_page_words_out_of_range_is_400(client, two_page):
    assert (
        client.post("/doc/page-words", json={"path": str(two_page), "page": 99}).status_code
        == 400
    )


def test_negative_page_is_a_contract_fault(client, two_page):
    """pymupdf would happily wrap: doc[-1] is the LAST page, silently."""
    assert (
        client.post("/doc/page-words", json={"path": str(two_page), "page": -1}).status_code
        == 422
    )


# -------------------------------------------------------- /doc/page-blocks


def test_page_blocks_joins_spans_with_no_separator(client, spans_fixture):
    """A word split across two spans must come back unspaced.

    Spans concatenate with nothing between them, and exactly one space is
    appended after each line. It is not " ".join(spans) — that would insert a
    space inside "Hyphenation" and break the caller's containment test.
    """
    blocks = client.post(
        "/doc/page-blocks", json={"path": str(spans_fixture), "page": 0}
    ).json()["blocks"]
    joined = "".join(b["text"] for b in blocks)
    assert "Hyphenation" in joined
    assert "Hyphen ation" not in joined


def test_page_blocks_preserves_internal_whitespace_runs(client, spans_fixture):
    blocks = client.post(
        "/doc/page-blocks", json={"path": str(spans_fixture), "page": 0}
    ).json()["blocks"]
    assert any("Second  line   with runs" in b["text"] for b in blocks)


def test_page_blocks_appends_one_space_per_line_and_never_strips(client, spans_fixture):
    blocks = client.post(
        "/doc/page-blocks", json={"path": str(spans_fixture), "page": 0}
    ).json()["blocks"]
    assert all(b["text"].endswith(" ") for b in blocks)


def test_page_blocks_excludes_image_blocks(client, spans_fixture):
    """Proof the reduced flag set is in force.

    TEXT_PRESERVE_WHITESPACE is 2 — strictly smaller than the default 199, not
    an addition to it — so image blocks are absent here while page-data's
    dict_blocks, which uses the default, must still contain them.
    """
    blocks = client.post(
        "/doc/page-blocks", json={"path": str(spans_fixture), "page": 0}
    ).json()["blocks"]
    assert blocks
    assert all(len(b["bbox"]) == 4 for b in blocks)

    doc = pymupdf.open(str(spans_fixture))
    try:
        default_blocks = doc[0].get_text("dict")["blocks"]
    finally:
        doc.close()
    types = [b.get("type") for b in default_blocks]
    assert 1 in types, "fixture must carry an image block for this test to mean anything"
    # Exactly the text blocks survive, and the image block does not.
    assert len(blocks) == types.count(0)


def test_page_blocks_page_fault_is_400(client, mixed_pages):
    assert (
        client.post(
            "/doc/page-blocks", json={"path": str(mixed_pages), "page": 1}
        ).status_code
        == 400
    )


def test_search_returns_every_quad_of_a_multi_line_hit(client, root):
    """One logical hit can be several quads; all of them are the highlight."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(
        pymupdf.Rect(72, 80, 190, 160),
        "wrapping anchor phrase continues here",
        fontsize=11,
    )
    path = root / "wrapped.pdf"
    doc.save(str(path))
    doc.close()

    rows = client.post(
        "/doc/search",
        json={
            "path": str(path),
            "needles": ["wrapping anchor phrase continues"],
            "page_order": [0],
        },
    ).json()["rows"]
    assert rows and len(rows[0]["quads"]) >= 2
    assert all(len(q) == 8 for q in rows[0]["quads"])


def test_search_open_failure_has_no_rows(client, corrupt):
    response = client.post(
        "/doc/search",
        json={"path": str(corrupt), "needles": ["x"], "page_order": [0]},
    )
    assert response.status_code == 400
    assert "rows" not in response.json()


def test_search_on_an_encrypted_document_is_all_error_rows(client, encrypted):
    rows = client.post(
        "/doc/search",
        json={"path": str(encrypted), "needles": ["secret"], "page_order": [0, 1]},
    ).json()["rows"]
    assert len(rows) == 2 and all("error" in r for r in rows)


def test_page_words_on_a_blank_page_is_an_empty_list(client, root):
    doc = pymupdf.open()
    doc.new_page()
    path = root / "blank.pdf"
    doc.save(str(path))
    doc.close()
    response = client.post("/doc/page-words", json={"path": str(path), "page": 0})
    assert response.status_code == 200
    assert response.json()["words"] == []


def test_page_words_on_an_encrypted_document_is_400(client, encrypted):
    assert (
        client.post("/doc/page-words", json={"path": str(encrypted), "page": 0}).status_code
        == 400
    )


def test_info_on_an_empty_file_is_a_document_fault(client, root):
    path = root / "empty.pdf"
    path.write_bytes(b"")
    response = client.post("/doc/info", json={"path": str(path)})
    assert response.status_code == 400
    assert response.json()["error"] == "document"
