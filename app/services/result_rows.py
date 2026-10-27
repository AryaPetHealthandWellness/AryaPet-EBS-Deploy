"""Reads test-result rows of any panel - chemistry, haematology, thyroid, urinalysis,
rapid antigen tests - from either a line of text or a row of table cells.

Labs print the same information in many layouts:

    Albumin 2.5 2.2 – 4.0 g/dl              name value range unit
    Haemoglobin 9.8 g/dL 12.0 - 18.0         name value unit range
    TSH 0.9 ng/mL < 0.6 H                    one-sided range, flag
    Protein Trace Negative                   qualitative value and expectation
    Creatinine 0.6 – 1.4 mg/dl  /  1.5       value printed on the line after its range

so a row is recognised by its parts (a name, then a value, then any of unit / range /
flag in any order) rather than by one fixed column order or panel name. A line is only
accepted as a result when something backs that up - a reference range, an H/L flag, a
known analyte name, or a header row ("Test  Result  Reference") above it - so
letterhead and patient-detail lines ("Age 6.0 years") aren't mistaken for results.

Used by both pdf_table_extractor (cell rows from the PDF's geometry) and
record_extractor (lines of a text layer or the client's OCR text).
"""

import re
from typing import Any

from app.services.analyte_gazetteer import canonical_analyte

# ── Building blocks ────────────────────────────────────────────────────────

_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
# A reading: a number standing on its own (not part of "T4", "x10^3" or a date).
_VALUE_RE = re.compile(
    rf"(?<![\w/.^])(?P<v>[<>≤≥]?\s?(?:{_NUMBER}))(?![\w^]|[./]\d)"
)
# A date repeats its separator ("21.09.2025", "26/10/26"); a decimal range such as
# "12.8-53.5" or "6.0-17.0" mixes them, and must not read as one.
_DATE_RE = re.compile(r"\b\d{1,2}([./-])\d{1,2}\1\d{2,4}\b(?![.]\d)")
_QUALITATIVE = (
    r"non[- ]?reactive|reactive|not detected|detected|not seen|seen|positive|negative|"
    r"absent|present|nil|trace|normal|abnormal|\+{1,4}|[1-4]\+"
)
_QUALITATIVE_VALUE_RE = re.compile(rf"(?<!\w)(?P<v>{_QUALITATIVE})(?![\w+])", re.IGNORECASE)
# Values that only ever mean a test outcome, safe to accept from a "Name: value" line.
_TEST_OUTCOME_RE = re.compile(
    r"^(non[- ]?reactive|reactive|not detected|detected|positive|negative|weak positive)\.?$",
    re.IGNORECASE,
)

_RANGE_RE = re.compile(
    rf"(?P<lo>{_NUMBER})\s*(?:[-–—−]{{1,2}}|to)\s*(?P<hi>{_NUMBER})"
    rf"|(?P<op>[<>≤≥]|up\s?to|less than|below|more than|above)\s*(?P<bound>{_NUMBER})",
    re.IGNORECASE,
)
_QUALITATIVE_RANGE_RE = re.compile(
    r"(?<!\w)(non[- ]?reactive|not detected|negative|nil|absent|normal)(?!\w)", re.IGNORECASE
)
_UNIT_RE = re.compile(
    r"(?<![\w/])("
    r"%|g/dl|mg/dl|[µu]g/dl|mcg/dl|ng/dl|ng/ml|pg/ml|[µu]g/l|ug/ml|u/l|iu/l|miu/ml|[µu]iu/ml|"
    r"mmol/l|[µu]mol/l|meq/l|mosm/kg|g/l|mg/l|mg/dl|"
    r"(?:cells|thou|lakhs?|mill(?:ion)?s?)?/(?:cumm|cmm|[µu]l|mm3)|"
    r"x?\s?10\^?\d+\s?/\s?(?:[µu]l|l)|"
    r"fl|pg|mm/hr|mm/1st hr|sec(?:onds)?|/hpf|/lpf|kg|bpm|mmhg|°\s?[fc]"
    r")(?![\w/])",
    re.IGNORECASE,
)
_FLAG_RE = re.compile(r"\s+(?P<flag>H|L|HH|LL|High|Low|\*|↑|↓)\s*$")
_FLAG_TOKEN_RE = re.compile(r"H|L|HH|LL|High|Low|↑|↓")
_LONE_VALUE_RE = re.compile(rf"^(?:[<>]?\s?(?:{_NUMBER})|{_QUALITATIVE})$", re.IGNORECASE)

# Names that are identifiers or patient details, never results.
_NOT_A_RESULT_RE = re.compile(
    r"\b(id|no|number|reg(?:istration)?|phone|mobile|contact|age|page|invoice|bill|amount|"
    r"date|dob|year|years|months?|pin|zip|plot|road|street|sex|gender|breed|species|"
    r"owner|name|dr|doctor|ref(?:erred)?|lab no|sample|specimen|report|receipt|"
    r"remarks?|comments?|impression|interpretation|conclusion|diagnosis|notes?|advice)\b",
    re.IGNORECASE,
)
_PROSE_WORD_RE = re.compile(
    r"\b(was|were|is|are|be|been|has|have|had|with|within|noted|showed?|shows|seen in|"
    r"after|before|given|done|advised|please|for|the)\b",
    re.IGNORECASE,
)

# ── Headers and titles ─────────────────────────────────────────────────────

_ROLE_HEADERS: dict[str, tuple[str, ...]] = {
    # Checked in this order so a cell is claimed by the most specific role.
    "unit": ("unit", "units", "uom"),
    "flag": ("flag", "status", "h/l"),
    "range": ("normal", "reference", "range", "interval", "bio. ref", "ref", "expected", "limits"),
    "value": ("value", "result", "observed", "reading", "finding"),
    "name": ("parameter", "test", "analyte", "investigation", "examination", "description",
             "component", "assay"),
}
_HEADER_WORD_RE = {
    role: re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.IGNORECASE)
    for role, words in _ROLE_HEADERS.items()
}

_PANEL_TITLE_RE = re.compile(
    r"\b(complete blood count|blood count|cbc|ha?emogram|ha?ematology|biochemistry|"
    r"chemistry|function tests?|liver|kidney|renal|hepatic|lipid|thyroid|electrolytes?|"
    r"urine|urinalysis|stool|fa?ecal|serology|coagulation|rapid test|snap test|antigen|"
    r"antibody|hormone|profile|panel|screen|culture|smear|analysis|test report|tests?)\b",
    re.IGNORECASE,
)


def header_roles(cells: list[str]) -> dict[str, int] | None:
    """Column index per role ("name", "value", "range", "unit", "flag") when the row is
    a results table's header, else None. Needs at least a name and a value column."""
    roles: dict[str, int] = {}
    for index, cell in enumerate(cells):
        text = (cell or "").strip()
        if not text or any(c.isdigit() for c in text):
            continue
        for role, regex in _HEADER_WORD_RE.items():
            if role not in roles and regex.search(text):
                roles[role] = index
                break
    if "name" in roles and "value" in roles:
        return roles
    return None


def is_header_line(line: str) -> bool:
    """A text line like "Test  Result  Unit  Reference Range"."""
    if any(c.isdigit() for c in line) or ":" in line:
        return False
    found = {role for role, regex in _HEADER_WORD_RE.items() if regex.search(line)}
    return {"name", "value"} <= found or ({"value", "range"} <= found and len(line.split()) <= 8)


def is_panel_title(line: str) -> bool:
    """A table's heading: short, no digits or "label:", and either printed in capitals
    ("COMPLETE BLOOD COUNT") or naming a kind of test ("Thyroid Profile")."""
    line = line.strip()
    words = line.split()
    if not (1 <= len(words) <= 8) or len(line) < 3 or ":" in line or line.startswith("*"):
        return False
    if any(c.isdigit() for c in line.replace("T4", "").replace("T3", "")):
        return False
    if is_header_line(line):
        return False
    letters = [c for c in line if c.isalpha()]
    if len(letters) < 3:
        return False
    if all(c.isupper() for c in letters) and len(words) >= 2:
        return True
    return bool(_PANEL_TITLE_RE.search(line)) and line[0].isupper() and not _PROSE_WORD_RE.search(line)


def title_text(line: str) -> str:
    """Display form of a title: capitals become Title Case, anything else is kept."""
    line = line.strip()
    letters = [c for c in line if c.isalpha()]
    return line.title() if letters and all(c.isupper() for c in letters) else line


# ── Rows ───────────────────────────────────────────────────────────────────


def _clean_name(name: str) -> str:
    name = re.sub(r"^(?:[-•*·]|\d{1,2}[.)])\s*", "", name.strip())
    return name.strip(" .:-–*")


def _bounds(reference: str) -> tuple[float | None, float | None] | None:
    m = _RANGE_RE.search(reference)
    if m is None:
        return None
    if m.group("lo") is not None:
        return _to_float(m.group("lo")), _to_float(m.group("hi"))
    bound = _to_float(m.group("bound"))
    op = m.group("op").lower()
    if op in ("<", "≤", "below", "less than") or op.startswith("up"):
        return None, bound
    return bound, None


def _to_float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text.replace(",", "").lstrip("<>≤≥ "))
    except ValueError:
        return None


def is_abnormal(value: str, reference: str, flag: str | None = None) -> bool | None:
    """True/False when the printed flag or reference range settles it, None otherwise."""
    if flag:
        return flag.upper() in ("H", "L", "HH", "LL", "HIGH", "LOW", "*", "↑", "↓")
    if not reference:
        return None
    qualitative = _QUALITATIVE_RANGE_RE.search(reference)
    if qualitative and not _RANGE_RE.search(reference):
        return value.strip().lower().replace("-", " ") != qualitative.group(1).lower().replace("-", " ")
    bounds = _bounds(reference)
    number = _to_float(value)
    if bounds is None or number is None:
        return None
    low, high = bounds
    return (low is not None and number < low) or (high is not None and number > high)


def _row(name: str, value: str, reference: str, unit: str, flag: str | None) -> dict[str, Any]:
    # "3.2--4.3" -> "3.2-4.3": one dash is what every reader of the range expects.
    reference = re.sub(r"(?<=\d)\s*--\s*(?=\d)", "-", reference)
    return {
        "name": name,
        "value": value,
        # As printed (range with its unit when they share a cell) - the previous card shape.
        "normal_range": " ".join(part for part in (reference, unit) if part),
        "unit": unit.upper(),
        "abnormal": bool(is_abnormal(value, reference, flag)),
        # Separated forms for the record: range without unit, unit in the lab's casing.
        "reference_range": reference,
        "unit_text": unit,
        "flag": flag,
    }


def parse_result_line(line: str) -> dict[str, Any] | None:
    """The parts of a "name value [unit] [range] [flag]" line, or None when it isn't
    shaped like one. `value` is None for a "name range" line whose value was printed on
    the next line. Whether the row is believable is left to `has_evidence`."""
    line = line.strip()
    if ":" in line or "=" in line or len(line) > 120 or _DATE_RE.search(line):
        return None

    flag = None
    m = _FLAG_RE.search(line)
    if m:
        flag = m.group("flag")
        line = line[: m.start()].rstrip()

    numeric = _VALUE_RE.search(line)
    qualitative = _QUALITATIVE_VALUE_RE.search(line)
    candidates = [c for c in (numeric, qualitative) if c is not None and c.start() > 0]
    if not candidates:
        return None
    first = min(candidates, key=lambda c: c.start())
    if re.search(r"\s--(?:\s|$)", line[: first.start()]):
        # "eGFRcr  --  0": "--" is the printed "not calculated"; the number after it
        # belongs to another column.
        return None
    name = _clean_name(line[: first.start()])
    if not name or not any(c.isalpha() for c in name) or len(name) > 60 or len(name.split()) > 7:
        return None
    if _NOT_A_RESULT_RE.search(name) or _PROSE_WORD_RE.search(name):
        return None

    # "Creatinine 0.6 – 1.4 mg/dl": the first number opens the range - no value here.
    # Checked from the number itself: the match can begin on the space before it, and
    # photo text has two between columns ("ALT  14-111 U/L"), which made a range's
    # lower bound read as the value.
    number_at = first.start() + len(first.group(0)) - len(first.group(0).lstrip())
    range_at_start = _RANGE_RE.match(line, number_at)
    if first is numeric and range_at_start and range_at_start.group("lo") is not None:
        value, rest = None, line[first.start() :]
    else:
        value, rest = first.group("v").strip(), line[first.end() :]

    unit = ""
    if value is not None:
        # Unit stuck to the value: "82%".
        glued = re.match(r"\s?(%)", rest)
        if glued:
            unit, rest = glued.group(1), rest[glued.end() :]

    reference = ""
    range_match = _RANGE_RE.search(rest)
    if range_match:
        reference = range_match.group(0).strip()
        rest = rest[: range_match.start()] + " " + rest[range_match.end() :]
    elif value is not None and qualitative is first:
        expected = _QUALITATIVE_RANGE_RE.search(rest)
        if expected:
            reference = expected.group(0)
            rest = rest[: expected.start()] + " " + rest[expected.end() :]

    unit_match = _UNIT_RE.search(rest)
    if unit_match and not unit:
        unit = unit_match.group(1)
        rest = rest[: unit_match.start()] + " " + rest[unit_match.end() :]

    # Whatever is left must be noise, not more words - otherwise it's a sentence.
    leftover = [w for w in rest.split() if any(c.isalpha() for c in w)]
    if len(leftover) > 1:
        return None
    if flag is None and leftover and _FLAG_TOKEN_RE.fullmatch(leftover[0]):
        # Analyzer exports print the flag in its own column mid-row:
        # "ALB  2.4  1.50  1.49  L  3.2--4.3  g/dL".
        flag = leftover[0]
    if value is None and not reference:
        return None

    return {"name": name, "value": value, "reference": reference, "unit": unit, "flag": flag}


def has_evidence(parts: dict[str, Any]) -> bool:
    """Whether a parsed line is believable as a result without a header row above it."""
    return bool(parts["reference"] or parts["flag"] or canonical_analyte(parts["name"]))


def outcome_pair(line: str) -> dict[str, Any] | None:
    """"Canine Parvovirus Antigen Test: Positive" - a named test with a plain outcome."""
    m = re.match(r"^(?P<name>[A-Za-z][^:=]{1,80}?)\s*[:\-–]\s*(?P<value>[A-Za-z -]+?)\s*$", line.strip())
    if m is None or not _TEST_OUTCOME_RE.match(m.group("value")):
        return None
    name = _clean_name(m.group("name"))
    if len(name.split()) > 8 or _NOT_A_RESULT_RE.search(name):
        return None
    return _row(name, m.group("value").rstrip("."), "", "", None)


def row_from_parts(parts: dict[str, Any]) -> dict[str, Any]:
    return _row(parts["name"], parts["value"], parts["reference"], parts["unit"], parts["flag"])


def row_from_cells(cells: list[str], roles: dict[str, int]) -> dict[str, Any] | None:
    """A result row from table cells whose columns were identified by `header_roles`."""

    def cell(role: str) -> str:
        index = roles.get(role)
        if index is None or index >= len(cells):
            return ""
        return " ".join((cells[index] or "").split())

    name = _clean_name(cell("name"))
    value = cell("value")
    if not name or not any(c.isalpha() for c in name) or not value:
        return None
    value_ok = _LONE_VALUE_RE.match(value) or _VALUE_RE.fullmatch(value)
    if not value_ok:
        # "9.8 g/dL" in the value cell: split the unit off.
        m = re.match(rf"^(?P<v>[<>]?\s?(?:{_NUMBER}))\s*(?P<u>\S+)$", value)
        if m is None or not _UNIT_RE.fullmatch(m.group("u")):
            return None
        value, unit_in_value = m.group("v"), m.group("u")
    else:
        unit_in_value = ""

    reference = cell("range")
    unit = cell("unit") or unit_in_value
    if reference and not unit:
        # Unit printed inside the range cell: "2.2 – 4.0 g/dl".
        unit_match = _UNIT_RE.search(reference)
        range_match = _RANGE_RE.search(reference)
        if unit_match and (range_match is None or unit_match.start() >= range_match.end()):
            unit = unit_match.group(1)
            reference = reference[: unit_match.start()].strip()
    flag = cell("flag") or None
    if flag and not re.fullmatch(r"H|L|HH|LL|High|Low|\*|↑|↓", flag, re.IGNORECASE):
        flag = None
    return _row(name, value, reference, unit, flag)


# ── Whole documents (text) ─────────────────────────────────────────────────

_SECTION_BREAK_RE = re.compile(
    r"^(\*{3,}|_{3,}|end of report|note\b|notes\b|remarks?\b|comments?\b|interpretation\b|"
    r"impression\b|advice\b|dr\.|m\.v\.)",
    re.IGNORECASE,
)


def sections_from_lines(lines: list[str]) -> list[dict[str, Any]]:
    """Result tables in a document's text, as [{"title", "parameters"}]. Rows under a
    header line are trusted; rows elsewhere need their own evidence (see module doc)."""
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_table = False
    misses = 0

    def add(row: dict[str, Any]) -> None:
        nonlocal current
        if current is None:
            current = {"title": None, "parameters": []}
            sections.append(current)
        current["parameters"].append(row)

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            continue
        if is_header_line(line):
            in_table, misses = True, 0
            continue

        outcome = outcome_pair(line)
        if outcome is not None:
            add(outcome)
            continue

        parts = parse_result_line(line)
        if parts is not None and parts["value"] is None and i < len(lines):
            following = lines[i].strip()
            if _LONE_VALUE_RE.match(following):
                parts["value"] = following
                i += 1
        if parts is not None and parts["value"] is not None and (in_table or has_evidence(parts)):
            add(row_from_parts(parts))
            misses = 0
            continue

        if is_panel_title(line):
            current = {"title": title_text(line), "parameters": []}
            sections.append(current)
            in_table, misses = False, 0
            continue
        if _SECTION_BREAK_RE.match(line):
            in_table = False
            continue
        misses += 1
        if misses >= 3:
            in_table = False

    return [s for s in sections if s["parameters"]]
