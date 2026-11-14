"""/doc/page-words' render_modes (contract 5): one entry per word row, in order; the majority text
render mode of the traced characters in the word's box, or null when none."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from sidecar import engine


@pytest.fixture
def visible_and_hidden(root: Path) -> Path:
    path = root / "visible_and_hidden.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "VISIBLE LABEL", fontsize=12)                     # render mode 0
    page.insert_text((72, 200), "HIDDEN WORDS", fontsize=12, render_mode=3)       # invisible
    page.insert_text((72, 300), "OUTLINE", fontsize=12, render_mode=1)            # stroke only
    doc.save(str(path))
    doc.close()
    return path


def test_render_modes_run_parallel_to_words(client, visible_and_hidden):
    body = client.post("/doc/page-words", json={"path": str(visible_and_hidden), "page": 0}).json()
    words, modes = body["words"], body["render_modes"]
    assert len(words) == len(modes) == 5
    by_word = {w[4]: m for w, m in zip(words, modes)}
    assert by_word["VISIBLE"] == 0 and by_word["LABEL"] == 0
    assert by_word["HIDDEN"] == 3 and by_word["WORDS"] == 3
    assert by_word["OUTLINE"] == 1


def test_words_stay_verbatim_beside_the_modes(client, visible_and_hidden):
    words = client.post("/doc/page-words", json={"path": str(visible_and_hidden), "page": 0}).json()["words"]
    doc = pymupdf.open(str(visible_and_hidden))
    try:
        assert words == [list(w) for w in doc[0].get_text("words")]
    finally:
        doc.close()


def test_a_word_with_no_traced_character_is_null(visible_and_hidden, monkeypatch):
    doc = pymupdf.open(str(visible_and_hidden))
    try:
        monkeypatch.setattr(pymupdf.Page, "get_texttrace", lambda self: [])
        assert engine.page_word_render_modes(doc, 0) == [None] * 5
    finally:
        doc.close()


def test_an_empty_page_has_no_rows(root):
    path = root / "empty.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()
    d = pymupdf.open(str(path))
    try:
        assert engine.page_words(d, 0) == [] and engine.page_word_render_modes(d, 0) == []
    finally:
        d.close()
