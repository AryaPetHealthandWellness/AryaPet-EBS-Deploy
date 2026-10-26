"""Extracts result tables from the original PDF's geometry rather than from OCR text.

A lab table's row structure is carried by *column position*, which is lost the moment
the document is flattened to a string - once "Albumin 2.5 2.2-4.0 g/dl ALP 1071.4" is
one line, nothing in the token stream says 2.5 belongs to Albumin and not to ALP. The
text-based parser can only guess that back with regex.

The PDF itself still has it, so this reads the PDF directly (the backend already holds
the uploaded file in S3) and works down two strategies:

  Tier 1 (ruled lines) - the table is drawn with cell rectangles, so pdfplumber can
    return true rows. Exact, no guessing.
  Tier 2 (word positions) - no usable grid, so lines are rebuilt from each word's
    position and split into cells on wide horizontal gaps.

Either way, a table is found by its header row ("Test  Result  Unit  Reference") -
wherever on the page that sits - and each later cell is matched to a header column by
its horizontal position, so a lab that reorders, renames or adds columns (a separate
Unit or Flag column) still parses, and a page can hold several tables, each under its
own title. Rows outside any header are read as text lines (app/services/result_rows.py)
and kept only when they carry their own evidence of being a result.

Callers fall back to the text-based parser when this returns no sections - that remains
the only option for photographed documents, which have no PDF geometry at all.
"""

import io
import logging
from typing import Any

from app.services.result_rows import (
    has_evidence,
    header_roles,
    is_panel_title,
    outcome_pair,
    parse_result_line,
    row_from_cells,
    row_from_parts,
    title_text,
)

logger = logging.getLogger(__name__)

# A row needs at least a name and a value to be worth keeping.
_MIN_COLUMNS = 2
# Header rows that stop matching for this many lines in a row have ended.
_MAX_MISSES = 3


def _is_section_title(line: str) -> bool:
    return is_panel_title(line)


def _clean(rows: list[list[Any]]) -> list[list[str]]:
    return [[" ".join(str(cell or "").split()) for cell in row] for row in rows]


def _rows_to_parameters(rows: list[list[str]]) -> list[dict[str, Any]]:
    """Turns raw table rows into parameters, using a header row to decide which column
    is the name, the value, the reference interval and so on."""
    rows = _clean(rows)
    if len(rows) < 2:
        return []

    header_at = next((i for i, row in enumerate(rows) if header_roles(row)), None)
    if header_at is None:
        # No recognizable header - fall back to the conventional ordering, but only
        # when the shape is right, so arbitrary non-table rows aren't misread.
        if len(rows[0]) < _MIN_COLUMNS:
            return []
        roles = {"name": 0, "value": 1}
        if len(rows[0]) > 2:
            roles["range"] = 2
        data_rows = rows
    else:
        roles = header_roles(rows[header_at]) or {}
        data_rows = rows[header_at + 1 :]

    parameters = []
    for row in data_rows:
        parameter = row_from_cells(row, roles)
        if parameter is not None:
            parameters.append(parameter)
    return parameters


# ── Tier 1: ruled tables ───────────────────────────────────────────────────


def _title_above(page: Any, top: float) -> str | None:
    """The nearest table title printed above `top` on the page."""
    if top <= 1:
        return None
    above = page.crop((0, 0, page.width, top)).extract_text() or ""
    for line in reversed(above.split("\n")):
        if is_panel_title(line):
            return title_text(line)
    return None


def _sections_from_cell_rows(rows: list[list[str]], title: str | None) -> list[dict[str, Any]]:
    """Rows of one grid read top to bottom. A grid can hold several panels - a title row
    ("KIDNEY FUNCTION TEST") and a fresh header row partway down start the next one."""
    if not any(header_roles(row) for row in rows):
        parameters = _rows_to_parameters(rows)
        return [{"title": title, "parameters": parameters}] if parameters else []

    sections: list[dict[str, Any]] = []
    roles: dict[str, int] | None = None
    for row in rows:
        filled = [cell for cell in row if cell]
        if len(filled) == 1 and is_panel_title(filled[0]):
            title = title_text(filled[0])
            continue
        found = header_roles(row)
        if found is not None:
            roles = found
            continue
        if roles is None:
            continue
        parameter = row_from_cells(row, roles)
        if parameter is None:
            continue
        if not sections or sections[-1]["title"] != title:
            sections.append({"title": title, "parameters": []})
        sections[-1]["parameters"].append(parameter)
    return sections


def _sections_from_ruled_tables(page: Any) -> list[dict[str, Any]]:
    sections = []
    for table in page.find_tables({"vertical_strategy": "lines", "horizontal_strategy": "lines"}):
        sections.extend(_sections_from_cell_rows(_clean(table.extract()), _title_above(page, table.bbox[1])))
    return sections


# ── Tier 2: word positions ─────────────────────────────────────────────────


def _word_lines(page: Any, y_tolerance: float = 3.0) -> list[list[dict[str, Any]]]:
    """Words grouped into printed lines by their vertical position."""
    words = sorted(page.extract_words(), key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict[str, Any]]] = []
    for word in words:
        if lines and abs(word["top"] - lines[-1][0]["top"]) <= y_tolerance:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def _cells(line: list[dict[str, Any]], column_gap: float = 12.0) -> list[dict[str, Any]]:
    """A line split into cells wherever there's a wide horizontal gap between words."""
    cells: list[dict[str, Any]] = []
    for word in line:
        if cells and word["x0"] - cells[-1]["x1"] <= column_gap:
            cells[-1]["text"] += " " + word["text"]
            cells[-1]["x1"] = word["x1"]
        else:
            cells.append({"text": word["text"], "x0": word["x0"], "x1": word["x1"]})
    return cells


def _assign_columns(cells: list[dict[str, Any]], columns: dict[str, dict[str, float]]) -> dict[str, str]:
    """Each cell's text under the header column it sits closest to. Values are often
    centred under their header while names are left-aligned, so both the centre and the
    left edge are compared."""
    assigned: dict[str, list[str]] = {}
    for cell in cells:
        centre = (cell["x0"] + cell["x1"]) / 2

        def distance(role: str, cell: dict[str, Any] = cell, centre: float = centre) -> float:
            column = columns[role]
            return min(abs(centre - (column["x0"] + column["x1"]) / 2), abs(cell["x0"] - column["x0"]))

        assigned.setdefault(min(columns, key=distance), []).append(cell["text"])
    return {role: " ".join(texts) for role, texts in assigned.items()}


def _sections_from_word_positions(page: Any) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    title: str | None = None
    columns: dict[str, dict[str, float]] | None = None
    misses = 0

    def add(row: dict[str, Any]) -> None:
        if not sections or sections[-1]["title"] != title:
            sections.append({"title": title, "parameters": []})
        sections[-1]["parameters"].append(row)

    for line in _word_lines(page):
        cells = _cells(line)
        texts = [cell["text"] for cell in cells]
        joined = " ".join(texts)

        roles = header_roles(texts)
        if roles is not None:
            columns = {role: cells[index] for role, index in roles.items()}
            misses = 0
            continue

        if columns is not None:
            by_role = _assign_columns(cells, columns)
            order = list(columns)
            row = row_from_cells(
                [by_role.get(role, "") for role in order], {role: i for i, role in enumerate(order)}
            )
            if row is not None:
                add(row)
                misses = 0
                continue

        outcome = outcome_pair(joined)
        parts = parse_result_line(joined)
        if outcome is not None:
            add(outcome)
            continue
        if parts is not None and parts["value"] is not None and has_evidence(parts):
            add(row_from_parts(parts))
            continue

        if is_panel_title(joined):
            title, columns = title_text(joined), None
            continue
        misses += 1
        if misses >= _MAX_MISSES:
            columns = None

    return [s for s in sections if s["parameters"]]


# ── Entry points ───────────────────────────────────────────────────────────


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """The PDF's own text layer, page by page. Empty for a scanned PDF (image only),
    which then needs the client's OCR text instead."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def extract_lab_sections_from_pdf(pdf_bytes: bytes) -> tuple[list[dict[str, Any]], str | None]:
    """Returns (sections, strategy). `strategy` names the tier that produced the
    result ("ruled_lines" / "word_positions") so callers can record how often the
    lossy text fallback is actually being reached. Returns ([], None) when the PDF
    yields no recognizable table - the caller should then fall back to the text
    parser rather than treating this as a failure."""
    import pdfplumber

    ruled: list[dict[str, Any]] = []
    clustered: list[dict[str, Any]] = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_ruled = _sections_from_ruled_tables(page)
            if page_ruled:
                ruled.extend(page_ruled)
                continue
            # Only pay for the word-position pass on pages the grid strategy
            # couldn't read, so a normal ruled report doesn't do the work twice.
            clustered.extend(_sections_from_word_positions(page))

    if ruled and clustered:
        return ruled + clustered, "ruled_lines+word_positions"
    if ruled:
        return ruled, "ruled_lines"
    if clustered:
        return clustered, "word_positions"
    return [], None
