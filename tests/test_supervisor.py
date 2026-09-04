"""uvicorn's supervisor SIGKILLs a worker that misses its health ping.

Found on prod 2026-09-03: ``/doc/annotate`` on the two largest documents
"killed the worker" — no traceback, no OOM, no kernel line, just uvicorn's
``Child process [pid] died``.  The cause is uvicorn ≥ 0.30's multiprocess
supervisor (``uvicorn/supervisors/multiprocess.py``): every 0.5 s it pings
each child over a pipe and, if the child's pong thread does not answer within
``--timeout-worker-healthcheck`` (default 5 s), calls ``process.kill()`` and
respawns it.  A pong thread is Python, so it needs the GIL — and PyMuPDF's
``Document.save`` holds the GIL for its whole duration (measured: 18.2 s of an
18.8 s save).  ``--workers 1`` runs no supervisor at all, which is why a
single-worker reproduction succeeds and a two-worker one dies at 5.6 s.

These two tests pin the mechanism so a uvicorn bump that changes it is
noticed, and document why the production unit passes the flag.  They serve
``tests/gil_hog.py`` rather than the sidecar: the sidecar needs a multi-
megabyte document to hold the GIL that long, and the hazard is uvicorn's,
not PyMuPDF's.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _kill_tree(proc: subprocess.Popen) -> None:
    """The supervisor's workers are separate processes; reap all of them."""
    if proc.poll() is None:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True
            )
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


def _serve_and_hog(healthcheck_timeout: int, hold_seconds: float) -> tuple[str, str]:
    """Run gil_hog under a two-worker supervisor, hit /hog, return
    (outcome, supervisor log).  outcome is "200" or the error's class name."""
    port = _free_port()
    popen_kwargs = {} if sys.platform == "win32" else {"start_new_session": True}
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "tests.gil_hog:app",
            "--host", "127.0.0.1", "--port", str(port),
            "--workers", "2",
            "--timeout-worker-healthcheck", str(healthcheck_timeout),
            "--no-access-log",
        ],
        cwd=str(REPO),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **popen_kwargs,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 60
        while True:
            if proc.poll() is not None:
                out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
                raise RuntimeError(f"uvicorn exited at startup ({proc.returncode}):\n{out}")
            try:
                urllib.request.urlopen(f"{base}/health", timeout=1).read()
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(0.2)
        try:
            with urllib.request.urlopen(
                f"{base}/hog?seconds={hold_seconds}", timeout=hold_seconds + 30
            ) as response:
                outcome = str(response.status)
        except urllib.error.URLError as exc:
            outcome = type(exc.reason).__name__ if exc.reason is not None else type(exc).__name__
        except Exception as exc:  # a reset connection surfaces as the raw OSError
            outcome = type(exc).__name__
        # Give the supervisor a moment to log the death and respawn.
        time.sleep(1.5)
    finally:
        _kill_tree(proc)
    log = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
    return outcome, log


def test_a_gil_bound_worker_is_killed_when_the_ping_times_out():
    outcome, log = _serve_and_hog(healthcheck_timeout=1, hold_seconds=3.0)
    assert outcome != "200", f"the request survived a 1 s health-check timeout:\n{log}"
    assert "died" in log, f"uvicorn did not report the worker's death:\n{log}"


def test_the_same_request_survives_when_the_timeout_covers_the_call():
    outcome, log = _serve_and_hog(healthcheck_timeout=30, hold_seconds=3.0)
    assert outcome == "200", f"the request failed under a 30 s health-check timeout:\n{log}"
    assert "died" not in log, f"a worker died under a 30 s health-check timeout:\n{log}"
