"""pdf-sidecar — an arm's-length HTTP wrapper around unmodified PyMuPDF.

The service holds no application semantics: it returns MuPDF's raw shapes and
lets the caller decide what they mean.  See README.md for why it exists.
"""

# The service's own release version.  /health.version returns THIS and nothing
# else — no `git describe` at runtime.
#
# Release ritual: bump this constant -> commit -> tag `v<x.y.z>` on that commit.
# tests/test_service.py::test_version_constant_matches_the_git_tag asserts the
# constant matches the tag (leading `v` stripped) and skips, loudly and by
# name, when HEAD carries no tag. (There is no tests/test_version.py; this
# comment named one for three releases.)
SIDECAR_VERSION = "0.1.5"

# Wire-contract version.  Any wire-shape change — a field added, removed or
# retyped, or a status code changed — increments this in BOTH repos in one
# change set, and the app's deploy gate compares equality.
#
# 2: /doc/annotate takes an optional page_range and answers with the three
#    X-Pdf-Sidecar-Excerpt-*/Total-Pages headers on every response;
#    /doc/info gains ocg_count.
# 3: /doc/page-words-ocr; /health gains ocr.
SIDECAR_CONTRACT = 3
