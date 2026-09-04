"""pdf-sidecar — an arm's-length HTTP wrapper around unmodified PyMuPDF.

The service holds no application semantics: it returns MuPDF's raw shapes and
lets the caller decide what they mean.  See README.md for why it exists.
"""

# The service's own release version.  /health.version returns THIS and nothing
# else — no `git describe` at runtime.
#
# Release ritual: bump this constant -> commit -> tag `v<x.y.z>` on that commit.
# tests/test_version.py asserts the constant matches the tag (leading `v`
# stripped) and skips, loudly and by name, when HEAD carries no tag.
SIDECAR_VERSION = "0.1.1"

# Wire-contract version.  Any wire-shape change — a field added, removed or
# retyped, or a status code changed — increments this in BOTH repos in one
# change set, and the app's deploy gate compares equality.
SIDECAR_CONTRACT = 1
