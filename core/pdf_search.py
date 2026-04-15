"""PDF text search with punctuation/whitespace normalization.

Performance note: PyMuPDF re-parses page text on every search_for call. For a
batch workflow over thousands of openings against the same PDF, that's
prohibitively slow (~200 ms per page × N openings × M pages). The SearchIndex
class walks every page once at start-of-run, caches a per-page normalized
word list with bounding rects, and serves all subsequent lookups from memory.
First opening pays the full extraction cost; every opening after is O(words).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import fitz  # PyMuPDF


# Em-dash, en-dash, figure dash, minus sign, non-breaking hyphen → ASCII hyphen.
_DASH_TRANSLATE = str.maketrans({
    "\u2013": "-",  # en dash
    "\u2014": "-",  # em dash
    "\u2012": "-",  # figure dash
    "\u2212": "-",  # minus sign
    "\u2011": "-",  # non-breaking hyphen
    "\u00a0": " ",  # non-breaking space
})

_WS_RUN = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Canonicalize a string for lenient text matching.

    Applied to BOTH the search query and the PDF-extracted text so that an
    em-dash in the drawing matches a hyphen in the Excel and vice versa.
    """
    if not text:
        return ""
    s = text.translate(_DASH_TRANSLATE)
    s = _WS_RUN.sub(" ", s)
    return s.strip()


@dataclass(frozen=True)
class Match:
    """A single hit of an opening number on a PDF page."""

    page_index: int
    page_label: str
    rect: fitz.Rect

    @property
    def center(self) -> tuple[float, float]:
        return ((self.rect.x0 + self.rect.x1) / 2.0, (self.rect.y0 + self.rect.y1) / 2.0)


def _safe_label(page: fitz.Page, page_index: int) -> str:
    """Return the Bluebeam page label, falling back to '<index+1>'."""
    try:
        label = page.get_label()  # type: ignore[attr-defined]
    except Exception:
        label = ""
    return label or str(page_index + 1)


@dataclass(frozen=True)
class _CachedWord:
    rect: fitz.Rect
    normalized: str


class SearchIndex:
    """Pre-built per-page normalized word index.

    Build once per run (`SearchIndex.build(doc)`), then call
    `find(opening_number)` repeatedly. Memory cost is one tuple per word in
    the PDF; for a large architectural drawing set, that's a few MB.
    """

    def __init__(
        self,
        page_labels: list[str],
        page_words: list[list[_CachedWord]],
    ) -> None:
        self._page_labels = page_labels
        self._page_words = page_words

    @classmethod
    def build(cls, doc: fitz.Document) -> "SearchIndex":
        page_labels: list[str] = []
        page_words: list[list[_CachedWord]] = []
        for page_index in range(doc.page_count):
            page = doc.load_page(page_index)
            page_labels.append(_safe_label(page, page_index))
            cached: list[_CachedWord] = []
            for w in page.get_text("words") or []:
                x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
                norm = normalize(text)
                if norm:
                    cached.append(_CachedWord(fitz.Rect(x0, y0, x1, y1), norm))
            page_words.append(cached)
        return cls(page_labels, page_words)

    @property
    def page_count(self) -> int:
        return len(self._page_words)

    def page_label(self, page_index: int) -> str:
        return self._page_labels[page_index]

    def find(self, opening_number: str) -> list[Match]:
        """Substring match on the normalized form, against every cached word.

        Substring match (rather than exact equality) catches the common case of
        an opening number embedded in a longer label like "Door H.2103-01-A".
        """
        needle = normalize(opening_number)
        if not needle:
            return []
        matches: list[Match] = []
        for page_index, words in enumerate(self._page_words):
            label = self._page_labels[page_index]
            for w in words:
                if needle in w.normalized:
                    matches.append(Match(page_index, label, w.rect))
        return matches


def search_opening_in_pdf(doc: fitz.Document, opening_number: str) -> list[Match]:
    """Convenience wrapper that builds a fresh index for a single search.

    Real callers should build a SearchIndex once and call .find() repeatedly;
    this helper exists only for tests and ad-hoc use.
    """
    return SearchIndex.build(doc).find(opening_number)
