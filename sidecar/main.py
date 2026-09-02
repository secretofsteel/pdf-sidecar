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
from .handles import FITZ_LOCK, cached_document
from .licence import assert_free_layout, layout_canary
from .models import (
    AnnotateBody,
    DocBody,
    PageBody,
    PageDataBody,
    PagesBody,
    RenderBody,
    SearchBody,
    ToMarkdownBody,
)
from .paths import resolve_allowed
from . import engine

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
LOGGER = logging.getLogger("sidecar")

# The one thing that refuses to boot.  A configuration mistake logs and starts
# (the deploy gate and the per-call contract check catch those); an unlicensed
# engine must never serve a single request.
assert_free_layout()

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


@app.post("/doc/annotate")
def doc_annotate(body: AnnotateBody) -> Response:
    path = resolve_allowed(body.path)
    for item in body.annotations:
        if (item.quads is None) == (item.rects is None):
            raise ContractFault(f"page {item.page}: exactly one of quads / rects")
        if not (item.quads or item.rects):
            raise ContractFault(f"page {item.page}: empty geometry list")
    with FITZ_LOCK:
        pdf = engine.annotate(str(path), body.annotations, body.garbage, body.deflate)
    return Response(content=pdf, media_type="application/pdf")
