"""Reads a lab report exported as a spreadsheet (CSV/TSV) - the "UserReport_*.csv"
files chemistry analyzers write, for example:

    Madhuroha Pet Care
    NAME:,Sox
    TEST TIME:,2026-08-14 12:04:38
    ------------------------------------------------------------------------
    Assay,Result,Absorbance,Reactivity, ,Ref,Unit
    ALB,2.4,1.5017813,1.4997904,L,3.2--4.3,g/dL

The same machine also exports a tab-separated variant whose values are quoted with a
stray tab inside the quotes ("Sox\t"). Columns are read by the header row rather than
by position, so the analyzer's own extras (Absorbance, Reactivity) never pass for a
result or a range, and the flag column - which has no header at all - is recognised by
its H/L contents.

Produces what record_extractor needs: the document as text lines (header fields as
"NAME: Sox", rows with their cells two spaces apart, like the app's photo text) and the
result table already split into rows.
"""

import csv
import io
import re
from typing import Any

from app.services.result_rows import header_roles, row_from_cells

_SEPARATOR_RE = re.compile(r"^[-=_*]{5,}$")
_FLAG_CELL_RE = re.compile(r"H|L|HH|LL|High|Low|\*|↑|↓", re.IGNORECASE)
# The header field naming the panel ("LAB.: Biochemistry"), used as the table's title.
_PANEL_FIELD_RE = re.compile(r"^(?:lab|department|panel|profile|section)\b", re.IGNORECASE)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _rows(text: str) -> list[list[str]]:
    """Cells per line, trimmed. The delimiter is whichever of tab or comma the file
    actually splits its lines on."""
    sample = text[:4096]
    delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
    rows = []
    for raw in csv.reader(io.StringIO(text), delimiter=delimiter):
        cells = [" ".join(cell.replace("\t", " ").split()) for cell in raw]
        while cells and not cells[-1]:
            cells.pop()
        rows.append(cells)
    return rows


def _flag_column(rows: list[list[str]], roles: dict[str, int]) -> int | None:
    """The unheaded column whose cells are only H/L flags (or blank)."""
    used = set(roles.values())
    width = max((len(r) for r in rows), default=0)
    for index in range(width):
        if index in used:
            continue
        cells = [r[index] for r in rows if index < len(r) and r[index]]
        if cells and all(_FLAG_CELL_RE.fullmatch(c) for c in cells):
            return index
    return None


def is_spreadsheet_report(filename: str | None) -> bool:
    return bool(filename) and filename.lower().rsplit(".", 1)[-1] in ("csv", "tsv")


def read_spreadsheet_report(data: bytes) -> tuple[str, list[dict[str, Any]]]:
    """(text lines, result sections) for record_extractor.build_record_card."""
    rows = _rows(_decode(data))

    lines: list[str] = []
    header_at: int | None = None
    roles: dict[str, int] | None = None
    panel = None
    for i, cells in enumerate(rows):
        filled = [c for c in cells if c]
        if not filled:
            continue
        if header_at is None and (found := header_roles(cells)):
            header_at, roles = i, found
        if len(filled) == 2 and filled[0].endswith(":"):
            # "NAME:,Sox" -> "NAME: Sox", so the record's header fields read it.
            lines.append(f"{filled[0]} {filled[1]}")
            if _PANEL_FIELD_RE.match(filled[0]):
                panel = filled[1]
        else:
            lines.append("  ".join(filled))

    sections: list[dict[str, Any]] = []
    if header_at is not None and roles is not None:
        body = []
        table_end = len(rows)
        for i, cells in enumerate(rows[header_at + 1 :], start=header_at + 1):
            filled = [c for c in cells if c]
            if not filled:
                continue
            if len(filled) == 1 and _SEPARATOR_RE.match(filled[0]):
                table_end = i  # the table ends at the analyzer's dashed rule
                break
            body.append(cells)
        # Notes printed under the table ("ALT、CK>Detection limit, please retest after
        # dilution.") become the record's results notes. The legend for "--" and the
        # instrument's status counters ("WAT:0  EMP:0") are not notes.
        for cells in rows[table_end + 1 :]:
            filled = [c for c in cells if c]
            if not filled or _SEPARATOR_RE.match(filled[0]) or filled[0].startswith("--"):
                continue
            if all(re.fullmatch(r"\w+:\s*\d+", c) for c in filled):
                continue
            # A comma inside the note splits it into cells in a comma-separated file.
            lines.append(f"Remarks: {', '.join(filled)}")
        flag = _flag_column(body, roles)
        if flag is not None and "flag" not in roles:
            roles = {**roles, "flag": flag}
        parameters = [row for row in (row_from_cells(cells, roles) for cells in body) if row]
        if parameters:
            sections.append({"title": panel or "Results", "parameters": parameters})

    return "\n".join(lines), sections
