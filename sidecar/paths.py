"""Path resolution and containment (R12).

Every path-valued field on every endpoint goes through ``resolve_allowed``:
the document ``path`` on all of them, and ``/doc/to-markdown``'s ``image_path``,
which is strictly more dangerous because the service *writes* into it.
"""

from __future__ import annotations

from pathlib import Path

from .config import ALLOWED_ROOTS
from .errors import PathNotAllowed


def resolve_allowed(raw: str) -> Path:
    """Resolve ``raw`` and require it to sit inside an allowlisted root.

    Resolution happens before the check, so ``../`` traversal is normalised
    away and then fails containment like any other outside path.  An empty
    allowlist refuses everything — see config._load_allowed_roots.
    """
    try:
        candidate = Path(raw).resolve()
    except (OSError, ValueError) as exc:  # e.g. a NUL byte in the path
        raise PathNotAllowed(f"unresolvable path: {exc}") from exc

    for root in ALLOWED_ROOTS:
        if candidate == root or root in candidate.parents:
            return candidate

    # The rejected path is echoed back: this is a loopback service and the
    # caller supplied the path, so it learns nothing it did not already know.
    raise PathNotAllowed(str(candidate))
