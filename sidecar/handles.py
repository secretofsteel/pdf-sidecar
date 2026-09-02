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

import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path

import pymupdf

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
