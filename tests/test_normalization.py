"""Round-trip tests for the search-string normalization.

The normalize() function must produce identical output for inputs that differ
only in whitespace or dash style, because architectural drawings inconsistently
use em-dashes, en-dashes, hyphens, and various spacing.
"""

from __future__ import annotations

import pytest

from core.pdf_search import normalize


@pytest.mark.parametrize(
    "left,right",
    [
        ("H.2103-01", "H.2103\u201101"),       # ASCII hyphen vs non-breaking hyphen
        ("H.2103-01", "H.2103\u201301"),       # ASCII hyphen vs en-dash
        ("H.2103-01", "H.2103\u201401"),       # ASCII hyphen vs em-dash
        ("H.2103-01", "H.2103\u201201"),       # ASCII hyphen vs figure dash
        ("H.2103-01", "H.2103\u221201"),       # ASCII hyphen vs minus sign
        ("H 2103 01", "H  2103   01"),         # collapsed runs of spaces
        ("H 2103 01", "H\u00a02103\u00a001"),  # NBSP vs space
        ("H.2103-01", "  H.2103-01  "),        # leading / trailing whitespace
    ],
)
def test_normalize_equivalence(left: str, right: str) -> None:
    assert normalize(left) == normalize(right)


def test_normalize_preserves_meaningful_content() -> None:
    assert normalize("H.2103-01") == "H.2103-01"
    assert normalize("Door 5073C.02") == "Door 5073C.02"


def test_normalize_empty() -> None:
    assert normalize("") == ""
    assert normalize("   ") == ""
    assert normalize(None) == ""  # type: ignore[arg-type]
