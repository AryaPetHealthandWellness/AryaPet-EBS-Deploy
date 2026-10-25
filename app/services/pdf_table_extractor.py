"""Extracts lab panels from the original PDF's geometry rather than from OCR text.

A lab table's row structure is carried by *column position*, which is lost the moment
the document is flattened to a string - once "Albumin 2.5 2.2-4.0 g/dl ALP 1071.4" is
one line, nothing in the token stream says 2.5 belongs to Albumin and not to ALP. The
text-based parser in document_classifier.py can only guess that back with regex.

The PDF itself still has it, so this reads the PDF directly (the backend already holds
the uploaded file in S3) and works down two strategies:

  Tier 1 (_tables_from_ruled_lines) - the table is drawn with cell rectangles, so
    pdfplumber can return true rows. Exact, no guessing.
  Tier 2 (_tables_from_word_positions) - no grid is drawn, so rows are rebuilt by
    clustering words on their y-coordinate and splitting columns on horizontal gaps.

Columns are then located by reading the header row ("parameter" / "value" /
"normal range") rather than by fixed index, so a lab that reorders or renames its
columns still parses.

Callers fall back to the text-based parser when this returns None - that remains the
only option for photographed documents, which have no PDF geometry at all.
"""

import io
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Same section-title test as document_classifier: fully-capitalized line naming a
# panel. Kept in sync deliberately - both paths should agree on what a section is.
_SECTION_KEYWORDS_RE = re.compile(
    r"TEST|FUNCTION|PANEL|PROFILE|ANALYSIS|COUNT|SCREEN|CULTURE", re.IGNORECASE
)
_RANGE_BOUNDS_RE = re.compile(r"([\d.]+)\s*[–\-—−]\s*([\d.]+)")
_UNIT_RE = re.compile(r"\b(g/dl|mg/dl|u/l|iu/l|mmol/l|umol/l|ng/ml|pg/ml|%)\b", re.IGNORECASE)
_NUMERIC_RE = re.compile(r"^[<>]?\s*\d+\.?\d*$")

# Header cells that identify each column's role.
_NAME_HEADERS = ("parameter", "test", "analyte", "investigation", "examination")
_VALUE_HEADERS = ("value", "result", "observed", "reading")
_RANGE_HEADERS = ("normal", "reference", "range", "interval", "bio. ref")

# A row needs at least a name and a value to be worth keeping.
_MIN_COLUMNS = 2


def _is_section_title(line: str) -> bool:
    line = line.strip()
    return bool(
        len(line) > 4
        and line == line.upper()
        and any(c.isalpha() for c in line)
        and ":" not in line
        and not line[0].isdigit()
        and _SECTION_KEYWORDS_RE.search(line)
    )


def _page_section_title(page_text: str) -> str | None:
    for line in page_text.split("\n"):
        if _is_section_title(line):
            return line.strip().title()
    return None


def _column_index(header: list[str], candidates: tuple[str, ...]) -> int | None:
    for i, cell in enumerate(header):
        if any(c in cell for c in candidates):
            return i
    return None


def _is_abnormal(value: str, normal_range: str) -> bool:
    bounds = _RANGE_BOUNDS_RE.search(normal_range)
    if bounds is None:
        return False
    try:
        val = float(value.lstrip("<> ").strip())
        low, high = float(bounds.group(1)), float(bounds.group(2))
    except ValueError:
        return False
    return val < low or val > high


def _build_parameter(name: str, value: str, normal_range: str) -> dict[str, Any]:
    unit = _UNIT_RE.search(normal_range)
    return {
        "name": name,
        "value": value,
        "normal_range": normal_range,
        "unit": unit.group(0).upper() if unit else "",
        "abnormal": _is_abnormal(value, normal_range),
    }


def _rows_to_parameters(rows: list[list[str]]) -> list[dict[str, Any]]:
    """Turns raw table rows into parameters, using the header row to decide which
    column is the name, the value and the reference interval."""
    if len(rows) < 2:
        return []

    header = [(cell or "").strip().lower() for cell in rows[0]]
    i_name = _column_index(header, _NAME_HEADERS)
    i_value = _column_index(header, _VALUE_HEADERS)
    i_range = _column_index(header, _RANGE_HEADERS)

    if i_name is None or i_value is None:
        # No recognizable header - fall back to the conventional ordering, but only
        # when the shape is right, so arbitrary non-table rows aren't misread.
        if len(header) < _MIN_COLUMNS:
            return []
        i_name, i_value = 0, 1
        i_range = 2 if len(header) > 2 else None
        data_rows = rows
    else:
        data_rows = rows[1:]

    parameters: list[dict[str, Any]] = []
    for row in data_rows:
        if max(i_name, i_value) >= len(row):
            continue
        name = (row[i_name] or "").strip()
        value = (row[i_value] or "").strip()
        normal_range = ""
        if i_range is not None and i_range < len(row):
            normal_range = (row[i_range] or "").strip()

        # A real result row has a label and a numeric reading.
        if not name or not value or not _NUMERIC_RE.match(value):
            continue
        if not any(c.isalpha() for c in name):
            continue
        parameters.append(_build_parameter(name, value, normal_range))

    return parameters


def _tables_from_ruled_lines(page: Any) -> list[list[list[str]]]:
    return page.extract_tables(
        {"vertical_strategy": "lines", "horizontal_strategy": "lines"}
    )


def _tables_from_word_positions(
    page: Any, y_tolerance: float = 3.0, column_gap: float = 12.0
) -> list[list[list[str]]]:
    """Rebuilds rows for tables that aren't drawn with any grid: group words whose
    vertical position matches, then break each group into cells wherever there's a
    wide horizontal gap between consecutive words."""
    words = page.extract_words()
    if not words:
        return []

    buckets: dict[int, list[dict[str, Any]]] = {}
    for word in words:
        key = round(word["top"] / y_tolerance)
        buckets.setdefault(key, []).append(word)

    rows: list[list[str]] = []
    for key in sorted(buckets):
        ordered = sorted(buckets[key], key=lambda w: w["x0"])
        cells: list[list[dict[str, Any]]] = []
        current = [ordered[0]]
        for previous, word in zip(ordered, ordered[1:], strict=False):
            if word["x0"] - previous["x1"] > column_gap:
                cells.append(current)
                current = [word]
            else:
                current.append(word)
        cells.append(current)
        rows.append([" ".join(w["text"] for w in cell) for cell in cells])

    return [rows] if rows else []


def _sections_from_page(page: Any, tables: list[list[list[str]]]) -> list[dict[str, Any]]:
    title = _page_section_title(page.extract_text() or "")
    sections = []
    for table in tables:
        parameters = _rows_to_parameters(table)
        if parameters:
            sections.append({"title": title, "parameters": parameters})
    return sections


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
            page_ruled = _sections_from_page(page, _tables_from_ruled_lines(page))
            if page_ruled:
                ruled.extend(page_ruled)
                continue
            # Only pay for the word-position pass on pages the grid strategy
            # couldn't read, so a normal ruled report doesn't do the work twice.
            clustered.extend(_sections_from_page(page, _tables_from_word_positions(page)))

    if ruled and clustered:
        return ruled + clustered, "ruled_lines+word_positions"
    if ruled:
        return ruled, "ruled_lines"
    if clustered:
        return clustered, "word_positions"
    return [], None
