"""Auto-place a callout text box where it doesn't obstruct the drawing.

Strategy: probe 8 compass directions around the anchor, render a small
grayscale clip of each candidate, and score by mean pixel brightness
(higher = whiter = more empty space). Penalize candidates that overlap any
rectangle in `occupied_rects` so two callouts in the same area don't stack.
"""

from __future__ import annotations

import math

import fitz


# 8 compass directions as (dx, dy) unit vectors in PDF user space (y down).
_DIRECTIONS: list[tuple[float, float]] = [
    (0.0, -1.0),    # N
    (0.707, -0.707),  # NE
    (1.0, 0.0),     # E
    (0.707, 0.707),   # SE
    (0.0, 1.0),     # S
    (-0.707, 0.707),  # SW
    (-1.0, 0.0),    # W
    (-0.707, -0.707), # NW
]


def _clamp_to_page(rect: fitz.Rect, page_rect: fitz.Rect) -> fitz.Rect:
    """Slide rect inside page_rect without resizing."""
    width = rect.width
    height = rect.height
    x0 = min(max(rect.x0, page_rect.x0), page_rect.x1 - width)
    y0 = min(max(rect.y0, page_rect.y0), page_rect.y1 - height)
    return fitz.Rect(x0, y0, x0 + width, y0 + height)


def _score_clip(page: fitz.Page, clip: fitz.Rect, dpi: int = 100) -> float:
    """Mean pixel brightness in [0, 255]. White = 255 = best."""
    pix = page.get_pixmap(clip=clip, dpi=dpi, colorspace=fitz.csGRAY, alpha=False)
    n = pix.width * pix.height
    if n == 0:
        return 0.0
    # pix.samples is bytes; sum and divide. ~9k pixels at 100 DPI for a 1.5x1 in box.
    return sum(pix.samples) / float(n)


def find_best_callout_position(
    page: fitz.Page,
    anchor_rect: fitz.Rect,
    text_box_size: tuple[float, float],
    occupied_rects: list[fitz.Rect],
    offset_in: float = 1.5,
    overlap_penalty: float = 80.0,
) -> fitz.Rect:
    """Return the best fitz.Rect for the callout text box.

    Args:
        page: the PDF page (used for rasterization + page bounds).
        anchor_rect: the matched text bbox; the leader line points to its center.
        text_box_size: (width_pt, height_pt) of the callout box to place.
        occupied_rects: callout rects already placed on this page in this run.
        offset_in: distance from anchor center to candidate box center, in inches.
        overlap_penalty: brightness points subtracted per overlapping rect.
    """
    page_rect = page.rect
    box_w, box_h = text_box_size
    anchor_cx = (anchor_rect.x0 + anchor_rect.x1) / 2.0
    anchor_cy = (anchor_rect.y0 + anchor_rect.y1) / 2.0
    offset_pt = offset_in * 72.0  # 1 in = 72 pt

    best_rect: fitz.Rect | None = None
    best_score = -math.inf

    for dx, dy in _DIRECTIONS:
        cx = anchor_cx + dx * offset_pt
        cy = anchor_cy + dy * offset_pt
        candidate = fitz.Rect(
            cx - box_w / 2.0,
            cy - box_h / 2.0,
            cx + box_w / 2.0,
            cy + box_h / 2.0,
        )
        candidate = _clamp_to_page(candidate, page_rect)

        score = _score_clip(page, candidate)
        for occ in occupied_rects:
            if candidate.intersects(occ):
                score -= overlap_penalty

        if score > best_score:
            best_score = score
            best_rect = candidate

    # Should always be set (8 directions tried), but satisfy the type checker.
    assert best_rect is not None
    return best_rect
