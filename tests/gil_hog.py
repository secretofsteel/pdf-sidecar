"""A stand-in for one long PyMuPDF call: an endpoint that holds the GIL.

``tests/test_supervisor.py`` serves this app under uvicorn's multiprocess
supervisor.  A pure-Python busy loop with the switch interval raised past the
request's length never hands the GIL to any other thread — the same shape as
``Document.save`` on a large document, where the C call simply does not
release it.  The supervisor's pong thread starves either way.
"""

from __future__ import annotations

import sys
import time

from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/hog")
def hog(seconds: float = 3.0) -> dict[str, float]:
    previous = sys.getswitchinterval()
    sys.setswitchinterval(3600.0)  # the eval loop will not yield for an hour
    try:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            pass
    finally:
        sys.setswitchinterval(previous)
    return {"held": seconds}
