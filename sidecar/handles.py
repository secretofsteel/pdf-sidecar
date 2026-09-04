"""Engine serialization and the read-only document-handle cache.

Two invariants live here, and both were established by measurement rather than
by reading the MuPDF docs:

1. **PyMuPDF is not thread-safe.**  FastAPI runs sync ``def`` handlers in
   anyio's worker-thread pool, whose default capacity is 40 — so without a
   lock a single worker process would run up to 40 concurrent engine calls.
   Every engine operation is therefore taken under ``FITZ_LOCK``, and
   parallelism comes from uvicorn worker *processes* instead.

2. **Handles accumulate mutations.**  Three highlight-and-save cycles on one
   cached handle yield annotation counts [1, 2, 3]; on fresh handles, [1, 1, 1].
   So the cache serves read-only endpoints only, and the mutating pair
   (``/doc/annotate``, ``/doc/to-markdown``, the latter because
   ``to_markdown`` calls ``doc.bake()``) opens private handles per request.
"""

from __future__ import annotations

import functools
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path

import pymupdf

from . import engine
from .errors import DocumentFault, fault_detail

LOGGER = logging.getLogger("sidecar.handles")

# One per worker process.  Held around EVERY engine call, including the cache's
# own eviction/close, which is why a functools.lru_cache would not do.
FITZ_LOCK = threading.Lock()

# Small on purpose: 4 workers x 4 handles is already up to 16 open documents,
# each holding MuPDF's parsed page tree.
_MAX_HANDLES = 4
_cache: "OrderedDict[tuple[str, int, int], pymupdf.Document]" = OrderedDict()


def _key(path: Path) -> tuple[str, int, int]:
    """Identity of a document *version*: rewriting the file invalidates it."""
    stat = os.stat(path)
    return (str(path), stat.st_mtime_ns, stat.st_size)


def open_document(path: Path) -> pymupdf.Document:
    """Open a document with no caching — the caller owns closing it.

    Used by the mutating endpoints.  Call under FITZ_LOCK.
    """
    try:
        # Positional, with no filetype= hint, exactly as the in-process callers
        # do today.  Passing filetype="pdf" would change which documents fail.
        return pymupdf.open(str(path))
    except Exception as exc:
        raise DocumentFault(fault_detail(exc)) from exc


def cached_document(path: Path) -> pymupdf.Document:
    """Return a cached read-only handle for ``path``, opening one if needed.

    Call under FITZ_LOCK.  The returned handle stays owned by the cache — do
    not close it.
    """
    try:
        key = _key(path)
    except OSError as exc:  # missing file, unreadable directory
        raise DocumentFault(fault_detail(exc)) from exc

    hit = _cache.get(key)
    if hit is not None:
        _cache.move_to_end(key)
        return hit

    doc = open_document(path)
    _cache[key] = doc
    _cache.move_to_end(key)
    while len(_cache) > _MAX_HANDLES:
        _, evicted = _cache.popitem(last=False)
        try:
            evicted.close()
        except Exception:  # a close failure must never fail the request
            LOGGER.warning("failed to close evicted handle", exc_info=True)
    return doc


def clear_cache() -> None:
    """Drop every cached handle.  Tests use this; the service does not."""
    with FITZ_LOCK:
        while _cache:
            _, doc = _cache.popitem()
            try:
                doc.close()
            except Exception:
                LOGGER.warning("failed to close handle during clear", exc_info=True)


def cache_size() -> int:
    return len(_cache)


# ------------------------------------------------------------ the OCR words cache
#
# One page of OCR costs 0.5-3.5 s of HELD GIL, and one citation routinely puts
# several of its anchors on the same page, so the second ask must not pay for
# it again.  ``functools.lru_cache`` is right here and wrong for the handle
# cache above, and the difference is closability: what this holds is a list of
# tuples with no ``close()`` to get wrong at eviction, whereas a Document
# evicted outside FITZ_LOCK is a native-code hazard.
#
# Per worker, like everything else in this module.
_OCR_CACHE_SIZE = 32


@functools.lru_cache(maxsize=_OCR_CACHE_SIZE)
def _ocr_words(
    version: tuple[str, int, int], path: str, page: int, dpi: int, language: str
) -> list[list]:
    """The cached body.  ``version`` is unread on purpose.

    It is in the signature so that rewriting the file MISSES — exactly as it
    invalidates a document handle above — and nowhere in the body because the
    path is what opens the document.
    """
    return engine.page_words_ocr(cached_document(Path(path)), page, dpi, language)


def ocr_words(path: Path, page: int, dpi: int, language: str) -> list[list]:
    """Cached Tesseract words for one page.  Call under FITZ_LOCK.

    The returned list is SHARED with the cache: a caller that mutates it
    poisons every later hit.  Nothing does — it is serialised straight to
    JSON — and copying a 400-row word list on every hit would spend most of
    what the cache saves.
    """
    try:
        version = _key(path)
    except OSError as exc:
        # A file deleted between the allowlist check and here. Without this the
        # bare os.stat would 500 where the fault taxonomy says 400.
        raise DocumentFault(fault_detail(exc)) from exc
    return _ocr_words(version, str(path), page, dpi, language)


def ocr_cache_info() -> functools._CacheInfo:
    """Hits/misses — the only way to prove the cache is a cache."""
    return _ocr_words.cache_info()


def clear_ocr_cache() -> None:
    """Drop every cached word list.  Tests use this; the service does not."""
    _ocr_words.cache_clear()
