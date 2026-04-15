"""Excel ingestion: validation + streaming row reader."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Iterator

import openpyxl


REQUIRED_FIRST_COLUMN = "Opening Number"


class ExcelValidationError(Exception):
    """Raised when the Excel file fails the schema rules."""


def _coerce(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def load_headers_and_rows(path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    """Load and validate an input Excel file.

    Returns (headers, rows). Rows are fully materialized as a list because the
    review loop needs random access for resume-after-crash, and even the worst
    realistic schedule (a few thousand rows) fits in memory comfortably as
    stringified dicts.

    Validation rules:
      - Sheet must have a header row.
      - First column must literally be 'Opening Number'.
      - All Opening Number values must be unique and non-empty.
    """
    wb = openpyxl.load_workbook(filename=str(path), read_only=True, data_only=True)
    ws = wb.active
    if ws is None:
        raise ExcelValidationError("Workbook has no active sheet.")

    rows_iter = ws.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        raise ExcelValidationError("Excel file is empty.")

    headers = [_coerce(h) for h in header_row]
    if not headers or headers[0] != REQUIRED_FIRST_COLUMN:
        raise ExcelValidationError(
            f"First column must be literally '{REQUIRED_FIRST_COLUMN}'. "
            f"Found: '{headers[0] if headers else '(empty)'}'."
        )

    rows: list[dict[str, str]] = []
    for row_index, raw in enumerate(rows_iter, start=2):
        # Skip fully empty rows so trailing blanks in the sheet don't break us.
        if all(v is None or _coerce(v) == "" for v in raw):
            continue
        row = {headers[i]: _coerce(v) for i, v in enumerate(raw) if i < len(headers)}
        opening = row.get(REQUIRED_FIRST_COLUMN, "").strip()
        if not opening:
            raise ExcelValidationError(
                f"Row {row_index}: '{REQUIRED_FIRST_COLUMN}' is empty."
            )
        row[REQUIRED_FIRST_COLUMN] = opening
        rows.append(row)

    wb.close()

    counts = Counter(r[REQUIRED_FIRST_COLUMN] for r in rows)
    dupes = sorted(name for name, c in counts.items() if c > 1)
    if dupes:
        sample = ", ".join(dupes[:10])
        more = "" if len(dupes) <= 10 else f" (and {len(dupes) - 10} more)"
        raise ExcelValidationError(
            f"Duplicate Opening Numbers are not allowed: {sample}{more}."
        )

    return headers, rows


def metadata_columns(headers: list[str]) -> list[str]:
    """Return the metadata columns (everything after Opening Number)."""
    return [h for h in headers if h != REQUIRED_FIRST_COLUMN]


def join_template(row: dict[str, str], selected_columns: list[str]) -> str:
    """Build the callout body text from the user's checkbox selection.

    Opening Number is always first. Selected columns follow in checkbox order,
    skipping any blanks so the result doesn't have empty ' / ' segments.
    """
    parts: list[str] = []
    opening = row.get(REQUIRED_FIRST_COLUMN, "").strip()
    if opening:
        parts.append(opening)
    for col in selected_columns:
        if col == REQUIRED_FIRST_COLUMN:
            continue
        value = row.get(col, "").strip()
        if value:
            parts.append(value)
    return " / ".join(parts)
