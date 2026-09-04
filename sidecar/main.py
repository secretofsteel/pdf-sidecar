"""The HTTP surface.

Every handler is a sync ``def`` on purpose: FastAPI runs those in a worker
thread rather than on the event loop, so a multi-second table scan cannot stall
/health or any other in-flight request.  The engine work inside is serialised
by FITZ_LOCK because PyMuPDF is not thread-safe and that thread pool is 40 wide
by default; real parallelism comes from uvicorn worker processes.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

# Set before pymupdf4llm is imported: on import it prints a one-line
# recommendation for the commercial layout package to stdout, which has no
# business in a JSON service's log stream — and the package it recommends is
# precisely the one this service refuses to run. The systemd unit sets the same
# variable; doing it here too covers dev, tests and any other launch path.
os.environ.setdefault("PYMUPDF_SUGGEST_LAYOUT_ANALYZER", "0")

import pymupdf
import pymupdf4llm
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import SIDECAR_CONTRACT, SIDECAR_VERSION
from .config import ALLOWED_ROOTS, WORKERS
from .errors import ContractFault, DocumentFault, LayoutCanaryTripped, fault_detail
from .handles import FITZ_LOCK, cached_document, ocr_words
from .licence import assert_free_layout, layout_canary
from .models import (
    AnnotateBody,
    DocBody,
    PageBody,
    PageDataBody,
    PagesBody,
    PageWordsOcrBody,
    RenderBody,
    SearchBody,
    ToMarkdownBody,
)
from .paths import resolve_allowed
from . import engine
from .engine_state import markdown_state, restore_baseline

# `import pymupdf4llm` above has just left the process in the import-only
# state (quad corrections skipped, PyMuPDF's default table flags), in which
# table cells lose their spaces — prod 2026-09-03, 193/669 documents.  The
# read endpoints port code that ran, for nearly all of its life, in the state
# a to_markdown call leaves behind; put the process there before the first
# request (engine_state.py says why that state, and not PyMuPDF's defaults).
restore_baseline()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
LOGGER = logging.getLogger("sidecar")

# The one thing that refuses to boot.  A configuration mistake logs and starts
# (the deploy gate and the per-call contract check catch those); an unlicensed
# engine must never serve a single request.
assert_free_layout()


def _tessdata_report() -> str:
    """Where MuPDF would look, and whether the file is actually there.

    ``get_tessdata()`` does NOT validate what it returns, which is exactly why
    the false branch has to print both halves: a prefix pointing at the wrong
    directory and a directory with no ``eng.traineddata`` in it produce the
    same engine error, and neither is visible from the error text.
    """
    try:
        resolved = pymupdf.get_tessdata()
    except Exception as exc:
        return f"tessdata dir unresolved ({fault_detail(exc)})"
    traineddata = os.path.join(str(resolved), "eng.traineddata")
    return (
        f"tessdata dir {resolved!r}, "
        f"eng.traineddata {'present' if os.path.exists(traineddata) else 'ABSENT'}"
    )


def _probe_ocr() -> bool:
    """Capability by EXECUTION, once per worker, at import.

    Deliberately placed after ``logging.basicConfig`` above: a line emitted
    before it is dropped, which is how the allowlist line went missing from a
    live worker log.

    Never raises. A worker with no Tesseract must still serve the other nine
    endpoints; what it must not do is claim it can OCR. The result rides on
    /health so the caller can stop asking rather than discover it one 500 at
    a time.
    """
    try:
        with FITZ_LOCK:
            engine.ocr_probe()
    except Exception as exc:
        LOGGER.warning(
            "OCR unavailable in this worker (%s) — %s; /health.ocr reports "
            "false and /doc/page-words-ocr will fail",
            fault_detail(exc),
            _tessdata_report(),
        )
        return False
    LOGGER.info("OCR available: %s", _tessdata_report())
    return True


OCR_AVAILABLE: bool = _probe_ocr()

app = FastAPI(title="pdf-sidecar", version=SIDECAR_VERSION)


# Every error leaves by one of these four doors, and every one of them emits the
# same flat {"error": <slug>, "detail": <str>} body. FastAPI's own shapes would
# otherwise give three different envelopes on the same service — a nested
# {"detail": {...}} for our faults, a list of validation dicts for 422, and
# plain text for an unhandled 500 — and a client cannot branch on that.


@app.exception_handler(StarletteHTTPException)
def _http_fault(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        body = exc.detail  # one of ours, already in the contract's shape
    else:
        body = {"error": "http", "detail": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(RequestValidationError)
def _validation_fault(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": "contract",
            "detail": "; ".join(
                f"{'.'.join(str(p) for p in err['loc'][1:])}: {err['msg']}"
                for err in exc.errors()
            )
            or "request body does not match the contract",
        },
    )


@app.exception_handler(Exception)
def _unexpected_fault(request: Request, exc: Exception) -> JSONResponse:
    # A bug here is the service's fault, not the document's, so it must not be
    # mistakable for a 400 — the caller degrades quietly on those.
    LOGGER.exception("unhandled error serving %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "internal", "detail": f"{type(exc).__name__}: {exc}"},
    )


@app.get("/health")
def health() -> dict[str, object]:
    """Liveness plus everything the deploy gate compares.

    `layout_canary` is re-read here rather than cached because the hazard it
    reports can be introduced by any import at any point in the process's life.
    """
    return {
        "status": "ok",
        "version": SIDECAR_VERSION,
        "contract": SIDECAR_CONTRACT,
        "pymupdf_version": pymupdf.__version__,
        "pymupdf4llm_version": pymupdf4llm.__version__,
        "layout_canary": layout_canary(),
        "workers": WORKERS,
        "allowed_roots": [str(r) for r in ALLOWED_ROOTS],
        # Cached from the startup probe, unlike layout_canary: this one is a
        # property of the deployment, not something an import can flip.
        "ocr": OCR_AVAILABLE,
    }


@app.post("/doc/info")
def doc_info(body: DocBody) -> dict[str, object]:
    path = resolve_allowed(body.path)
    with FITZ_LOCK:
        # An encrypted document opens cleanly and reports its page count; only
        # page loads fail. Reporting the flags rather than refusing here is what
        # keeps `needs_pass` observable at all.
        return engine.info(cached_document(path))


@app.post("/doc/text-pages")
def doc_text_pages(body: PagesBody) -> dict[str, object]:
    path = resolve_allowed(body.path)
    with FITZ_LOCK:
        return {"rows": engine.text_pages(cached_document(path), body.pages)}


@app.post("/doc/search")
def doc_search(body: SearchBody) -> dict[str, object]:
    path = resolve_allowed(body.path)
    with FITZ_LOCK:
        rows = engine.search(
            cached_document(path), body.needles, body.page_order, body.quads
        )
    return {"rows": rows}


@app.post("/doc/page-words")
def doc_page_words(body: PageBody) -> dict[str, object]:
    path = resolve_allowed(body.path)
    with FITZ_LOCK:
        return {"words": engine.page_words(cached_document(path), body.page)}


@app.post("/doc/page-words-ocr")
def doc_page_words_ocr(body: PageWordsOcrBody) -> dict[str, object]:
    """Tesseract words for one page, in `/doc/page-words`' envelope and shape.

    The same 8-element rows from a different source of truth, which is the
    whole point: these words exist on pages that carry no glyphs at all. The
    caller wants GEOMETRY from them — where on the page a phrase it already
    has sits — not text.

    Under FITZ_LOCK like every other engine call. It is a long one — 0.5-3.5 s
    of held GIL at 300 dpi — which is why the production unit runs with
    `--timeout-worker-healthcheck` and why the result is cached per worker.
    """
    path = resolve_allowed(body.path)
    with FITZ_LOCK:
        return {"words": ocr_words(path, body.page, body.dpi, body.language)}


@app.post("/doc/page-blocks")
def doc_page_blocks(body: PageBody) -> dict[str, object]:
    path = resolve_allowed(body.path)
    with FITZ_LOCK:
        return {"blocks": engine.page_blocks(cached_document(path), body.page)}


@app.post("/doc/page-data")
def doc_page_data(body: PageDataBody) -> list[dict[str, object]]:
    """A bare list, unlike the two cite endpoints' {"rows": ...} envelope.

    The asymmetry is deliberate: the cite endpoints were re-wrapped when they
    gained per-page error rows, and the ingestion path was explicitly left
    alone so its payload and fault encoding stay byte-comparable.
    """
    path = resolve_allowed(body.path)
    with FITZ_LOCK:
        return engine.page_data(cached_document(path), body.pages, body.parts)


@app.post("/doc/render")
def doc_render(body: RenderBody) -> Response:
    # get_pixmap accepts both matrix and dpi and lets dpi win silently, so a
    # caller sending both would get a rendering it did not ask for.
    if (body.matrix is None) == (body.dpi is None):
        raise ContractFault("exactly one of matrix / dpi is required")
    path = resolve_allowed(body.path)
    with FITZ_LOCK:
        png = engine.render(
            cached_document(path), body.page, body.clip, body.matrix, body.dpi
        )
    return Response(content=png, media_type="image/png")


@app.post("/doc/to-markdown")
def doc_to_markdown(body: ToMarkdownBody) -> dict[str, str]:
    path = resolve_allowed(body.path)
    # image_path is written into, which makes it the more dangerous of the two
    # path fields, not the lesser. It is containment-checked but NOT substituted:
    # the engine copies this string verbatim into the markdown's image targets,
    # so handing it a resolved path would name a directory the caller never
    # supplied.
    resolve_allowed(body.image_path)

    canary = layout_canary()
    if canary is not None:
        LOGGER.error(
            "refusing /doc/to-markdown: pymupdf._get_layout is %s — the "
            "commercial PyMuPDF-Layout parser is bound in this process",
            canary,
        )
        raise LayoutCanaryTripped(f"pymupdf._get_layout is {canary}")

    with FITZ_LOCK:
        # to_markdown sets the baseline state itself; the exit restores it
        # regardless of what the call did (engine_state.py).
        with markdown_state():
            try:
                # The PATH, never a cached Document: to_markdown calls doc.bake().
                markdown = pymupdf4llm.to_markdown(
                    str(path),
                    write_images=body.write_images,
                    image_path=body.image_path,
                    image_format=body.image_format,
                    dpi=body.dpi,
                    image_size_limit=body.image_size_limit,
                    force_text=body.force_text,
                    table_strategy=body.table_strategy,
                    page_separators=body.page_separators,
                )
            except Exception as exc:
                raise DocumentFault(fault_detail(exc)) from exc
    return {"markdown": markdown if isinstance(markdown, str) else ""}


def _size_mb(path: Path) -> float:
    """Source size for the log line — never a reason to fail a served request."""
    try:
        return os.stat(path).st_size / 1e6
    except OSError:
        return -1.0


@app.post("/doc/annotate")
def doc_annotate(body: AnnotateBody) -> Response:
    path = resolve_allowed(body.path)
    for item in body.annotations:
        if (item.quads is None) == (item.rects is None):
            raise ContractFault(f"page {item.page}: exactly one of quads / rects")
        if not (item.quads or item.rects):
            raise ContractFault(f"page {item.page}: empty geometry list")

    source_mb = _size_mb(path)
    started = time.perf_counter()
    with FITZ_LOCK:
        pdf, excerpt = engine.annotate(
            str(path),
            body.annotations,
            body.garbage,
            body.deflate,
            body.page_range,
        )
    # The line the annotate timeout gets retuned from: what was asked for, how
    # much of the source it cost, and how much came back.
    LOGGER.info(
        "/doc/annotate mode=%s page_range=%s source_mb=%.2f output_mb=%.2f "
        "elapsed_ms=%.1f",
        "full" if body.page_range is None else "excerpt",
        "none"
        if body.page_range is None
        else f"{body.page_range.start}-{body.page_range.end}",
        source_mb,
        len(pdf) / 1e6,
        (time.perf_counter() - started) * 1000.0,
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        # On EVERY response, excerpt or not. The caller cannot open the PDF to
        # find out where it sits in the publication, and the range-less form
        # answers (0, n, n) precisely so it never has to branch on presence.
        headers={
            "X-Pdf-Sidecar-Excerpt-Start": str(excerpt["start"]),
            "X-Pdf-Sidecar-Excerpt-Count": str(excerpt["count"]),
            "X-Pdf-Sidecar-Total-Pages": str(excerpt["total_pages"]),
        },
    )
