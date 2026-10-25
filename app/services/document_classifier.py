"""Classifies client-submitted OCR text and extracts a structured "card" from it,
using a self-hosted spaCy pipeline (en_core_web_sm) - lightweight enough to run
alongside the API on a t3.micro, unlike the transformer-based approach this replaced.

spaCy's small model has no zero-shot classification or medical NER, so:
- Classification is keyword-based (same idea as the very first Textract-era version
  of this module), matched against spaCy's lemmatized tokens rather than raw
  substrings - catches "diagnosed"/"diagnosis" alike, for example.
- Field extraction uses spaCy's built-in generic NER (DATE/ORG/PERSON) plus
  keyword-anchored sentence lookup (via doc.sents) for fields with no generic
  entity type, like diagnosis/treatment_plan.

This remains a first-pass heuristic, not a trained clinical NLP model. Lab reports
get an additional table-row walk (_extract_lab_sections) on top of the sentence-level
fields above, since a multi-parameter panel (LFT, KFT, CBC, ...) needs every row
captured, not just whichever one sentence mentions "test"/"result"/"panel". This
mirrors the same regex + gap-name technique the on-device Flutter parser uses
(lib/features/scanner/on_device_ocr.dart _parseSection): find each "value  range[unit]"
match in a test section's text, and treat the text between one match and the next as
that parameter's name - reliable for the common row-clean OCR layout ("Albumin 2.5
2.2-4.0 g/dl"), though (like the Flutter parser) it still won't reconstruct genuinely
column-jumbled table text where names and values sit in entirely separate blocks.

The spaCy pipeline is loaded once per process (lazily, via lru_cache) since loading
is comparatively expensive - see app/workers/processing_consumer.py, the only caller.
"""

import re
from functools import lru_cache
from typing import Any, Literal

from app.services.analyte_gazetteer import annotate_parameters
from app.services.pdf_table_extractor import extract_lab_sections_from_pdf

DocumentType = Literal["lab_report", "vet_visit", "unknown"]

_SPACY_MODEL = "en_core_web_sm"

_LAB_KEYWORDS = (
    "reference range",
    "specimen",
    "cbc",
    "wbc",
    "rbc",
    "hematology",
    "biochemistry",
    "test result",
    "panel",
)

_VET_VISIT_KEYWORDS = (
    "diagnosis",
    "diagnose",
    "clinic",
    "veterinarian",
    "treatment plan",
    "treatment",
    "physical exam",
    "vaccination",
    "presenting complaint",
)


@lru_cache(maxsize=1)
def _nlp() -> Any:
    import spacy

    return spacy.load(_SPACY_MODEL)


def _lemmatized_text(text: str) -> tuple[Any, str]:
    doc = _nlp()(text)
    lemmas = " ".join(token.lemma_.lower() for token in doc)
    return doc, lemmas


def classify_document(text: str) -> DocumentType:
    if not text.strip():
        return "unknown"

    _, lemmas = _lemmatized_text(text)
    has_lab_keyword = any(keyword in lemmas for keyword in _LAB_KEYWORDS)
    has_vet_keyword = any(keyword in lemmas for keyword in _VET_VISIT_KEYWORDS)

    if has_lab_keyword:
        return "lab_report"
    if has_vet_keyword:
        return "vet_visit"
    return "unknown"


def _first_entity(doc: Any, label: str) -> str | None:
    for ent in doc.ents:
        if ent.label_ == label:
            return ent.text
    return None


def _sentence_containing(doc: Any, *keywords: str) -> str | None:
    for sent in doc.sents:
        lower_sent = sent.text.lower()
        if any(keyword in lower_sent for keyword in keywords):
            return sent.text.strip()
    return None


def build_lab_report_card(text: str) -> dict[str, Any]:
    doc = _nlp()(text)
    card: dict[str, Any] = {
        "type": "lab_report",
        "collection_date": _first_entity(doc, "DATE"),
        "test_result": _sentence_containing(doc, "test", "result", "panel"),
    }
    sections = _extract_lab_sections(text)
    if sections:
        for section in sections:
            annotate_parameters(section["parameters"])
        card["sections"] = sections
    return card


def build_card_from_pdf(pdf_bytes: bytes) -> dict[str, Any] | None:
    """Builds a lab report card from the PDF's own table geometry instead of from
    flattened OCR text - see app/services/pdf_table_extractor.py for why that's a
    fundamentally better signal. Returns None when the PDF has no readable table,
    so callers fall back to the text-based path."""
    sections, strategy = extract_lab_sections_from_pdf(pdf_bytes)
    if not sections:
        return None

    for section in sections:
        annotate_parameters(section["parameters"])

    return {
        "type": "lab_report",
        "sections": sections,
        # Records which tier produced this so the share of documents falling through
        # to the lossy text parser is visible in the data rather than guessed at.
        "extraction": {"source": "pdf", "strategy": strategy},
    }


# ── Table-row extraction for multi-parameter lab panels ─────────────────────

_SECTION_TITLE_KEYWORDS_RE = re.compile(
    r"TEST|FUNCTION|PANEL|PROFILE|ANALYSIS|COUNT|SCREEN|CULTURE", re.IGNORECASE
)

# Fixed unit list, not a generic word-match fallback: a range with no unit at
# all (e.g. a plain ratio like "0.7-2.0") must NOT greedily swallow the next
# parameter's name as if it were an unrecognized unit.
_VALUE_RANGE_RE = re.compile(
    r"(\d+\.?\d*)\s+"
    r"([\d.]+\s*[–\-—−]\s*[\d.]+(?:\s*(?:g/dl|mg/dl|u/l|iu/l|mmol/l|umol/l|%))?)",
    re.IGNORECASE,
)

_UNIT_RE = re.compile(r"\b(g/dl|mg/dl|u/l|iu/l|mmol/l|umol/l)\b", re.IGNORECASE)
_RANGE_BOUNDS_RE = re.compile(r"([\d.]+)\s*[–\-—−]\s*([\d.]+)")

# "Creatinine 0.6 – 1.4 mg/dl" followed by "1.5" on its own line: some labs' text layer
# emits the value cell after the range cell. Recognised so the row isn't lost.
_RANGE_ONLY_ROW_RE = re.compile(r"^(?P<name>.*[A-Za-z].*?)\s+(?P<range>[\d.]+\s*[–\-—−]\s*[\d.]+.*)$")
_LONE_VALUE_RE = re.compile(r"^[<>]?\d+\.?\d*$")


def _is_section_title(line: str) -> bool:
    # Real section titles in these reports are printed fully capitalized
    # ("LIVER FUNCTION TEST"); requiring that rules out ordinary prose that
    # happens to contain one of the keywords (e.g. "...within panel.").
    return (
        len(line) > 4
        and line == line.upper()
        and any(c.isalpha() for c in line)
        and not line.startswith("*")
        and ":" not in line
        and not line[0].isdigit()
        and bool(_SECTION_TITLE_KEYWORDS_RE.search(line))
    )


def _section_value_range_block(lines: list[str]) -> str:
    """Text from just after a "...Value...Normal range..." header line up to
    the next footer marker (*, Note, Dr., M.V.) - the same anchor the
    Flutter parser uses to isolate a section's actual data rows from its
    title, header, and trailing signature/disclaimer lines."""
    kept: list[str] = []
    in_block = False
    for line in lines:
        lower = line.lower()
        if not in_block:
            if "value" in lower and "normal" in lower:
                in_block = True
            continue
        if line.startswith("*") or lower.startswith(("note", "dr.", "m.v.")):
            break
        kept.append(line)
    return " ".join(_reorder_trailing_values(kept))


def _reorder_trailing_values(lines: list[str]) -> list[str]:
    """Rewrites "<name> <range>" + "<value>" line pairs as "<name> <value> <range>" so
    the row regex sees the usual order. Only applies when the name doesn't already end
    in a number, i.e. the row really has no value of its own."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _RANGE_ONLY_ROW_RE.match(line)
        if (
            m is not None
            and i + 1 < len(lines)
            and _LONE_VALUE_RE.match(lines[i + 1])
            and not _LONE_VALUE_RE.match(m.group("name").split()[-1])
        ):
            out.append(f"{m.group('name')} {lines[i + 1]} {m.group('range')}")
            i += 2
            continue
        out.append(line)
        i += 1
    return out


def _extract_section_parameters(block: str) -> list[dict[str, Any]]:
    """Walks every "<name> <value> <range>[unit]" row in a section's data
    block. The name for each match is the text between the previous match's
    end and this match's start - reliable as long as name and value sit
    adjacent on the same line, which holds for the common row-clean OCR
    layout but not for column-jumbled table text (see module docstring)."""
    params: list[dict[str, Any]] = []
    last_end = 0
    for m in _VALUE_RANGE_RE.finditer(block):
        name = block[last_end:m.start()].strip()
        last_end = m.end()
        if not name or not name[0].isalpha():
            continue
        value = m.group(1)
        normal_range = m.group(2).strip()
        unit_match = _UNIT_RE.search(normal_range)
        params.append(
            {
                "name": name,
                "value": value,
                "normal_range": normal_range,
                "unit": unit_match.group(0).upper() if unit_match else "",
                "abnormal": _is_abnormal(value, normal_range),
            }
        )
    return params


def _is_abnormal(value: str, normal_range: str) -> bool:
    bounds = _RANGE_BOUNDS_RE.search(normal_range)
    if bounds is None:
        return False
    try:
        val = float(value)
        low, high = float(bounds.group(1)), float(bounds.group(2))
    except ValueError:
        return False
    return val < low or val > high


def extract_lab_sections(text: str) -> list[dict[str, Any]]:
    """Lab table sections parsed from flattened text - the fallback when there's no PDF
    geometry (photos). Used by app/services/record_extractor.py."""
    return _extract_lab_sections(text)


def _extract_lab_sections(text: str) -> list[dict[str, Any]]:
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    title_lines = [(i, line.title()) for i, line in enumerate(lines) if _is_section_title(line)]
    if not title_lines:
        return []

    sections: list[dict[str, Any]] = []
    for idx, (start, title) in enumerate(title_lines):
        end = title_lines[idx + 1][0] if idx + 1 < len(title_lines) else len(lines)
        block = _section_value_range_block(lines[start:end])
        sections.append({"title": title, "parameters": _extract_section_parameters(block)})
    return sections


def build_vet_visit_card(text: str) -> dict[str, Any]:
    doc = _nlp()(text)
    return {
        "type": "vet_visit",
        "visit_date": _first_entity(doc, "DATE"),
        "clinic_name": _first_entity(doc, "ORG"),
        "diagnosis": _sentence_containing(doc, "diagnosis", "diagnosed"),
        "treatment_plan": _sentence_containing(doc, "treatment"),
    }


def build_unknown_card(text: str) -> dict[str, Any]:
    return {"type": "unknown", "raw_text": text}


def build_document_card(text: str) -> dict[str, Any]:
    document_type = classify_document(text)
    if document_type == "lab_report":
        return build_lab_report_card(text)
    if document_type == "vet_visit":
        return build_vet_visit_card(text)
    return build_unknown_card(text)
