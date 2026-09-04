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

This wrapper is **AGPL-3.0-or-later**. See [LICENSE](LICENSE).

It depends on two works that are **dual licensed AGPL-3.0 / Artifex
Commercial**, and uses them under the AGPL:

| dependency | version | licence |
|---|---|---|
| PyMuPDF | 1.28.2 | AGPL-3.0 or Artifex Commercial |
| pymupdf4llm | 0.2.9 | AGPL-3.0 or Artifex Commercial |

No commercial licence is held for either, which is why this repository is
published in full and why `pymupdf-layout` — a third component under a
different, non-commercial licence — is refused outright (see below).

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

venv/bin/uvicorn sidecar.main:app --host 127.0.0.1 --port 8077 --workers 4 \
    --timeout-worker-healthcheck 300 --no-access-log
```

Binds loopback only. There is no authentication: the loopback bind and the path
allowlist are the boundary.

**`--timeout-worker-healthcheck` is not optional under `--workers`.** See
"The supervisor's health ping" below: at uvicorn's default of 5 s, a worker
saving a large document is killed mid-save.

## Configuration

| variable | meaning |
|---|---|
| `PORT` | listen port (loopback only) |
| `PDF_SIDECAR_WORKERS` | uvicorn worker processes; reported by `/health` |
| `PDF_SIDECAR_ALLOWED_ROOTS` | **JSON array of absolute paths.** Every path-valued request field is resolved and containment-checked against these roots. |
| `TESSDATA_PREFIX` | Directory holding Tesseract's `eng.traineddata`, for `/doc/page-words-ocr`. Unset or wrong → `/health.ocr` false; every other endpoint is unaffected. |

`PDF_SIDECAR_ALLOWED_ROOTS` is JSON rather than a separator-joined string
because a Windows path (`C:/dev/...`) would split on `:`. It is read by systemd's
`EnvironmentFile=` and by python-dotenv, and is **never shell-sourced** — the
JSON does not survive `export $(cat .env | xargs)`.

A missing or malformed value yields an **empty allowlist**, one ERROR log line,
and a service that starts and refuses every request with 403. That is
deliberate: refusing to boot would turn a configuration typo into a restart
loop on every dependent unit, whereas 403-with-a-reason is loud, safe and
diagnosable.

### OCR and `TESSDATA_PREFIX`

`TESSDATA_PREFIX` points **AT** the directory that holds the traineddata file,
not at its parent. Ship **`tessdata_fast`'s `eng.traineddata`**: measured
against the standard file it returns identical words (396 on a dense page) and
runs ~2.4× faster (3.5 s against 8.5 s), and this layer's accuracy bar is a
fuzzy word-window match against text the caller already has — it wants
geometry, not a text of record. The standard `eng.traineddata` is the
documented alternative if OCR quality ever turns out to be what a miss is
attributable to; it is a file swap and nothing else.

Capability is decided by **executing** a 1×1-page OCR once per worker at
startup, not by looking for the file: `pymupdf.get_tessdata()` reports the
directory MuPDF will use but does not validate it, and no tessdata at all, a
prefix pointing somewhere wrong, and a language whose file is missing are not
distinguishable from their errors alone (the last two are both
`FzErrorLibrary code=3`). The result is `/health.ocr`, and the false branch
logs the resolved directory **and** whether `eng.traineddata` is in it, which
is what actually tells the two apart.

## Endpoints

All are `POST` with a JSON body except `/health`. Every one takes an absolute
`path` that must resolve inside an allowlisted root.

| endpoint | returns |
|---|---|
| `GET /health` | service, engine and configuration facts |
| `/doc/info` | `{page_count, is_encrypted, needs_pass, ocg_count}` |
| `/doc/text-pages` | `{rows: [{page, text} \| {page, error}]}` for the requested pages |
| `/doc/search` | `{rows: [...]}`; page-major, needle-minor, stops at the first hit |
| `/doc/page-words` | `get_text("words")` rows, verbatim 8-element |
| `/doc/page-blocks` | text blocks as `{bbox, text}` |
| `/doc/page-words-ocr` | the same 8-element rows, from a **Tesseract** textpage — words on pages that carry no glyphs |
| `/doc/page-data` | a part-selectable per-page payload (text, blocks, dict blocks, tables, clusters, drawings, images, scan facts) |
| `/doc/render` | PNG bytes; a `get_pixmap` passthrough |
| `/doc/to-markdown` | `{markdown}` via pymupdf4llm's free legacy parser |
| `/doc/annotate` | annotated PDF bytes — the whole document, or a page window (below) |

`ocg_count` is the number of optional-content groups the document declares.
It is a count, not a verdict; see "Page-scoped excerpts" for what makes it
worth reporting.

### Page-scoped excerpts

`/doc/annotate` takes an optional `page_range: {start, end}` — inclusive,
0-based, in SOURCE page numbers. Given one, the service copies that window
into a fresh document, stamps the highlights on the copy (annotation `page`
values stay in source coordinates on the wire and are shifted here), and saves
that instead of the whole publication. On a 532-page source this is the
difference between an 18-second, 46 MB response and a millisecond-scale one.

Three headers ride on **every** `/doc/annotate` response, range or no range,
so a caller never has to branch on their presence:

| header | meaning |
|---|---|
| `X-Pdf-Sidecar-Excerpt-Start` | 0-based source page the output begins at |
| `X-Pdf-Sidecar-Excerpt-Count` | pages in the returned document |
| `X-Pdf-Sidecar-Total-Pages` | pages in the source document |

The range-less form answers `(0, n, n)`. `count == total` therefore means "you
are holding the whole document".

Four rules constrain a range. Three are decided from the request alone and
answer `422 {"error": "contract"}`: `start <= end`, at most
`MAX_PAGES_PER_REQUEST` pages, and every annotation page inside the window.
The fourth — `end` past the last page — cannot be, because the request does
not say how long the document is; it is checked after the document is opened
and answers 422 as well. It is a contract fault rather than a document fault
because the document is fine: `insert_pdf` would have CLAMPED such a range and
returned a plausible one-page excerpt for a window nobody asked for.

**Excerpts drop `/OCProperties`.** `insert_pdf` does not carry the
optional-content dictionary into the copy, so an excerpt of a document that
hides a layer renders that layer. Callers that care must read `ocg_count` from
`/doc/info` and keep such documents on the whole-document path — the service
reports the count and takes no view.

### Faults

| condition | status |
|---|---|
| this document, or this page of it, cannot be read | `400 {"error": "document"}` |
| path outside the allowlist, or traversing out of it | `403 {"error": "path_not_allowed"}` |
| the request violates this contract | `422 {"error": "contract"}` |
| the layout canary tripped | `503 {"error": "layout_canary"}` |
| the service itself failed — a bug, or a dependency that is not there (`/doc/page-words-ocr` with no working Tesseract) | `500 {"error": "internal"}` |

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

### The supervisor's health ping

uvicorn ≥ 0.30 runs `--workers N` under a supervisor that pings every worker
over a pipe each 0.5 s and **SIGKILLs any that does not answer within
`--timeout-worker-healthcheck` seconds** (default 5), logging only
`Child process [pid] died`. The answer comes from a Python thread, which needs
the GIL — and PyMuPDF holds the GIL for the whole of a long call. On the
2026-09-03 deployment every `/doc/annotate` on the two largest documents (a
532-page and a 35 MB PDF, 18 s and 84 s to save) killed its worker, and with
it every request in flight there. `--workers 1` runs no supervisor, which is
why a single-worker reproduction succeeds. Pass the flag with a value
comfortably above the longest single engine call you expect — the production
unit uses 300 s, its reverse proxy's own read timeout; `tests/test_supervisor.py`
pins the mechanism.

The save method is not a lever. v0.1.1 briefly switched `/doc/annotate` to
`Document.tobytes()` on a measured "3× speed-up" that was an artefact of
measuring a *second* save of the same `Document` — which is ~3× faster and a
few KB smaller whichever method runs second. On a fresh document `tobytes()`
and `save(BytesIO)` take the same 18 s and produce the same size; v0.1.2
restored the `save()` call the in-process code made. Only the flag keeps the
worker alive.

## Process-wide state pymupdf4llm mutates

`import pymupdf4llm` calls `pymupdf.TOOLS.unset_quad_corrections(True)` at
module level, and `to_markdown` reassigns `pymupdf.table.FLAGS` on every call
(dropping `TEXT_ACCURATE_BBOXES` from PyMuPDF's default). Both change what
`find_tables().extract()` returns, and three states are therefore possible:

| state | table cells | shipped as |
|---|---|---|
| import-only: `(True, PyMuPDF's FLAGS)` | spaces and dots lost — `VesselName:`, `wwwdatajmagojp` | v0.1.0 (193 of 669 prod documents) |
| PyMuPDF's defaults: `(False, PyMuPDF's FLAGS)` | underscores displaced — `c 1/okhotsk anl … _` | v0.1.1–v0.1.2 (50 of 669) |
| **baseline: `(True, to_markdown's FLAGS)`** | intact | v0.1.3 |

The baseline is the state a `to_markdown` call leaves behind, because that is
where the in-process code this service replaces spent nearly all of its life:
one long-lived worker imported pymupdf4llm at its first markdown extraction and
never changed back. `sidecar/engine_state.py` owns it — restored once at
import, right after pymupdf4llm is imported, and again after every
`to_markdown` call. `tests/test_engine_state.py` pins all three states on a
synthetic table and the real defect on a page of a real document.

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

**The caller's version pin and this checkout move together.** The application
pins `SIDECAR_CONTRACT` and `SIDECAR_VERSION` and asserts both at test-session
start, so the moment its pin moves, its whole PDF suite errors until this
checkout is moved to the matching tag — and the reverse, a sidecar rolled
forward under an unmoved pin, fails the same way. Move the pair, in that
order: tag the release here first, then move the pin there.
