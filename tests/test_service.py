"""Service-level contract: health, the licence canary, and path containment."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pymupdf
import pytest

from sidecar import SIDECAR_CONTRACT, SIDECAR_VERSION
from sidecar.licence import assert_free_layout, layout_canary

REPO = Path(__file__).resolve().parent.parent


def test_health_has_exactly_the_nine_contract_fields(client):
    body = client.get("/health").json()
    assert set(body) == {
        "status",
        "version",
        "contract",
        "pymupdf_version",
        "pymupdf4llm_version",
        "layout_canary",
        "workers",
        "allowed_roots",
        "ocr",
    }
    assert body["status"] == "ok"
    assert body["version"] == SIDECAR_VERSION
    assert body["contract"] == SIDECAR_CONTRACT
    assert body["pymupdf_version"] == pymupdf.__version__
    # The deploy gate asserts this is null; a non-null value means unlicensed
    # code is bound in the process serving the request.
    assert body["layout_canary"] is None
    assert isinstance(body["workers"], int)
    assert isinstance(body["allowed_roots"], list) and body["allowed_roots"]
    # The deploy gate prints this unconditionally and asserts it only at
    # contract >= 3, so what /health owes it is a boolean, always present.
    assert isinstance(body["ocr"], bool)


def test_health_reports_the_roots_it_actually_enforces(client, root):
    assert client.get("/health").json()["allowed_roots"] == [str(root)]


def test_licence_assert_passes_on_a_clean_venv():
    assert_free_layout()
    assert layout_canary() is None


def test_no_source_file_imports_a_layout_module():
    """The fourth leg the startup assert cannot cover.

    `pymupdf4llm.layout` ships on every clean install, so its absence cannot be
    asserted — it is guarded by never importing it. The pattern is statement-
    anchored so that prose in a comment, and the assert's own find_spec call,
    do not trip it.
    """
    pattern = re.compile(
        r"^[ \t]*(?:import|from)[^#\n]*pymupdf(?:4llm)?\.layout"
        r"|^[ \t]*from[ \t]+pymupdf(?:4llm)?[ \t]+import[^#\n]*\blayout\b"
        r"|(?:import_module|__import__)\([ \t]*['\"]pymupdf(?:4llm)?\.layout",
        re.MULTILINE,
    )
    offenders = [
        str(p.relative_to(REPO))
        for p in REPO.rglob("*.py")
        if "venv" not in p.parts and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"forbidden layout import in: {offenders}"


def test_the_licence_grep_would_actually_catch_an_offender(tmp_path):
    """A guard that can only pass is not evidence.

    Plants each spelling the pattern must catch, and each comment-only form it
    must not, and asserts the verdicts.
    """
    pattern = re.compile(
        r"^[ \t]*(?:import|from)[^#\n]*pymupdf(?:4llm)?\.layout"
        r"|^[ \t]*from[ \t]+pymupdf(?:4llm)?[ \t]+import[^#\n]*\blayout\b"
        r"|(?:import_module|__import__)\([ \t]*['\"]pymupdf(?:4llm)?\.layout",
        re.MULTILINE,
    )
    # Assembled at runtime rather than written literally: this file is itself
    # inside the scan surface, and a literal sample here would make the
    # repo-wide guard above fail on its own positive control.
    mod = "pymupdf"
    mod4 = "pymupdf4llm"
    layout = "layout"
    must_catch = [
        f"import {mod}.{layout}",
        f"import {mod4}.{layout}",
        f"from {mod}.{layout} import x",
        f"from {mod4}.{layout} import x",
        f"from {mod} import {layout}",
        f"from {mod4} import {layout}",
        f"from {mod} import {layout} as L",
        f"import os, {mod}.{layout}",
        f"importlib.import_module('{mod}.{layout}')",
        f'__import__("{mod4}.{layout}")',
    ]
    must_not_catch = [
        f"# never import {mod}.{layout} anywhere",
        f'find_spec("{mod}.{layout}")',
        f'"""Talks about {mod}.{layout} in prose."""',
    ]
    assert [s for s in must_catch if not pattern.search(s)] == []
    assert [s for s in must_not_catch if pattern.search(s)] == []


def test_version_constant_matches_the_git_tag():
    """The release ritual's only enforcement point.

    Skips loudly and by name when HEAD carries no tag — `git describe` exits
    128 there, and treating a non-zero exit as "fine" is how a version gate
    silently stops gating.
    """
    result = subprocess.run(
        ["git", "describe", "--tags", "--exact-match"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(
            "HEAD carries no exact tag "
            f"(git describe exited {result.returncode}: "
            f"{result.stderr.strip()}) — run after tagging the release"
        )
    tag = result.stdout.strip()
    # The tag carries a leading `v` and the constant does not — the one
    # off-by-a-character this assertion exists to catch.
    assert tag.startswith("v"), f"release tags are v-prefixed, got {tag!r}"
    assert tag[1:] == SIDECAR_VERSION


@pytest.mark.parametrize(
    "outside",
    ["/etc/hosts", "/tmp/not-a-root/x.pdf"],
)
def test_paths_outside_the_roots_are_refused(client, outside):
    response = client.post("/doc/info", json={"path": outside})
    assert response.status_code == 403
    assert response.json()["error"] == "path_not_allowed"


def test_traversal_out_of_a_root_is_refused(client, root):
    response = client.post(
        "/doc/info", json={"path": str(root / ".." / ".." / "etc" / "hosts")}
    )
    assert response.status_code == 403


def test_to_markdown_image_path_is_containment_checked(client, two_page):
    """image_path is written into, so it is the more dangerous of the two."""
    response = client.post(
        "/doc/to-markdown",
        json={
            "path": str(two_page),
            "write_images": True,
            "image_path": "/etc",
            "image_format": "png",
            "dpi": 200,
            "image_size_limit": 0.05,
            "force_text": True,
            "table_strategy": "lines",
            "page_separators": False,
        },
    )
    assert response.status_code == 403


def test_handle_cache_reuses_and_invalidates_on_mtime(client, root):
    from sidecar.handles import FITZ_LOCK, cached_document

    path = root / "cache_probe.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 96), "one")
    doc.save(str(path))
    doc.close()

    with FITZ_LOCK:
        first = cached_document(path)
        assert cached_document(path) is first

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 96), "two")
    doc.save(str(path))
    doc.close()

    with FITZ_LOCK:
        assert cached_document(path) is not first


def test_handle_cache_is_bounded(client, root):
    from sidecar.handles import FITZ_LOCK, cache_size, cached_document

    for index in range(6):
        path = root / f"bound_{index}.pdf"
        doc = pymupdf.open()
        doc.new_page()
        doc.save(str(path))
        doc.close()
        with FITZ_LOCK:
            cached_document(path)
    assert cache_size() <= 4


def test_the_licence_grep_catches_a_planted_file(tmp_path, monkeypatch):
    """Exercise the collection step, not just the pattern.

    The pattern test above proves the regex matches the right strings; this
    proves the scan actually visits repository files and reports them, so a
    mistyped glob cannot pass as "no offenders".
    """
    planted = REPO / "sidecar" / "_planted_offender.py"
    forbidden = "import " + "pymupdf" + "." + "layout"
    planted.write_text(f"{forbidden}\n", encoding="utf-8")
    try:
        pattern = re.compile(
            r"^[ \t]*(?:import|from)[^#\n]*pymupdf(?:4llm)?\.layout"
            r"|^[ \t]*from[ \t]+pymupdf(?:4llm)?[ \t]+import[^#\n]*\blayout\b"
            r"|(?:import_module|__import__)\([ \t]*['\"]pymupdf(?:4llm)?\.layout",
            re.MULTILINE,
        )
        offenders = [
            str(p.relative_to(REPO))
            for p in REPO.rglob("*.py")
            if "venv" not in p.parts and pattern.search(p.read_text(encoding="utf-8"))
        ]
        assert str(planted.relative_to(REPO)) in offenders
    finally:
        planted.unlink()


def test_boot_assert_refuses_each_failing_leg(monkeypatch):
    """The one thing allowed to refuse startup, proved to actually refuse."""
    import importlib.util

    from sidecar.licence import LayoutLicenceError

    monkeypatch.setattr(pymupdf, "_get_layout", object())
    with pytest.raises(LayoutLicenceError, match="_get_layout"):
        assert_free_layout()
    monkeypatch.undo()

    real_find_spec = importlib.util.find_spec
    for module in ("pymupdf.layout", "pymupdf_layout"):
        monkeypatch.setattr(
            importlib.util,
            "find_spec",
            lambda name, *a, _t=module, **kw: (
                object() if name == _t else real_find_spec(name, *a, **kw)
            ),
        )
        with pytest.raises(LayoutLicenceError, match=module):
            assert_free_layout()
        monkeypatch.undo()


def test_to_markdown_refuses_when_the_canary_trips(client, two_page, root, caplog):
    """The per-request gate — the one that catches a post-startup import."""
    import sidecar.main as main

    monkeypatch_value = "commercial-parser"
    original = main.layout_canary
    main.layout_canary = lambda: monkeypatch_value
    try:
        with caplog.at_level("ERROR"):
            response = client.post(
                "/doc/to-markdown",
                json={
                    "path": str(two_page),
                    "write_images": False,
                    "image_path": str(root),
                    "image_format": "png",
                    "dpi": 200,
                    "image_size_limit": 0.05,
                    "force_text": True,
                    "table_strategy": "lines",
                    "page_separators": False,
                },
            )
    finally:
        main.layout_canary = original

    assert response.status_code == 503
    assert response.json()["error"] == "layout_canary"
    assert any(monkeypatch_value in r.getMessage() for r in caplog.records)


def test_health_reports_a_tripped_canary(client, monkeypatch):
    monkeypatch.setattr(pymupdf, "_get_layout", "commercial")
    assert client.get("/health").json()["layout_canary"] is not None


def test_a_symlink_out_of_the_root_is_refused(client, root, tmp_path):
    """Containment is checked after resolution, so a link cannot smuggle."""
    outside = tmp_path / "outside.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(outside))
    doc.close()

    link = root / "escape.pdf"
    link.symlink_to(outside)
    try:
        response = client.post("/doc/info", json={"path": str(link)})
        assert response.status_code == 403
    finally:
        link.unlink()


def test_a_root_reached_through_a_symlink_still_works(client, root, tmp_path):
    """The positive half: resolution must not lock legitimate callers out.

    Temp directories are commonly symlinked, so resolving only one side would
    turn every test — and every dev box — into a 403.
    """
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 96), "via link")
    target = root / "linked_target.pdf"
    doc.save(str(target))
    doc.close()

    link_dir = tmp_path / "root_link"
    link_dir.symlink_to(root)
    try:
        response = client.post(
            "/doc/info", json={"path": str(link_dir / "linked_target.pdf")}
        )
        assert response.status_code == 200
    finally:
        link_dir.unlink()


def test_an_unresolvable_path_is_a_path_fault_not_a_crash(client, root):
    """A symlink loop raises RuntimeError, which must not surface as a 500."""
    loop = root / "loop_a"
    other = root / "loop_b"
    loop.symlink_to(other)
    other.symlink_to(loop)
    try:
        response = client.post("/doc/info", json={"path": str(loop / "x.pdf")})
        assert response.status_code == 403
        assert response.json()["error"] == "path_not_allowed"
    finally:
        loop.unlink()
        other.unlink()


def test_an_unexpected_error_is_json_not_plain_text(two_page, monkeypatch):
    """A service bug must be distinguishable from a bad document.

    The caller degrades quietly on 400 and must not do so on a 500, so the two
    cannot share a shape — or a content type. The client is built with
    raise_server_exceptions=False so it reports what a real HTTP caller would
    see rather than re-raising into the test.
    """
    from fastapi.testclient import TestClient

    import sidecar.main as main

    def boom(*a, **kw):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(main.engine, "info", boom)
    with TestClient(main.app, raise_server_exceptions=False) as unwrapped:
        response = unwrapped.post("/doc/info", json={"path": str(two_page)})
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"] == "internal"


def test_validation_failures_use_the_contract_body(client, two_page):
    response = client.post("/doc/text-pages", json={"path": str(two_page)})
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "contract"
    assert isinstance(body["detail"], str) and body["detail"]
