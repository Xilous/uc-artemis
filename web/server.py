"""Flask app: upload → template → review SPA → done.

The review screen is now a single-page app driven by /api/* JSON endpoints.
PDF.js renders the floor plan client-side; we never serve PNG previews.
All callout state (current draft and previously-placed) lives on a SVG
overlay the client controls; PyMuPDF's annotations are configured to be
ignored by PDF.js (annotationMode: DISABLE) so incremental saves don't
require a PDF.js reload.
"""

from __future__ import annotations

import csv
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import fitz
from flask import (
    Flask,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.utils import redirect

from core.excel_loader import (
    REQUIRED_FIRST_COLUMN,
    ExcelValidationError,
    join_template,
    load_headers_and_rows,
    metadata_columns,
)
from core.journal import CompletedEntry, Journal, hash_file
from core.pdf_search import Match, SearchIndex
from core.pdf_writer import (
    DEFAULT_BOX_H_PT,
    DEFAULT_BOX_W_PT,
    WrittenAnnot,
    add_callout,
    open_working_copy,
    prepare_working_copy,
    save_incremental,
    update_callout_position,
)
from core.summary_xml import write_summary
from core.whitespace import find_best_callout_position


@dataclass
class CurrentReview:
    """One Excel row's search outcome plus the cycling match cursor."""

    opening_number: str
    row: dict[str, str]
    matches: list[Match]   # may be empty for zero-match openings
    match_index: int = 0   # 0-based; cycles via /api/next


@dataclass
class AppState:
    input_pdf_path: Path | None = None
    input_excel_path: Path | None = None
    output_pdf_path: Path | None = None
    output_xml_path: Path | None = None
    output_csv_path: Path | None = None

    headers: list[str] = field(default_factory=list)
    rows: list[dict[str, str]] = field(default_factory=list)
    template_columns: list[str] = field(default_factory=list)

    journal: Journal | None = None
    doc: fitz.Document | None = None  # working PDF copy (annotated in place)
    search_index: SearchIndex | None = None  # built once at start-of-run

    # Producer/consumer
    pending_queue: "queue.Queue[CurrentReview]" = field(
        default_factory=lambda: queue.Queue(maxsize=8)
    )
    worker_thread: threading.Thread | None = None
    worker_started: bool = False
    worker_done: bool = False

    # Currently-presented row (consumed by the review SPA)
    current: CurrentReview | None = None

    # Per-page registry of placed callout boxes for the whitespace probe overlap penalty.
    occupied_by_page: dict[int, list[fitz.Rect]] = field(default_factory=dict)

    # Successfully written annotations, keyed by xref for fast lookup on update.
    written: list[WrittenAnnot] = field(default_factory=list)

    lock: threading.Lock = field(default_factory=threading.Lock)


STATE = AppState()


# ============================================================================
# Producer thread
# ============================================================================


def _search_worker(state: AppState) -> None:
    """Walk Excel rows, search the PDF for each, push results to the queue.

    Skips rows already processed (per the journal). Builds the SearchIndex
    once on first call so per-row search is O(words) instead of O(pages × extract).

    Zero-match rows are NOT auto-skipped here — they're pushed to the queue
    with empty matches so the user sees the "no matches" screen and has to
    click Skip explicitly.
    """
    assert state.doc is not None and state.journal is not None
    if state.search_index is None:
        state.search_index = SearchIndex.build(state.doc)
    for row in state.rows:
        opening = row[REQUIRED_FIRST_COLUMN]
        if state.journal.is_processed(opening):
            continue
        matches = state.search_index.find(opening)
        state.pending_queue.put(
            CurrentReview(opening_number=opening, row=row, matches=matches)
        )
    state.worker_done = True


def _ensure_worker(state: AppState) -> None:
    with state.lock:
        if state.worker_started:
            return
        t = threading.Thread(target=_search_worker, args=(state,), daemon=True)
        state.worker_thread = t
        state.worker_started = True
        t.start()


def _pull_next_review(state: AppState) -> CurrentReview | None:
    """Pop the next opening from the queue without auto-skipping zero-matches.

    Returns None if no row is ready right now (worker still searching) and
    the run is not yet done. Returns None if the run is fully done — caller
    distinguishes via state.worker_done + queue empty.
    """
    if state.current is not None:
        return state.current
    try:
        result = state.pending_queue.get_nowait()
    except queue.Empty:
        return None
    state.current = result
    return result


def _is_run_done(state: AppState) -> bool:
    return state.worker_done and state.pending_queue.empty() and state.current is None


# ============================================================================
# Flask app factory
# ============================================================================


def create_app() -> Flask:
    here = Path(__file__).parent
    app = Flask(
        __name__,
        template_folder=str(here / "templates"),
        static_folder=str(here / "static"),
    )

    # ----- Upload -----

    @app.get("/")
    def index() -> str:
        return render_template("upload.html", defaults={"excel": "", "pdf": ""})

    @app.post("/pick")
    def pick_file() -> Any:
        """Open a native OS file dialog and return the chosen path."""
        kind = (request.json or {}).get("kind", "")
        if kind == "excel":
            title = "Select Excel file"
            filetypes = [("Excel workbook", "*.xlsx"), ("All files", "*.*")]
        elif kind == "pdf":
            title = "Select PDF file"
            filetypes = [("PDF document", "*.pdf"), ("All files", "*.*")]
        else:
            abort(400)

        path = _ask_open_filename(title, filetypes)
        return jsonify({"path": path or ""})

    @app.post("/load")
    def load() -> Any:
        excel_path = request.form.get("excel_path", "").strip().strip('"')
        pdf_path = request.form.get("pdf_path", "").strip().strip('"')

        defaults = {"excel": excel_path, "pdf": pdf_path}

        errors: list[str] = []
        if not excel_path or not Path(excel_path).is_file():
            errors.append(f"Excel file not found: {excel_path or '(empty)'}")
        if not pdf_path or not Path(pdf_path).is_file():
            errors.append(f"PDF file not found: {pdf_path or '(empty)'}")
        if errors:
            return render_template("upload.html", errors=errors, defaults=defaults), 400

        try:
            headers, rows = load_headers_and_rows(excel_path)
        except ExcelValidationError as e:
            return render_template("upload.html", errors=[str(e)], defaults=defaults), 400

        pdf_in = Path(pdf_path)
        excel_in = Path(excel_path)
        out_dir = pdf_in.parent
        out_pdf = out_dir / f"{pdf_in.stem}_markups.pdf"
        out_xml = out_dir / f"{pdf_in.stem}_markups_summary.xml"
        out_csv = out_dir / f"{pdf_in.stem}_manual.csv"

        pdf_hash = hash_file(pdf_in)
        excel_hash = hash_file(excel_in)
        journal = Journal.load_or_create(out_dir, pdf_hash, excel_hash)

        # ALWAYS re-copy from the source. The journal is the source of truth
        # for what's been done; we replay completed callouts into the fresh
        # copy below. This eliminates the "broken working PDF" failure class
        # entirely — any structural damage from prior incremental saves is
        # discarded each /load.
        prepare_working_copy(pdf_in, out_pdf)

        with STATE.lock:
            STATE.input_pdf_path = pdf_in
            STATE.input_excel_path = excel_in
            STATE.output_pdf_path = out_pdf
            STATE.output_xml_path = out_xml
            STATE.output_csv_path = out_csv
            STATE.headers = headers
            STATE.rows = rows
            STATE.journal = journal
            STATE.doc = open_working_copy(out_pdf)
            STATE.search_index = None
            STATE.template_columns = list(journal.state.template_columns)
            STATE.pending_queue = queue.Queue(maxsize=8)
            STATE.worker_thread = None
            STATE.worker_started = False
            STATE.worker_done = False
            STATE.current = None
            STATE.occupied_by_page = {}
            STATE.written = []

        # Replay any completed callouts from the journal onto the fresh PDF
        # copy. This is the resume mechanism: the journal records every
        # accepted callout's anchor, box, and body text — re-applying them
        # to a fresh copy gives the user the same visible state without
        # depending on the prior working copy being intact.
        if STATE.doc is not None and journal.state.completed:
            _replay_journal_into_doc(STATE.doc, journal)

        return redirect(url_for("template_screen"))

    # ----- Template config -----

    @app.get("/template")
    def template_screen() -> str:
        cols = metadata_columns(STATE.headers)
        return render_template(
            "template.html",
            metadata_columns=cols,
            preselected=set(STATE.template_columns),
            row_count=len(STATE.rows),
            done_count=len(STATE.journal.state.completed) if STATE.journal else 0,
        )

    @app.post("/start")
    def start() -> Any:
        selected = request.form.getlist("columns")
        STATE.template_columns = selected
        if STATE.journal is not None:
            STATE.journal.set_template_columns(selected)
        _ensure_worker(STATE)
        return redirect(url_for("review"))

    # ----- Review SPA shell -----

    @app.get("/review")
    def review() -> Any:
        return render_template("review.html")

    # ----- PDF streaming for PDF.js -----

    @app.get("/pdf_file")
    def pdf_file() -> Any:
        """Stream the working PDF copy with HTTP Range support.

        Werkzeug's send_file(conditional=True) honors Range headers
        automatically. PDF.js will issue range requests for only the parts
        of the file it needs, which is what makes multi-GB PDFs feasible.
        Cache-Control: no-store prevents stale browser-cached ranges after
        save_incremental extends the file on disk.
        """
        if STATE.output_pdf_path is None:
            abort(404)
        resp = send_file(
            STATE.output_pdf_path,
            mimetype="application/pdf",
            conditional=True,
            max_age=0,
        )
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Pragma"] = "no-cache"
        return resp

    # ----- JSON state endpoints -----

    @app.get("/api/state")
    def api_state() -> Any:
        if _is_run_done(STATE):
            return jsonify({"done": True, "next": url_for("done")})

        review_obj = _pull_next_review(STATE)
        if review_obj is None:
            return jsonify({"waiting": True})

        return jsonify(_serialize_state(review_obj))

    @app.post("/api/next")
    def api_next() -> Any:
        if STATE.current is None:
            abort(400)
        if STATE.current.matches:
            STATE.current.match_index = (
                STATE.current.match_index + 1
            ) % len(STATE.current.matches)
        return jsonify(_serialize_state(STATE.current))

    @app.post("/api/accept")
    def api_accept() -> Any:
        if STATE.current is None or STATE.doc is None or STATE.journal is None:
            abort(400)
        if not STATE.current.matches:
            abort(400)  # zero-match openings can't be accepted
        payload = request.get_json(force=True)
        box = payload.get("box")
        if not box or len(box) != 4:
            abort(400)

        match = STATE.current.matches[STATE.current.match_index]
        page = STATE.doc.load_page(match.page_index)
        text_box_rect = fitz.Rect(*box) & page.rect
        anchor = match.center
        body_text = join_template(STATE.current.row, STATE.template_columns)

        # Full Excel row (every column, stringified) is stashed on the
        # annotation and the journal so nothing is lost — even columns the
        # user didn't include in the visible callout text.
        metadata = dict(STATE.current.row)

        annot = add_callout(
            page,
            anchor,
            text_box_rect,
            body_text,
            STATE.current.opening_number,
            metadata=metadata,
        )
        save_incremental(STATE.doc)

        written = WrittenAnnot(
            opening_number=STATE.current.opening_number,
            page_index=match.page_index,
            page_label=match.page_label,
            body_text=body_text,
            text_box_rect=text_box_rect,
            anchor_point=anchor,
            created_at=datetime.now(),
            xref=annot.xref,
            metadata=metadata,
        )
        STATE.written.append(written)
        STATE.occupied_by_page.setdefault(match.page_index, []).append(text_box_rect)

        STATE.journal.mark_completed(
            STATE.current.opening_number,
            CompletedEntry(
                page_index=match.page_index,
                page_label=match.page_label,
                body_text=body_text,
                text_box=(
                    text_box_rect.x0,
                    text_box_rect.y0,
                    text_box_rect.x1,
                    text_box_rect.y1,
                ),
                anchor=anchor,
                placed_at=written.created_at.isoformat(),
                xref=annot.xref,
                metadata=metadata,
            ),
        )
        STATE.current = None
        return _next_state_response()

    @app.post("/api/skip")
    def api_skip() -> Any:
        if STATE.current is None or STATE.journal is None:
            abort(400)
        STATE.journal.mark_skipped(STATE.current.opening_number)
        STATE.current = None
        return _next_state_response()

    @app.post("/api/update_callout")
    def api_update_callout() -> Any:
        """Reposition an existing previously-placed callout (drag-release auto-save)."""
        if STATE.doc is None or STATE.journal is None:
            abort(400)
        payload = request.get_json(force=True)
        try:
            xref = int(payload["xref"])
            box = payload["box"]
            x0, y0, x1, y1 = float(box[0]), float(box[1]), float(box[2]), float(box[3])
        except (KeyError, ValueError, TypeError):
            abort(400)

        target = next((w for w in STATE.written if w.xref == xref), None)
        if target is None:
            abort(404)

        page = STATE.doc.load_page(target.page_index)
        new_rect = fitz.Rect(x0, y0, x1, y1) & page.rect
        ok = update_callout_position(page, xref, new_rect, target.anchor_point)
        if not ok:
            abort(404)
        save_incremental(STATE.doc)

        target.text_box_rect = new_rect

        # Rebuild the per-page occupied list to reflect the new position so
        # the whitespace probe doesn't keep penalizing the OLD spot.
        STATE.occupied_by_page[target.page_index] = [
            w.text_box_rect for w in STATE.written if w.page_index == target.page_index
        ]

        STATE.journal.update_completed_position(
            target.opening_number, (new_rect.x0, new_rect.y0, new_rect.x1, new_rect.y1)
        )
        return jsonify({"ok": True})

    @app.get("/api/page/<int:page_index>/callouts")
    def api_page_callouts(page_index: int) -> Any:
        """Return all previously-placed callouts on a given page."""
        items = []
        for w in STATE.written:
            if w.page_index != page_index:
                continue
            items.append(
                {
                    "xref": w.xref,
                    "opening_number": w.opening_number,
                    "body_text": w.body_text,
                    "box": [
                        w.text_box_rect.x0,
                        w.text_box_rect.y0,
                        w.text_box_rect.x1,
                        w.text_box_rect.y1,
                    ],
                    "anchor": [w.anchor_point[0], w.anchor_point[1]],
                }
            )
        return jsonify({"callouts": items})

    # ----- Done -----

    @app.get("/done")
    def done() -> str:
        _finalize_outputs()
        snap = _progress_snapshot()
        return render_template(
            "done.html",
            output_pdf=str(STATE.output_pdf_path),
            output_xml=str(STATE.output_xml_path),
            output_csv=str(STATE.output_csv_path),
            **snap,
        )

    return app


# ============================================================================
# Helpers
# ============================================================================


def _replay_journal_into_doc(doc: fitz.Document, journal: Journal) -> None:
    """Re-apply the journal's completed callouts onto a fresh PDF copy.

    Called once at the end of /load when a resumed run finds existing
    completed entries. For each entry we call add_callout with the same
    anchor/box/body_text/opening_number that was originally placed, capture
    the new xref, populate STATE.written + STATE.occupied_by_page, and update
    the journal entry's xref. A single save_incremental at the end persists
    everything to disk.
    """
    journal_dirty = False
    for opening, entry in journal.state.completed.items():
        if entry.page_index < 0 or entry.page_index >= doc.page_count:
            continue
        page = doc.load_page(entry.page_index)
        box = fitz.Rect(*entry.text_box) & page.rect
        anchor = (float(entry.anchor[0]), float(entry.anchor[1]))
        annot = add_callout(
            page,
            anchor,
            box,
            entry.body_text,
            opening,
            metadata=entry.metadata,
        )
        new_xref = annot.xref
        if entry.xref != new_xref:
            entry.xref = new_xref
            journal_dirty = True

        STATE.written.append(
            WrittenAnnot(
                opening_number=opening,
                page_index=entry.page_index,
                page_label=entry.page_label,
                body_text=entry.body_text,
                text_box_rect=box,
                anchor_point=anchor,
                created_at=datetime.fromisoformat(entry.placed_at),
                xref=new_xref,
                metadata=dict(entry.metadata),
            )
        )
        STATE.occupied_by_page.setdefault(entry.page_index, []).append(box)

    if STATE.written:
        save_incremental(doc)
    if journal_dirty:
        journal.flush()


def _ask_open_filename(title: str, filetypes: list[tuple[str, str]]) -> str:
    """Pop a native OS file dialog and return the chosen absolute path."""
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        path = filedialog.askopenfilename(parent=root, title=title, filetypes=filetypes)
        return path or ""
    finally:
        root.destroy()


def _serialize_state(review_obj: CurrentReview) -> dict[str, Any]:
    """Build the JSON state shape the SPA expects from /api/state etc."""
    body_text = join_template(review_obj.row, STATE.template_columns)
    progress = _progress_snapshot()

    base: dict[str, Any] = {
        "opening": review_obj.opening_number,
        "body_text": body_text,
        "match_index": review_obj.match_index,
        "match_count": len(review_obj.matches),
        "zero_match": len(review_obj.matches) == 0,
        "progress": progress,
    }

    if not review_obj.matches:
        base["match"] = None
        return base

    match = review_obj.matches[review_obj.match_index]
    page = STATE.doc.load_page(match.page_index) if STATE.doc else None
    if page is None:
        base["match"] = None
        return base

    auto_box = find_best_callout_position(
        page,
        match.rect,
        (DEFAULT_BOX_W_PT, DEFAULT_BOX_H_PT),
        STATE.occupied_by_page.get(match.page_index, []),
    )
    base["match"] = {
        "page_index": match.page_index,
        "page_label": match.page_label,
        "page_width": page.rect.width,
        "page_height": page.rect.height,
        "anchor_pdf": [match.center[0], match.center[1]],
        "anchor_rect_pdf": [
            match.rect.x0,
            match.rect.y0,
            match.rect.x1,
            match.rect.y1,
        ],
        "auto_box_pdf": [auto_box.x0, auto_box.y0, auto_box.x1, auto_box.y1],
    }
    return base


def _next_state_response() -> Any:
    """After accept/skip, return the state for the NEXT opening (or done)."""
    if _is_run_done(STATE):
        return jsonify({"done": True, "next": url_for("done")})
    nxt = _pull_next_review(STATE)
    if nxt is None:
        return jsonify({"waiting": True})
    return jsonify(_serialize_state(nxt))


def _progress_snapshot() -> dict[str, int]:
    j = STATE.journal
    if j is None:
        return {"placed": 0, "skipped": 0, "unmatched": 0, "total": 0}
    return {
        "placed": len(j.state.completed),
        "skipped": len(j.state.skipped),
        "unmatched": len(j.state.unmatched),
        "total": len(STATE.rows),
    }


def _finalize_outputs() -> None:
    """Write the Markups Summary XML and the manual-action CSV at end-of-run."""
    if STATE.journal is None or STATE.output_xml_path is None or STATE.output_csv_path is None:
        return
    if STATE.output_pdf_path is None:
        return

    write_summary(
        STATE.written,
        document_filename=STATE.output_pdf_path.name,
        output_xml_path=STATE.output_xml_path,
    )

    headers = STATE.headers
    skipped_keys = set(STATE.journal.state.skipped)
    unmatched_keys = set(STATE.journal.state.unmatched)
    needs_manual = skipped_keys | unmatched_keys

    with open(STATE.output_csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Reason", *headers])
        for r in STATE.rows:
            opening = r[REQUIRED_FIRST_COLUMN]
            if opening not in needs_manual:
                continue
            reason = "no_matches" if opening in unmatched_keys else "user_skipped"
            w.writerow([reason, *(r.get(h, "") for h in headers)])
