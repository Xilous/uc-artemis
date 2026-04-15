"""Write Bluebeam-compatible callout annotations into a PDF copy.

PyMuPDF 1.24.x has no high-level set_callout method on Annot, so we build
the callout by adding a FreeText annotation and then writing the /IT, /CL,
and /LE entries directly into the PDF dictionary via xref_set_key. The /CL
array (the leader-line endpoints) must be in the same coordinate system as
the annotation's stored /Rect — which is PDF-native (origin from the
mediabox, y bottom-up), NOT PyMuPDF user space. We apply the inverse of
page.transformation_matrix to convert.

We also stash the full Excel row for each opening as a JSON blob under a
private dictionary key /UCArtemisMeta on the annotation. This is Path 1 of
the custom-columns story: data is preserved in the PDF even though Bluebeam
doesn't yet know how to surface it as Markups List columns. When we later
figure out Bluebeam's private-dict schema for custom columns (Path 2), a
follow-up script can walk /UCArtemisMeta across all annotations and rewrite
the values into Bluebeam's expected key. Nothing is ever lost.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import fitz


AUTHOR = "UC Artemis"
BORDER_RGB = (1.0, 0.0, 0.0)  # red
FILL_RGB = (1.0, 1.0, 1.0)    # white
TEXT_RGB = (0.0, 0.0, 0.0)    # black
TEXT_FONTSIZE = 10
BORDER_WIDTH = 1.0

# Default text-box dimensions (points). Width fits ~25 characters of 10pt text;
# height is 2 lines. Callers can override if a row's body text is unusually long.
DEFAULT_BOX_W_PT = 144.0  # 2.0 in
DEFAULT_BOX_H_PT = 36.0   # 0.5 in


PRIVATE_META_KEY = "UCArtemisMeta"


def _pdf_hex_string(s: str) -> str:
    """Encode a Python string as a PDF hex literal <hex> for xref_set_key.

    Hex-encoding is foolproof: no parenthesis/backslash escaping issues, no
    ambiguity with PDF operators, and Unicode is preserved by going through
    UTF-8 bytes first. The tradeoff is doubled size versus a literal string,
    but callout metadata is a few hundred bytes at most, so the overhead is
    negligible.
    """
    return "<" + s.encode("utf-8").hex() + ">"


@dataclass
class WrittenAnnot:
    """Record of a successfully written callout, used for the summary XML
    and for re-finding the annotation later (drag-to-reposition).
    """

    opening_number: str
    page_index: int
    page_label: str
    body_text: str
    text_box_rect: fitz.Rect
    anchor_point: tuple[float, float]
    created_at: datetime
    xref: int  # PDF object number; stable across incremental saves
    metadata: dict[str, str] = field(default_factory=dict)


def prepare_working_copy(input_pdf: str | Path, output_pdf: str | Path) -> Path:
    """Copy the input PDF to the output path so the original is never touched."""
    src = Path(input_pdf)
    dst = Path(output_pdf)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def open_working_copy(working_pdf: str | Path) -> fitz.Document:
    """Open the working PDF copy for in-place incremental annotation.

    If MuPDF flagged the file as "repaired" on open (e.g. because a prior
    session crashed mid-save and left dangling appended objects), the on-disk
    byte offsets no longer match the in-memory xref table and PyMuPDF will
    refuse any subsequent incremental save. Fix this proactively by doing a
    one-time clean rewrite (garbage=4 deduplicates and renumbers xrefs),
    then reopen. Callers that store xrefs across saves MUST refresh them
    after this function returns — see web/server.py:_restore_written_from_journal.
    """
    import gc
    import os as _os
    import time

    path = Path(working_pdf)
    doc = fitz.open(str(path))
    if not doc.is_repaired:
        return doc

    tmp = path.with_suffix(path.suffix + ".clean.tmp")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
    doc.save(str(tmp), garbage=4, deflate=True)
    doc.close()
    del doc
    gc.collect()

    # Windows holds the mmap briefly even after close+del+gc. Replace the
    # original via "unlink + rename" with a small retry; renaming directly
    # over an open mmap fails on Windows but unlink can succeed because
    # Windows allows delete-on-close semantics.
    last_err: Exception | None = None
    for attempt in range(10):
        try:
            if path.exists():
                path.unlink()
            _os.rename(str(tmp), str(path))
            last_err = None
            break
        except PermissionError as e:
            last_err = e
            time.sleep(0.1 * (attempt + 1))
    if last_err is not None:
        # Fall back to keeping the cleaned file at the .clean.tmp name and
        # opening that instead, so the user can still run.
        return fitz.open(str(tmp))

    return fitz.open(str(path))


def _user_to_pdf_pt(page: fitz.Page, x: float, y: float) -> tuple[float, float]:
    """Convert PyMuPDF user-space (top-down) coords to PDF-native (bottom-up).

    The /Rect entry of an annotation is stored in PDF-native coordinates
    (origin at the mediabox, y axis bottom-up). PyMuPDF translates user-space
    rects automatically when storing /Rect, but anything WE write directly
    via xref_set_key (like /CL) must be pre-converted ourselves, otherwise
    the leader line ends up in a different coordinate system from the box.
    """
    pt = fitz.Point(x, y) * ~page.transformation_matrix
    return pt.x, pt.y


def add_callout(
    page: fitz.Page,
    anchor_point: tuple[float, float],
    text_box_rect: fitz.Rect,
    body_text: str,
    opening_number: str,
    metadata: dict[str, str] | None = None,
) -> fitz.Annot:
    """Place a single callout markup on a page.

    The callout has a leader line from the text box edge to the anchor point.
    Metadata visible in Bluebeam's built-in Markups List columns:
      - /T (Author)   → always "UC Artemis"
      - /Subj         → opening number
      - /Contents     → body_text (the joined template string)

    The full Excel row is additionally stored under /UCArtemisMeta as a JSON
    blob. This preserves every column of the source Excel row inside the PDF
    even though Bluebeam's default UI doesn't show them as columns yet.
    """
    annot = page.add_freetext_annot(
        text_box_rect,
        body_text,
        fontsize=TEXT_FONTSIZE,
        fontname="helv",
        text_color=TEXT_RGB,
        border_color=BORDER_RGB,
        align=fitz.TEXT_ALIGN_LEFT,
    )

    annot.set_border(width=BORDER_WIDTH)
    annot.set_colors(stroke=BORDER_RGB)
    annot.set_info(
        title=AUTHOR,        # Bluebeam "Author" column
        subject=opening_number,
        content=body_text,
    )

    # Compute a sensible knee point on the side of the box closest to the anchor
    # so the leader line doesn't cut through the box itself.
    knee_x, knee_y = _knee_point(text_box_rect, anchor_point)
    ax, ay = _user_to_pdf_pt(page, *anchor_point)
    kx, ky = _user_to_pdf_pt(page, knee_x, knee_y)

    doc = page.parent
    doc.xref_set_key(annot.xref, "IT", "/FreeTextCallout")
    doc.xref_set_key(annot.xref, "CL", f"[{ax} {ay} {kx} {ky}]")
    doc.xref_set_key(annot.xref, "LE", "[/OpenArrow /None]")

    if metadata:
        blob = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        doc.xref_set_key(annot.xref, PRIVATE_META_KEY, _pdf_hex_string(blob))

    annot.update()
    return annot


def read_callout_metadata(doc: fitz.Document, xref: int) -> dict[str, str]:
    """Read back the /UCArtemisMeta blob from an annotation, if present.

    Returns an empty dict if the key is missing or unparseable — callers get
    a safe default and can check for emptiness.
    """
    try:
        raw = doc.xref_get_key(xref, PRIVATE_META_KEY)
    except Exception:
        return {}
    if not raw or raw[0] not in ("string", "hex-string"):
        return {}
    value = raw[1]
    # PyMuPDF returns hex strings as "<hex>" — strip the angle brackets.
    if value.startswith("<") and value.endswith(">"):
        try:
            blob = bytes.fromhex(value[1:-1]).decode("utf-8")
        except ValueError:
            return {}
    elif value.startswith("(") and value.endswith(")"):
        # PDF literal string — strip parens (basic unescape only)
        blob = value[1:-1].replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
    else:
        blob = value
    try:
        loaded = json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _knee_point(box: fitz.Rect, anchor: tuple[float, float]) -> tuple[float, float]:
    """Pick the midpoint of the box edge nearest the anchor."""
    ax, ay = anchor
    cx = (box.x0 + box.x1) / 2.0
    cy = (box.y0 + box.y1) / 2.0
    dx = ax - cx
    dy = ay - cy
    # Choose horizontal vs vertical edge by which delta is larger.
    if abs(dx) >= abs(dy):
        edge_x = box.x1 if dx > 0 else box.x0
        return (edge_x, cy)
    else:
        edge_y = box.y1 if dy > 0 else box.y0
        return (cx, edge_y)


def save_incremental(doc: fitz.Document) -> None:
    """Append new annotations to the working copy on disk.

    Incremental save is what makes multi-GB PDFs feasible: PyMuPDF appends
    only the new objects to the file rather than rewriting the whole stream.
    """
    doc.save(
        doc.name,
        incremental=True,
        encryption=fitz.PDF_ENCRYPT_KEEP,
    )


def find_annot_by_xref(page: fitz.Page, xref: int) -> fitz.Annot | None:
    """Walk a page's annotations and return the one with the given xref.

    O(n) over the page's annotation count, which is small in practice.
    """
    for annot in page.annots() or []:
        if annot.xref == xref:
            return annot
    return None


def update_callout_position(
    page: fitz.Page,
    xref: int,
    new_box_rect: fitz.Rect,
    anchor_point: tuple[float, float],
) -> bool:
    """Move an existing callout's text box to a new rect, redrawing the leader.

    The anchor point doesn't move — only the text box. The leader knee is
    recomputed against the new box edge nearest the anchor so the leader
    line still exits the box on the correct side.

    Returns True if the annotation was found and updated, False otherwise.
    """
    annot = find_annot_by_xref(page, xref)
    if annot is None:
        return False

    annot.set_rect(new_box_rect)

    knee_x, knee_y = _knee_point(new_box_rect, anchor_point)
    ax, ay = _user_to_pdf_pt(page, *anchor_point)
    kx, ky = _user_to_pdf_pt(page, knee_x, knee_y)

    doc = page.parent
    doc.xref_set_key(annot.xref, "CL", f"[{ax} {ay} {kx} {ky}]")
    annot.update()
    return True
