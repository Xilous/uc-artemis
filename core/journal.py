"""Crash-recoverable journal for the per-row pinning loop.

Keyed to a hash of the input PDF AND the input Excel. If either input changes
between runs, the journal is archived and a fresh one is started so we never
resume into stale state.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


JOURNAL_FILENAME = ".uc_artemis_journal.json"
HASH_CHUNK_BYTES = 1024 * 1024  # 1 MB


@dataclass
class CompletedEntry:
    page_index: int
    page_label: str
    body_text: str
    text_box: tuple[float, float, float, float]
    anchor: tuple[float, float]
    placed_at: str  # ISO format
    xref: int  # PDF object number; lets a resumed run find the annotation again
    metadata: dict[str, str] = field(default_factory=dict)  # full Excel row


@dataclass
class JournalState:
    pdf_hash: str
    excel_hash: str
    template_columns: list[str] = field(default_factory=list)
    completed: dict[str, CompletedEntry] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)


def hash_file(path: str | Path) -> str:
    """Streamed SHA-256 so multi-GB files don't load into memory."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def journal_path(output_dir: str | Path) -> Path:
    return Path(output_dir) / JOURNAL_FILENAME


class Journal:
    """Append-mostly JSON journal with atomic writes."""

    def __init__(self, path: Path, state: JournalState) -> None:
        self.path = path
        self.state = state

    @classmethod
    def load_or_create(
        cls, output_dir: str | Path, pdf_hash: str, excel_hash: str
    ) -> "Journal":
        path = journal_path(output_dir)
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = None
            if (
                raw
                and raw.get("pdf_hash") == pdf_hash
                and raw.get("excel_hash") == excel_hash
            ):
                state = _decode_state(raw)
                return cls(path, state)
            # Hash mismatch or unreadable → archive and start fresh.
            if path.exists():
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                archived = path.with_suffix(f".{ts}.bak.json")
                try:
                    path.rename(archived)
                except OSError:
                    pass
        state = JournalState(pdf_hash=pdf_hash, excel_hash=excel_hash)
        j = cls(path, state)
        j.flush()
        return j

    def set_template_columns(self, columns: list[str]) -> None:
        self.state.template_columns = list(columns)
        self.flush()

    def mark_completed(self, opening_number: str, entry: CompletedEntry) -> None:
        self.state.completed[opening_number] = entry
        self.flush()

    def update_completed_position(
        self,
        opening_number: str,
        new_text_box: tuple[float, float, float, float],
    ) -> None:
        """Update an existing completed entry's box after a drag-reposition.

        Anchor, page, body text, and xref all stay the same — only the box
        rect changes. Leaves the placed_at timestamp untouched (the callout
        was created at that time; this is just a move).
        """
        existing = self.state.completed.get(opening_number)
        if existing is None:
            return
        existing.text_box = new_text_box
        self.flush()

    def mark_skipped(self, opening_number: str) -> None:
        if opening_number not in self.state.skipped:
            self.state.skipped.append(opening_number)
        self.flush()

    def mark_rejected(self, opening_number: str) -> None:
        if opening_number not in self.state.rejected:
            self.state.rejected.append(opening_number)
        self.flush()

    def mark_unmatched(self, opening_number: str) -> None:
        if opening_number not in self.state.unmatched:
            self.state.unmatched.append(opening_number)
        self.flush()

    def is_processed(self, opening_number: str) -> bool:
        return (
            opening_number in self.state.completed
            or opening_number in self.state.skipped
            or opening_number in self.state.rejected
            or opening_number in self.state.unmatched
        )

    def flush(self) -> None:
        """Atomic JSON write: temp file + os.replace."""
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = _encode_state(self.state)
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def _encode_state(state: JournalState) -> dict[str, Any]:
    return {
        "pdf_hash": state.pdf_hash,
        "excel_hash": state.excel_hash,
        "template_columns": state.template_columns,
        "completed": {k: asdict(v) for k, v in state.completed.items()},
        "skipped": state.skipped,
        "rejected": state.rejected,
        "unmatched": state.unmatched,
    }


def _decode_state(raw: dict[str, Any]) -> JournalState:
    completed = {
        k: CompletedEntry(
            page_index=v["page_index"],
            page_label=v["page_label"],
            body_text=v["body_text"],
            text_box=tuple(v["text_box"]),  # type: ignore[arg-type]
            anchor=tuple(v["anchor"]),  # type: ignore[arg-type]
            placed_at=v["placed_at"],
            xref=v.get("xref", 0),  # default 0 for journals from Phase 1
            metadata=dict(v.get("metadata") or {}),  # default empty for older journals
        )
        for k, v in (raw.get("completed") or {}).items()
    }
    return JournalState(
        pdf_hash=raw["pdf_hash"],
        excel_hash=raw["excel_hash"],
        template_columns=list(raw.get("template_columns") or []),
        completed=completed,
        skipped=list(raw.get("skipped") or []),
        rejected=list(raw.get("rejected") or []),
        unmatched=list(raw.get("unmatched") or []),
    )
