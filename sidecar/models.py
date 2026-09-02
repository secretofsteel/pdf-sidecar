"""Request models.

Validation here is the 422 half of the fault taxonomy: anything the engine
would mis-handle is refused before it reaches MuPDF, loudly, as a contract
fault rather than as a mystery 500 or — worse — a wrong answer.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# The client windows every multi-page request at 32 (SIDECAR_PAGE_WINDOW), and
# /doc/page-data's contract caps it explicitly.  Enforcing it server-side too
# makes an unbounded, minutes-long worker hold structurally impossible rather
# than merely conventional.
MAX_PAGES_PER_REQUEST = 32

# Negative indices are refused because pymupdf accepts them and wraps
# Python-style: doc[-1] silently returns the LAST page.  A client bug would
# then get a confident wrong answer instead of an error.
PageIndex = Annotated[int, Field(ge=0)]


class _Body(BaseModel):
    # Reject unknown fields: a caller sending a kwarg this contract does not
    # define has diverged from it, and silently ignoring the field is how a
    # contract drifts.
    model_config = ConfigDict(extra="forbid")


class DocBody(_Body):
    path: str = Field(min_length=1)


class PageBody(DocBody):
    page: PageIndex


class PagesBody(DocBody):
    pages: list[PageIndex] = Field(min_length=1, max_length=MAX_PAGES_PER_REQUEST)


class SearchBody(DocBody):
    needles: list[str] = Field(min_length=1)
    page_order: list[PageIndex] = Field(min_length=1, max_length=MAX_PAGES_PER_REQUEST)
    quads: bool = True


class PageDataBody(DocBody):
    pages: list[PageIndex] = Field(min_length=1, max_length=MAX_PAGES_PER_REQUEST)
    parts: list[
        Literal[
            "text", "blocks", "dict_blocks", "tables",
            "clusters", "drawings", "images", "scan",
        ]
    ] = Field(min_length=1)


class RenderBody(PageBody):
    clip: list[float] | None = Field(default=None, min_length=4, max_length=4)
    matrix: list[float] | None = Field(default=None, min_length=6, max_length=6)
    dpi: int | None = Field(default=None, gt=0)


class ToMarkdownBody(DocBody):
    """The eight kwargs the app passes today, typed and required.

    Strictness is not hygiene here: pymupdf4llm 0.2.9 SWALLOWS an unknown kwarg
    with only a stdout warning and returns silently different markdown, so a
    typo on the caller's side would otherwise be invisible.
    """

    write_images: bool
    image_path: str
    image_format: str
    dpi: int = Field(gt=0)
    image_size_limit: float
    force_text: bool
    table_strategy: str
    page_separators: bool


class Annotation(_Body):
    page: PageIndex
    # Flat 8 floats per quad, in ul, ur, ll, lr order.  Reconstructed as four
    # point pairs — pymupdf.Quad rejects a flat sequence of 8.
    quads: list[Annotated[list[float], Field(min_length=8, max_length=8)]] | None = None
    rects: list[Annotated[list[float], Field(min_length=4, max_length=4)]] | None = None
    color: Annotated[list[float], Field(min_length=3, max_length=3)] = [1.0, 0.92, 0.23]
    opacity: float = Field(default=0.4, ge=0.0, le=1.0)


class AnnotateBody(DocBody):
    # An EMPTY list is legal and must stay so: the caller re-saves the document
    # unconditionally after annotating, so every request whose anchors all
    # failed to resolve arrives here with nothing to highlight and still
    # expects a valid PDF back. Refusing it would turn "we could not locate
    # that citation" into a service error.
    annotations: list[Annotation]
    garbage: int = 3
    deflate: bool = True
