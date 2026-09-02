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


def test_health_has_exactly_the_eight_contract_fields(client):
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
    assert result.stdout.strip().lstrip("v") == SIDECAR_VERSION


@pytest.mark.parametrize(
    "outside",
    ["/etc/hosts", "/tmp/not-a-root/x.pdf"],
)
def test_paths_outside_the_roots_are_refused(client, outside):
    response = client.post("/doc/info", json={"path": outside})
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "path_not_allowed"


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
