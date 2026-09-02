"""Every PyMuPDF call the service makes, and nothing else.

Two rules govern this module:

* **Raw shapes only.**  MuPDF's return values cross the wire as they are —
  7-tuple blocks stay 7-tuples, raw character counts stay counts.  The caller
  owns every threshold, every label and every verdict.  A field that looks like
  it wants normalising almost certainly has a consumer that depends on the
  un-normalised form.
* **Reproduce today's call, not a better one.**  Flags, kwargs and even the
  absence of kwargs are pinned against the in-process code this service
  replaces, because the acceptance test is byte-identical output.  Where two
  neighbouring calls use different flags, that difference is deliberate and
  commented.

Every function here must be called with ``handles.FITZ_LOCK`` held.
"""

from __future__ import annotations

import io
from typing import Any

import pymupdf

from .errors import DocumentFault, fault_detail

# ---------------------------------------------------------------- page access


def _page(doc: pymupdf.Document, index: int) -> pymupdf.Page:
    """Load a page, or raise the 400 fault.

    Page load is where an encrypted document actually fails — it opens fine —
    and where a damaged page tree surfaces.
    """
    try:
        return doc[index]
    except Exception as exc:
        raise DocumentFault(fault_detail(exc)) from exc


# ------------------------------------------------------------------ /doc/info


def info(doc: pymupdf.Document) -> dict[str, Any]:
    return {
        "page_count": doc.page_count,
        "is_encrypted": bool(doc.is_encrypted),
        # needs_pass is an int (0/1) on 1.28.2, not a bool.
        "needs_pass": bool(doc.needs_pass),
    }


# ------------------------------------------------------------ /doc/text-pages


def text_pages(doc: pymupdf.Document, pages: list[int]) -> list[dict[str, Any]]:
    """One row per requested page, in request order; nothing is skipped.

    A page that fails to load becomes an error row rather than failing the
    request, so a caller scanning pages in priority order still gets the pages
    it could have read — matching the in-process loop, which never touched a
    later page once an earlier one matched.
    """
    rows: list[dict[str, Any]] = []
    for pno in pages:
        try:
            # DEFAULT flags (195).  NOT page-blocks' TEXT_PRESERVE_WHITESPACE,
            # which is a strictly smaller set and yields different text.
            rows.append({"page": pno, "text": doc[pno].get_text("text")})
        except Exception as exc:
            rows.append({"page": pno, "error": fault_detail(exc)})
    return rows


# ---------------------------------------------------------------- /doc/search


def search(
    doc: pymupdf.Document,
    needles: list[str],
    page_order: list[int],
    quads: bool,
) -> list[dict[str, Any]]:
    """Page-major, needle-minor; the first hit wins and ends the scan.

    The ordering is the whole point and is the single most inverted rule in the
    contract: iterating needles first would return the *best* needle's page,
    where the in-process loop returns the *earliest* page's best needle.  On a
    document with a fragment early and the full anchor late, the two disagree
    about both the page and the resulting quality tier.
    """
    rows: list[dict[str, Any]] = []
    for pno in page_order:
        try:
            page = doc[pno]
        except Exception as exc:
            rows.append({"page": pno, "error": fault_detail(exc)})
            continue

        for index, needle in enumerate(needles):
            found = page.search_for(needle, quads=quads)
            if not found:
                continue
            if quads:
                geometry = [
                    [q.ul.x, q.ul.y, q.ur.x, q.ur.y, q.ll.x, q.ll.y, q.lr.x, q.lr.y]
                    for q in found
                ]
                rows.append({"page": pno, "needle_index": index, "quads": geometry})
            else:
                rows.append(
                    {
                        "page": pno,
                        "needle_index": index,
                        "rects": [[r.x0, r.y0, r.x1, r.y1] for r in found],
                    }
                )
            return rows  # stop at the first hit; later pages are never visited
    return rows


# ----------------------------------------------------- /doc/page-words|blocks


def page_words(doc: pymupdf.Document, index: int) -> list[list[Any]]:
    """`get_text("words")` verbatim: 8 elements per row.

    Elements 5 and 6 (block_no, line_no) are load-bearing downstream — the
    caller unions word rects per (block, line) to build stacked line bars — so
    the rows must not be reshaped or renumbered.
    """
    return [list(w) for w in _page(doc, index).get_text("words")]


def page_blocks(doc: pymupdf.Document, index: int) -> list[dict[str, Any]]:
    """Text blocks as the block-level anchor search consumes them.

    TEXT_PRESERVE_WHITESPACE (2) is *smaller* than the default (199): it drops
    image blocks and keeps internal whitespace runs.  Its sibling in
    /doc/page-data deliberately uses the default instead — do not unify them.

    The join rule is equally deliberate: spans concatenate with NO separator,
    and one space is appended after each line.  A word split across two spans
    must come back unspaced, because that is what the caller's containment
    test was tuned against.
    """
    page_dict = _page(doc, index).get_text(
        "dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE
    )
    blocks: list[dict[str, Any]] = []
    for block in page_dict.get("blocks", []):
        if block.get("type") != 0:  # text blocks only
            continue
        text = ""
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text += span.get("text", "")
            text += " "
        blocks.append({"bbox": list(block["bbox"]), "text": text})
    return blocks


# ------------------------------------------------------------- /doc/page-data


def _scan_facts(page: pymupdf.Page, errors: dict[str, str]) -> dict[str, Any]:
    """Raw counts behind the scanned-page decision — never the decision itself.

    Two of the caller's gates are integer comparisons on these counts, so a
    ratio would be wrong at the boundaries (1,116 disagreements over 32,020
    measured pairs).  Both legs degrade to the value the in-process code
    produced when the same call failed, which is why `errors["scan"]` is
    informational and the part is still emitted.
    """
    try:
        trace = page.get_texttrace()
        total = sum(len(span.get("chars", ())) for span in trace)
        invisible = sum(
            len(span.get("chars", ())) for span in trace if span.get("type") == 3
        )
    except Exception as exc:
        total = invisible = 0  # today's `tt = []`
        errors["scan"] = fault_detail(exc)

    try:
        page_area = page.rect.get_area() or 1.0
        ratio = max(
            (
                pymupdf.Rect(image["bbox"]).get_area() / page_area
                for image in page.get_image_info()
            ),
            default=0.0,
        )
    except Exception as exc:
        ratio = None  # the caller then declines to call the page scanned
        errors["scan"] = fault_detail(exc)

    return {
        "total_chars": total,
        "invisible_chars": invisible,
        "max_image_area_ratio": ratio,
    }


def _tables(page: pymupdf.Page, errors: dict[str, str]) -> list[dict[str, Any]]:
    """find_tables() with no kwargs, and per-item faults.

    `bbox` is a computed property, so it either returns four floats or raises —
    it is never absent.  When it raises, the item carries `errors.bbox` and no
    `bbox`, and the caller reproduces today's 1-pt strip at the page bottom.
    """
    try:
        finder = page.find_tables()
        found = list(getattr(finder, "tables", [])) if finder else []
    except Exception as exc:
        errors["tables"] = fault_detail(exc)
        return []

    out: list[dict[str, Any]] = []
    for table in found:
        row: dict[str, Any] = {}
        try:
            row["bbox"] = list(table.bbox)
        except Exception as exc:
            row["errors"] = {"bbox": fault_detail(exc)}
        try:
            row["rows"] = table.extract()
        except Exception as exc:
            row["rows"] = None
            row["error"] = fault_detail(exc)
        out.append(row)
    return out


def _images(page: pymupdf.Page, errors: dict[str, str]) -> list[dict[str, Any]]:
    """Placements per image, in get_images(full=True) order.

    Order is load-bearing: the caller numbers images by position when it builds
    the description context a model reads back.
    """
    try:
        listed = page.get_images(full=True)
    except Exception as exc:
        errors["images"] = fault_detail(exc)
        return []

    out: list[dict[str, Any]] = []
    for image in listed:
        xref = image[0]
        try:
            out.append(
                {"xref": xref, "rects": [list(r) for r in page.get_image_rects(xref)]}
            )
        except Exception as exc:
            out.append({"xref": xref, "rects": None, "error": fault_detail(exc)})
    return out


def _part(errors: dict[str, str], name: str, produce) -> Any:
    """Run one part, recording a part-level fault instead of failing the page."""
    try:
        return produce()
    except Exception as exc:
        errors[name] = fault_detail(exc)
        return None


def page_data(
    doc: pymupdf.Document, pages: list[int], parts: list[str]
) -> list[dict[str, Any]]:
    """The library extractor's whole per-page payload, part-selectable.

    Faults are recorded at the granularity the caller already handles: a part
    that fails yields its empty value and an `errors` entry; a table or image
    that fails is emitted alone with its own error and the caller skips exactly
    that item; a page that will not load yields every part null under
    `errors["page"]`, which the caller turns into a whole-document fallback.
    """
    wanted = set(parts)
    out: list[dict[str, Any]] = []

    for pno in pages:
        errors: dict[str, str] = {}
        try:
            page = doc[pno]
        except Exception as exc:
            row = {"page": pno, "page_rect": None, "errors": {"page": fault_detail(exc)}}
            row.update({name: None for name in wanted})
            out.append(row)
            continue

        row = {"page": pno, "page_rect": list(page.rect)}

        if "text" in wanted:
            # DEFAULT flags, matching the in-process bare call.
            row["text"] = _part(errors, "text", lambda: page.get_text("text"))
        if "blocks" in wanted:
            # 7-tuples, verbatim.  The caller reads element 5 to key a repair;
            # reshaping to 5-tuples would disable it silently.
            row["blocks"] = _part(
                errors, "blocks", lambda: [list(b) for b in page.get_text("blocks")]
            )
        if "dict_blocks" in wanted:
            row["dict_blocks"] = _part(errors, "dict_blocks", lambda: _dict_blocks(page))
        if "tables" in wanted:
            row["tables"] = _tables(page, errors)
        if "clusters" in wanted:
            row["clusters"] = _part(
                errors,
                "clusters",
                lambda: [
                    list(r)
                    for r in page.cluster_drawings(x_tolerance=10, y_tolerance=10)
                ],
            )
        if "drawings" in wanted:
            # Only `rect` is consumed: the individual thin rules that
            # cluster_drawings merges away are what the formula heuristic needs.
            row["drawings"] = _part(
                errors,
                "drawings",
                lambda: [{"rect": list(d["rect"])} for d in page.get_drawings()],
            )
        if "images" in wanted:
            row["images"] = _images(page, errors)
        if "scan" in wanted:
            row["scan"] = _scan_facts(page, errors)

        row["errors"] = errors
        out.append(row)

    return out


def _dict_blocks(page: pymupdf.Page) -> list[dict[str, Any]]:
    """`get_text("dict")` with DEFAULT flags, indexed over ALL blocks.

    The index enumerates image blocks too, and the caller keys a lookup off it.
    That lookup has a known off-by-one on image-before-text pages; preserving
    the raw enumeration preserves the existing output, which is what the
    acceptance test compares against.  Fixing it belongs to the caller, later.

    Image bytes are never serialised — a type-1 block carries the decoded image
    in `blk["image"]`, and only its geometry is wanted here.
    """
    out: list[dict[str, Any]] = []
    for index, block in enumerate(page.get_text("dict").get("blocks", [])):
        lines = [
            {
                "bbox": list(line["bbox"]),
                "text": "".join(s.get("text", "") for s in line.get("spans", [])),
            }
            for line in block.get("lines", [])
        ]
        out.append(
            {
                "index": index,
                "type": block.get("type"),
                "bbox": list(block["bbox"]),
                "lines": lines,
            }
        )
    return out


# ---------------------------------------------------------------- /doc/render


def render(
    doc: pymupdf.Document,
    index: int,
    clip: list[float] | None,
    matrix: list[float] | None,
    dpi: int | None,
) -> bytes:
    """A get_pixmap passthrough — the caller owns every geometry decision.

    `annots` is deliberately not passed: it defaults to True and all four call
    sites rely on that.  `dpi` is never normalised into a matrix, because
    get_pixmap writes the dpi into the PNG header — the bytes differ even when
    the pixels match, which is why parity is measured on decoded pixels.
    """
    page = _page(doc, index)
    kwargs: dict[str, Any] = {}
    if clip is not None:
        kwargs["clip"] = clip
    if matrix is not None:
        kwargs["matrix"] = pymupdf.Matrix(*matrix)
    if dpi is not None:
        kwargs["dpi"] = dpi
    try:
        return page.get_pixmap(**kwargs).tobytes("png")
    except Exception as exc:
        raise DocumentFault(fault_detail(exc)) from exc


# -------------------------------------------------------------- /doc/annotate


def annotate(
    path: str, annotations: list[Any], garbage: int, deflate: bool
) -> bytes:
    """Highlight and save, on a private handle.

    The handle must be private: annotations accumulate on a reused one, so the
    second call on a cached handle would return a document carrying the first
    call's highlights too.
    """
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        raise DocumentFault(fault_detail(exc)) from exc

    try:
        for item in annotations:
            page = _page(doc, item.page)
            if item.quads is not None:
                # Only four point pairs construct a Quad; a flat 8-sequence
                # raises, and four bare [x, y] pairs make add_highlight_annot
                # return None with no exception at all.
                items = [
                    pymupdf.Quad(r[0:2], r[2:4], r[4:6], r[6:8]) for r in item.quads
                ]
            else:
                items = [pymupdf.Rect(*r) for r in item.rects]

            try:
                annot = page.add_highlight_annot(items)
            except Exception as exc:
                raise DocumentFault(fault_detail(exc)) from exc
            if annot is None:
                # Silent None would ship a PDF whose highlight simply is not
                # there, with nothing failing anywhere.
                raise RuntimeError(
                    f"add_highlight_annot returned None for page {item.page} — "
                    "rejected geometry"
                )

            annot.set_colors(stroke=tuple(item.color))  # stroke; fill renders default
            annot.set_opacity(item.opacity)
            annot.update()  # builds the appearance stream the viewer renders

        buffer = io.BytesIO()
        doc.save(buffer, garbage=garbage, deflate=deflate)
        return buffer.getvalue()
    finally:
        doc.close()
