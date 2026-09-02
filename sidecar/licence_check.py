"""Standalone pre-flight for the licence canary.

The service also asserts this at import, but under ``--workers N`` an import
failure is a respawn loop rather than a stop: the supervisor keeps starting
workers that keep dying, and systemd's ``Restart=always`` keeps restarting the
supervisor.  Running this as ``ExecStartPre`` fails the unit ONCE, cleanly, with
the reason in the journal.

    python -m sidecar.licence_check
"""

from __future__ import annotations

import sys

from .licence import LayoutLicenceError, assert_free_layout


def main() -> int:
    try:
        assert_free_layout()
    except LayoutLicenceError as exc:
        print(f"pdf-sidecar: {exc}", file=sys.stderr)
        return 1
    print("pdf-sidecar: licence canary clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
