"""Lone UTF-16 surrogates out of every JSON answer.

MuPDF's per-character output (``rawdict`` / texttrace ``chars``) carries a
font's ToUnicode values one code unit at a time. A font that maps a glyph to
half of a surrogate pair — Cambria Math does this for its mathematical
alphanumerics, ``U+D835`` then ``U+DCxx`` on two glyphs — puts a lone surrogate
into a Python ``str``. Strings MuPDF assembles itself (``get_text`` "text",
"words", "dict", "blocks") already carry ``U+FFFD`` for each such unit, but
text built from the characters — ``find_tables`` cell text (``/doc/page-data``'s
``tables`` part) and ``pymupdf4llm.to_markdown`` — keeps the surrogates, and a
surrogate is not encodable as UTF-8: the JSON serialiser raised and the whole
request answered 500 (a real 91-page plan lost to one formula on page 84).

``utf8_safe`` gives each surrogate code unit the replacement character, which is
exactly what MuPDF's own text modes give it, so a table cell and the page text
around it agree. It is a passthrough for everything else, and ASCII strings
return on the first check. The service's rule — MuPDF's shapes as MuPDF
produces them — holds: no shape changes, only an unencodable value takes the
value MuPDF itself uses for it.
"""

from __future__ import annotations

import re
from typing import Any

REPLACEMENT = chr(0xFFFD)
_SURROGATE = re.compile("[" + chr(0xD800) + "-" + chr(0xDFFF) + "]")


def utf8_safe(value: Any) -> Any:
    """``value`` with every lone surrogate in every string replaced by U+FFFD.

    Recurses through dicts (values only — the keys are this service's own),
    lists and tuples (a tuple comes back a list, which is how JSON carries it
    anyway). Any other type is returned as it is.
    """
    if isinstance(value, str):
        if value.isascii():
            return value
        return _SURROGATE.sub(REPLACEMENT, value)
    if isinstance(value, dict):
        return {k: utf8_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [utf8_safe(v) for v in value]
    return value
