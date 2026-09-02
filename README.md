# pdf-sidecar

A small HTTP service that wraps **unmodified [PyMuPDF](https://pymupdf.readthedocs.io/)**
and returns its raw output over loopback JSON.

It exists for one reason: PyMuPDF is licensed AGPL-3.0 (or a commercial licence
from Artifex). Running it in-process makes the calling application a derivative
work. Running it here — a separate program, in a separate process, talking over
a documented network interface, with this wrapper published under the AGPL —
keeps that obligation where it belongs: on this repository, which is published
in full.

This service holds **no application semantics**. It returns MuPDF's shapes as
MuPDF produces them: raw character counts rather than a "scanned page" verdict,
7-element block tuples rather than a tidied structure, page-major search order
rather than best-match order. Every threshold, label and decision belongs to
the caller. That is a deliberate design constraint, not an oversight — it is
what keeps this repository generic enough to publish.

## Licence

AGPL-3.0-or-later. See [LICENSE](LICENSE).

If you deploy a modified version of this service and let anyone interact with
it over a network, §13 of the AGPL requires you to offer them its source.

## The PyMuPDF-Layout landmine

**`pymupdf-layout` must never be installed in this venv.** It is licensed
Polyform-Noncommercial / Artifex, and no licence is held for it.

The hazard is that it does not announce itself. Importing `pymupdf.layout` —
directly, or indirectly through `pymupdf4llm.layout`, whose `__init__.py` is
the single line `import pymupdf.layout` — sets `pymupdf._get_layout`
**process-wide and import-order-sensitively**, which silently rebinds
`pymupdf4llm.to_markdown` to the commercial parser. Nothing errors. The output
just quietly comes from code this project is not licensed to run.

Three defences, in order of strength:

1. **The service refuses to start** unless `pymupdf._get_layout is None` and
   neither `pymupdf.layout` nor `pymupdf_layout` is importable
   (`sidecar/licence.py`).
2. **`/doc/to-markdown` re-checks the flag on every request** and refuses with
   503 if it has changed, because a stray import anywhere in the process can
   flip it after startup.
3. **The test suite greps this repository's own source** for every spelling of
   the forbidden import, and separately proves that grep would catch a planted
   offender — a guard that can only pass is not evidence.

`/health.layout_canary` reports the live value; deployment gates assert it is
`null`.

Note that `pymupdf4llm.layout` **ships with pymupdf4llm 0.2.9** on every clean
install, so its mere presence cannot be asserted against — and it cannot be
tested by importing it, because that import *is* the hazard. It is guarded by
never importing it, which is what defence 3 checks.

## Running it

```bash
python -m venv venv
venv/bin/pip install -r requirements.txt      # POSIX
# venv\Scripts\pip install -r requirements.txt    # Windows

cp .env.example .env        # then set PDF_SIDECAR_ALLOWED_ROOTS (see below)

venv/bin/uvicorn sidecar.main:app --host 127.0.0.1 --port 8077 --workers 4 --no-access-log
```

Binds loopback only. There is no authentication: the loopback bind and the path
allowlist are the boundary.

## Configuration

| variable | meaning |
|---|---|
| `PORT` | listen port (loopback only) |
| `PDF_SIDECAR_WORKERS` | uvicorn worker processes; reported by `/health` |
| `PDF_SIDECAR_ALLOWED_ROOTS` | **JSON array of absolute paths.** Every path-valued request field is resolved and containment-checked against these roots. |

`PDF_SIDECAR_ALLOWED_ROOTS` is JSON rather than a separator-joined string
because a Windows path (`C:/dev/...`) would split on `:`. It is read by systemd's
`EnvironmentFile=` and by python-dotenv, and is **never shell-sourced** — the
JSON does not survive `export $(cat .env | xargs)`.

A missing or malformed value yields an **empty allowlist**, one ERROR log line,
and a service that starts and refuses every request with 403. That is
deliberate: refusing to boot would turn a configuration typo into a restart
loop on every dependent unit, whereas 403-with-a-reason is loud, safe and
diagnosable.

## Endpoints

All are `POST` with a JSON body except `/health`. Every one takes an absolute
`path` that must resolve inside an allowlisted root.

| endpoint | returns |
|---|---|
| `GET /health` | service, engine and configuration facts |
| `/doc/info` | `{page_count, is_encrypted, needs_pass}` |
| `/doc/text-pages` | `{rows: [{page, text} \| {page, error}]}` for the requested pages |
| `/doc/search` | `{rows: [...]}`; page-major, needle-minor, stops at the first hit |
| `/doc/page-words` | `get_text("words")` rows, verbatim 8-element |
| `/doc/page-blocks` | text blocks as `{bbox, text}` |
| `/doc/page-data` | a part-selectable per-page payload (text, blocks, dict blocks, tables, clusters, drawings, images, scan facts) |
| `/doc/render` | PNG bytes; a `get_pixmap` passthrough |
| `/doc/to-markdown` | `{markdown}` via pymupdf4llm's free legacy parser |
| `/doc/annotate` | annotated PDF bytes |

### Faults

| condition | status |
|---|---|
| this document, or this page of it, cannot be read | `400 {"error": "document"}` |
| path outside the allowlist, or traversing out of it | `403 {"error": "path_not_allowed"}` |
| the request violates this contract | `422 {"error": "contract"}` |
| the layout canary tripped | `503 {"error": "layout_canary"}` |

The 400 class is the important one: it means *this document is unusable*, which
a caller should handle exactly as it already handles a bad document. Everything
else means *the service or the contract is broken*, which must never degrade
silently.

### Two things that look like bugs and are not

- **`/doc/page-blocks` and `/doc/page-data`'s `dict_blocks` use different text
  flags.** `page-blocks` passes `TEXT_PRESERVE_WHITESPACE` (2), which is
  *smaller* than the default (199) and excludes image blocks; `dict_blocks`
  uses the default and includes them. Both callers depend on their own form.
- **`/doc/page-data` returns a bare list while the two cite endpoints return
  `{"rows": ...}`.** The cite endpoints were re-wrapped when they gained
  per-page error rows; the ingestion payload was deliberately left alone.

## Concurrency

PyMuPDF is not thread-safe. Handlers are sync `def` (so FastAPI runs them off
the event loop) and every engine call is serialised by one per-process lock;
parallelism comes from uvicorn worker **processes**.

A read-only handle cache of 4 documents per worker avoids re-parsing across a
multi-call sequence. `/doc/annotate` and `/doc/to-markdown` never use it —
annotations accumulate on a reused handle, and `to_markdown` bakes the
document.

## Versioning

`sidecar/__init__.py` carries `SIDECAR_VERSION`, which `/health.version`
returns. To release: bump the constant, commit, then tag `v<x.y.z>` on that
commit. `tests/test_service.py::test_version_constant_matches_the_git_tag`
asserts the two agree, and skips with a named reason when HEAD carries no tag.

`SIDECAR_CONTRACT` is the wire-contract version. Any change to a shape or a
status code increments it, in this repo and in the caller, in one change set.

## Tests

```bash
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest
```
