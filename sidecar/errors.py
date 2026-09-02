"""The fault taxonomy, as the sidecar emits it.

One rule decides every status code: does the caller need to distinguish "this
document is unusable" (which it must handle exactly as it handles a bad
document today) from "the service or the contract is broken" (which must never
degrade silently)?  The first is 400; the rest are 403/422/5xx.
"""

from __future__ import annotations

from fastapi import HTTPException


class DocumentFault(HTTPException):
    """400 — this document, or this page of it, cannot be served.

    The caller reproduces its existing per-site degradation: a pypdf fallback
    on ingest, a "no citation found" on the cite path.  It is NOT a service
    failure and must not be confused with one.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=400, detail={"error": "document", "detail": detail})


class PathNotAllowed(HTTPException):
    """403 — the path is outside PDF_SIDECAR_ALLOWED_ROOTS, or traverses out."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            status_code=403, detail={"error": "path_not_allowed", "detail": detail}
        )


class ContractFault(HTTPException):
    """422 — the request itself is malformed against this contract.

    Raised for the rules pydantic cannot express: "exactly one of these two
    fields", and an empty geometry list. Both are cases the engine would
    otherwise accept and answer wrongly rather than loudly.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=422, detail={"error": "contract", "detail": detail})


class LayoutCanaryTripped(HTTPException):
    """503 — the commercial layout parser became reachable after startup.

    Refusing the request is the only safe answer: serving it would run
    unlicensed code.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(
            status_code=503, detail={"error": "layout_canary", "detail": detail}
        )


def fault_detail(exc: BaseException) -> str:
    """Render an engine exception for a 400 body or an error row.

    Type name included because MuPDF's messages alone are often ambiguous
    ("cannot find page 2 in page tree" could be either kind of fault).
    """
    return f"{type(exc).__name__}: {exc}"
