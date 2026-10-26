"""Builds a pet record shaped like the app's Add Document form from a document's text
(and, for PDFs, its table geometry).

One output field per form field, in form order:

  document_type        one of DOCUMENT_TYPES, or None when the evidence is absent or
                       split between types (abstain rather than guess)
  visit_date           ISO date of the visit / test result, or None
  document_title       the document's own heading(s), or None
  clinic_provider      the issuing clinic / lab as printed, or None
  tags                 subset of UI_TAGS, each backed by wording in the document body
  primary_diagnosis    the clinician's own wording, or None
  parameters           every measured value: table rows plus "Name = value unit" lines
  results_notes        remarks/interpretation printed with the results, or None
  symptoms             complaint items, excluding negated ones ("No emesis")
  medications          drug_name / dosage / frequency, frequency left None when unstated
  clinical_notes       observations and remaining notes, or None
  clinical_directives  follow-up instructions

followed by fields the form doesn't have yet:

  vaccinations         vaccine / date_given / batch / next_due, as printed
  procedures           procedures performed (surgery, dressing, imaging study ...)

Everything is copied from the source text, not inferred: no normal ranges are supplied,
no frequency is defaulted, and no diagnosis is derived from values. Missing fields stay
None / []. This is deterministic rule-based extraction (no spaCy), so the same document
always produces the same record.

`type` and `sections` are also emitted in the previous card's shape so the current app
keeps rendering lab tables until it reads the fields above.
"""

import re
from datetime import date
from typing import Any

from app.services.analyte_gazetteer import annotate_parameters
from app.services.pdf_table_extractor import extract_lab_sections_from_pdf, extract_pdf_text
from app.services.result_rows import is_header_line, is_panel_title, sections_from_lines

DOCUMENT_TYPES = ("vet_visit", "lab_report", "surgery", "vaccination", "prescription", "other")
UI_TAGS = (
    "Wellness",
    "Dental",
    "Surgery",
    "Bloodwork",
    "Vaccines",
    "Routine",
    "Preventive Care",
    "Emergency",
)

# ── Line hygiene ────────────────────────────────────────────────────────────

# Printed disclaimers, footers and signature lines. Matching against them would tag a
# record from "SPECIFIC TREATMENT OF OTHER PETS" or a lab's legal small print.
_BOILERPLATE_RE = re.compile(
    r"please (note|contact|call)|powered by|generated on|authori[sz]ed signature|"
    r"page \d+ of \d+|end of report|results depend on|only an aid|medico|"
    r"inter ?laboratory|correlated with the clinical|^_{3,}|^[*\s]+$",
    re.IGNORECASE,
)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.split("\n") if line.strip()]


def _is_boilerplate(line: str) -> bool:
    if _BOILERPLATE_RE.search(line):
        return True
    # Long all-capitals lines are notices, not clinical content.
    letters = [c for c in line if c.isalpha()]
    return len(line) > 40 and bool(letters) and all(c.isupper() for c in letters)


# ── Labelled sections ("Complaint ...", "Diagnosis: ...") ─────────────────

_SECTION_LABELS: dict[str, str] = {
    "complaint": r"(?:chief |presenting )?complaints?|reason for visit|presenting signs|c/o",
    "observations": r"observations?|clinical findings|findings|physical exam(?:ination)?|"
    r"examination|vitals|study|views?|technique",
    "diagnosis": r"(?:provisional |tentative |final |differential )?diagnosis|assessment|impression",
    "notes": r"notes|plan|treatment(?: plan)?|advice|recommendations?|instructions",
    "medications": r"rx|prescription|medications?|medicines|drugs prescribed",
    "results_notes": r"remarks?|comments?|interpretation",
    "procedures": r"procedures?(?: performed| done)?|surgery (?:performed|done)|surgical procedure|"
    r"operation(?: performed)?",
}
_LABEL_RES = {
    key: re.compile(rf"^(?:{pattern})\b\s*(?P<sep>[:\-–]\s*)?(?P<rest>.*)$", re.IGNORECASE)
    for key, pattern in _SECTION_LABELS.items()
}
# A block ends at these even when no new label follows.
_BLOCK_END_RE = re.compile(r"^(next visit|authori[sz]ed|end of report|\*{3,}|_{3,})", re.IGNORECASE)


def _section_label(line: str) -> tuple[str, str] | None:
    """(section, text after the label) when the line opens a labelled section.

    The label must be followed by a separator or by a capitalised word, so prose like
    "Treatment of the wound was..." isn't mistaken for a "Treatment" heading."""
    for key, regex in _LABEL_RES.items():
        m = regex.match(line)
        if m is None:
            continue
        rest = m.group("rest").strip()
        if m.group("sep") or not rest or rest[0].isupper():
            return key, rest
    return None


def _labelled_sections(lines: list[str]) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        label = _section_label(line)
        if label is not None:
            current, rest = label
            sections.setdefault(current, [])
            if rest:
                sections[current].append(rest)
            continue
        if _BLOCK_END_RE.match(line) or _is_boilerplate(line):
            current = None
            continue
        if current is not None:
            sections[current].append(line)
    return sections


# ── Dates ───────────────────────────────────────────────────────────────────

_MONTHS = {
    m: i
    for i, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for m in names
}
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_RES = (
    # 21.09.2025 / 21/09/2025 / 21-09-25 - read day first, as Indian clinics print them.
    re.compile(r"\b(?P<d>\d{1,2})[./-](?P<m>\d{1,2})[./-](?P<y>\d{4}|\d{2})\b"),
    # 2025-09-21
    re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b"),
    # 16 Dec, 2022 / 16th December 2022
    re.compile(
        rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<mon>{_MONTH_NAMES})\.?,?\s+(?P<y>\d{{4}})\b", re.I
    ),
    # December 16, 2022
    re.compile(
        rf"\b(?P<mon>{_MONTH_NAMES})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y>\d{{4}})\b", re.I
    ),
)
# Most to least specific. "Generated on" is last: it's when the PDF was printed.
_DATE_LABELS = (
    r"visit date|date of visit|consultation date",
    r"date of (?:test )?result|result date|report(?:ed)? date|date of report",
    r"(?:sample|specimen) (?:received|collected|collection) date|collection date|"
    r"date of collection|(?:sample|specimen) (?:received|collected)(?: on)?",
    r"\bdate\b",
    r"generated on",
)


def _parse_date(text: str) -> str | None:
    for regex in _DATE_RES:
        m = regex.search(text)
        if m is None:
            continue
        groups = m.groupdict()
        try:
            month = _MONTHS[groups["mon"].lower()] if groups.get("mon") else int(groups["m"])
            year = int(groups["y"])
            if year < 100:
                year += 2000
            return date(year, month, int(groups["d"])).isoformat()
        except (ValueError, KeyError):
            continue
    return None


def _find_date(text: str) -> str | None:
    for label in _DATE_LABELS:
        for m in re.finditer(rf"(?:{label})\s*[:\-]?\s*", text, re.IGNORECASE):
            # Only the text right after the label, so "Date:" doesn't borrow a date
            # printed further along the line.
            parsed = _parse_date(text[m.end() : m.end() + 25])
            if parsed:
                return parsed
    return None


# ── Header fields ──────────────────────────────────────────────────────────

_PROVIDER_RE = re.compile(
    r"\b(clinic|hospital|diagnostics?|laborator(?:y|ies)|labs?|pet care|animal care|"
    r"vet(?:erinary)? (?:centre|center|care))\b",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(
    r"\b(clinical notes|consultation (?:notes|record|summary)|case sheet|discharge summary|"
    r"prescription|vaccination (?:certificate|record|card)|immuni[sz]ation record|"
    r"operative report|surgery report|surgical report|anaest?hesia record|"
    r"radiology report|x-?ray report|ultrasound report|lab(?:oratory)? report|test report|invoice)\b",
    re.IGNORECASE,
)


def _find_provider(lines: list[str]) -> str | None:
    # Letterhead sits at the top; a clinic named in the body is usually a referral.
    for line in lines[:10]:
        if "@" in line or "www." in line.lower() or _is_boilerplate(line):
            continue
        if _PROVIDER_RE.search(line):
            return line
    return None


def _find_title(lines: list[str], table_sections: list[dict[str, Any]]) -> str | None:
    titles = [s["title"] for s in table_sections if s.get("title")]
    if titles:
        return "; ".join(dict.fromkeys(titles))
    for line in lines:
        m = _TITLE_RE.search(line)
        if m and not _is_boilerplate(line):
            return m.group(1).title()
    return None


# ── Document type ──────────────────────────────────────────────────────────

_TYPE_SIGNALS: dict[str, tuple[str, ...]] = {
    "lab_report": (
        r"reference (?:range|interval)",
        r"normal range",
        r"function test",
        r"\bcbc\b",
        r"ha?ematology",
        r"biochemistry",
        r"specimen",
        r"sample (?:received|collected)",
        r"lab(?:oratory)? report",
        r"test result",
        r"test report",
        r"rapid test|snap test",
        r"\bantigen\b|\bantibod(?:y|ies)\b|\belisa\b|\bpcr\b",
        r"urinalysis|urine (?:routine|analysis|examination)",
        r"blood count|ha?emogram",
        r"thyroid (?:profile|panel)",
    ),
    "vet_visit": (r"clinical notes", r"consultation", r"case sheet"),
    "prescription": (r"^\s*rx\b", r"\bprescription\b"),
    "vaccination": (
        r"vaccination (?:certificate|record|card)",
        r"vaccines? (?:given|administered)",
        r"immuni[sz]ation record",
        r"(?:batch|lot) no",
    ),
    "surgery": (
        r"operative report",
        r"surg(?:ery|ical) report",
        r"procedure performed",
        r"anaest?hesia (?:record|protocol)",
        r"post-?op(?:erative)? (?:notes|care)",
    ),
    "other": (
        r"radiograph",
        r"x-?ray report",
        r"ultrasound report",
        r"radiology report",
        r"\binvoice\b",
        r"\bestimate\b",
    ),
}


def _title_lines(lines: list[str]) -> str:
    """The document's own headings near the top ("Radiology Report", "COMPLETE BLOOD
    COUNT") - what the document says it is, which outweighs words in its body."""
    return "\n".join(
        line
        for line in lines[:15]
        if not _is_boilerplate(line)
        and ((_TITLE_RE.search(line) and len(line.split()) <= 6) or is_panel_title(line))
    )


def _classify(
    body: str,
    titles: str,
    sections: dict[str, list[str]],
    result_rows: int,
    vaccinations: int,
) -> str | None:
    scores = {doc_type: 0 for doc_type in DOCUMENT_TYPES}
    for doc_type, signals in _TYPE_SIGNALS.items():
        for signal in signals:
            if re.search(signal, body, re.IGNORECASE | re.MULTILINE):
                scores[doc_type] += 1
            if titles and re.search(signal, titles, re.IGNORECASE | re.MULTILINE):
                scores[doc_type] += 2
    # A visit note is recognised by its clinical headings more than by any word.
    scores["vet_visit"] += sum(1 for key in ("complaint", "observations", "diagnosis") if key in sections)
    if "medications" in sections:
        scores["prescription"] += 1
    if "procedures" in sections:
        scores["surgery"] += 1
    if result_rows >= 2:
        scores["lab_report"] += 2
    scores["vaccination"] += min(vaccinations, 2)

    best = max(scores.values())
    if best == 0:
        return None
    winners = [doc_type for doc_type, score in scores.items() if score == best]
    # A tie means the document reads as two different things - abstain for review
    # rather than pick by an arbitrary precedence.
    return winners[0] if len(winners) == 1 else None


# ── Tags (only the app's chips) ────────────────────────────────────────────

_TAG_RES: dict[str, re.Pattern[str]] = {
    tag: re.compile(pattern, re.IGNORECASE)
    for tag, pattern in {
        "Wellness": r"\bwellness\b|\bgeneral health (?:exam|check)",
        "Dental": r"\b(dental|teeth|tooth|tartar|gingiv\w*|periodont\w*|scaling|dentistry)\b",
        "Surgery": r"\b(surgery|surgical|operation|operative|spay\w*|neuter\w*|castrat\w*|"
        r"ovariohysterectomy|sutures?|stitches)\b",
        "Bloodwork": r"\b(cbc|blood (?:test|work|panel|profile|count|smear)|ha?ematology|"
        r"biochemistry|serum|liver function|kidney function|renal function|lft|kft)\b",
        "Vaccines": r"\b(vaccin\w*|immuni[sz]\w*|booster)\b",
        # "Urine routine examination" is a test name, not a routine visit.
        "Routine": r"(?<!urine )\b(routine|annual (?:check|exam|visit)\w*|health check\w*|check-?ups?)\b",
        "Preventive Care": r"\b(preventi\w+|de-?worm\w*|anthelmintic|heartworm prevent\w*|"
        r"flea (?:and|&) tick prevent\w*)\b",
        "Emergency": r"\b(emergency|urgent|critical care|casualty|triage)\b",
    }.items()
}


_NON_BLOOD_RE = re.compile(r"urine|urinalysis|stool|fa?ecal|faeces|feces", re.IGNORECASE)


def _tags(body: str, parameters: list[dict[str, Any]], document_type: str | None) -> list[str]:
    tags = {tag for tag, regex in _TAG_RES.items() if regex.search(body)}
    # A table of recognised blood analytes is bloodwork even if the report never says so
    # (urine and stool panels share analyte names like Glucose, so they don't count).
    if any(
        p.get("canonical_name") and p.get("section") and not _NON_BLOOD_RE.search(p["section"])
        for p in parameters
    ):
        tags.add("Bloodwork")
    if document_type == "vaccination":
        tags.add("Vaccines")
    if document_type == "surgery":
        tags.add("Surgery")
    return [tag for tag in UI_TAGS if tag in tags]


# ── Parameters ─────────────────────────────────────────────────────────────

_UNIT_IN_RANGE_RE = re.compile(r"\s*([a-zA-Zµ%][\w/%µ^*.]*(?:/[\w.]+)?)\s*$")
# "Rectal Temp = 101 *F". '=' is unambiguous; ':' is only trusted for vital-sign names,
# since "Registration Number: 1978" has the same shape.
_EQUALS_PAIR_RE = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z .()/%-]{0,40}?)\s*=\s*(?P<value>[<>]?\d+(?:\.\d+)?)\s*(?P<unit>\S{0,12})\s*$"
)
_COLON_PAIR_RE = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z .()/%-]{0,40}?)\s*:\s*(?P<value>[<>]?\d+(?:\.\d+)?)\s*(?P<unit>\S{0,12})\s*$"
)
_VITAL_NAME_RE = re.compile(
    r"\b(temp(?:erature)?|weight|wt|heart rate|hr|pulse|resp(?:iration|iratory rate)?|rr|"
    r"crt|bcs|body condition|spo2|blood pressure|bp)\b",
    re.IGNORECASE,
)
_NOT_A_PARAMETER_RE = re.compile(
    r"\b(id|no|number|reg(?:istration)?|phone|mobile|contact|age|page|invoice|bill|amount)\b",
    re.IGNORECASE,
)


def _split_unit(normal_range: str) -> tuple[str, str]:
    """('2.2 – 4.0', 'g/dl') from '2.2 – 4.0 g/dl', keeping the lab's own casing."""
    m = _UNIT_IN_RANGE_RE.search(normal_range)
    if m is None or not any(c.isalpha() or c == "%" for c in m.group(1)):
        return normal_range.strip(), ""
    return normal_range[: m.start()].strip(), m.group(1)


def _table_parameters(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parameters = []
    for section in sections:
        for row in section["parameters"]:
            if "reference_range" in row:
                reference_range, unit = row["reference_range"], row.get("unit_text", "")
            else:
                reference_range, unit = _split_unit(row.get("normal_range", ""))
            parameter = {
                "name": row["name"],
                "value": row["value"],
                "unit": unit,
                "reference_range": reference_range,
                "section": section.get("title"),
                # Only claimed when the printed range or H/L flag settles it.
                "abnormal": row.get("abnormal") if (reference_range or row.get("flag")) else None,
            }
            if row.get("flag"):
                parameter["flag"] = row["flag"]
            if row.get("canonical_name"):
                parameter["canonical_name"] = row["canonical_name"]
            parameters.append(parameter)
    return parameters


def _pair_parameter(line: str) -> dict[str, Any] | None:
    m = _EQUALS_PAIR_RE.match(line)
    if m is None:
        m = _COLON_PAIR_RE.match(line)
        if m is None or not _VITAL_NAME_RE.search(m.group("name")):
            return None
    name = m.group("name").strip()
    if _NOT_A_PARAMETER_RE.search(name):
        return None
    return {
        "name": name,
        "value": m.group("value"),
        "unit": m.group("unit"),
        "reference_range": "",
        "section": None,
        "abnormal": None,
    }


# ── Symptoms, directives, medications ─────────────────────────────────────

_NEGATED_RE = re.compile(r"^(no|not|nil|denies|without|absence of|negative for)\b", re.IGNORECASE)
# Statements that the pet is well are status, not symptoms.
_NORMAL_STATUS_RE = re.compile(
    r"^(active|alert|bright|responsive|eating (?:well|normally)|normal)\b[\s&,and]*"
    r"(active|alert|bright|responsive)?\.?$",
    re.IGNORECASE,
)
_REPORT_LEADIN_RE = re.compile(
    r"^(?:(?:owner |it was )?reported that (?:the )?pet has|owner reports?|presented with|"
    r"pet has|c/o)\s+",
    re.IGNORECASE,
)
_DIRECTIVE_RE = re.compile(
    r"\b(review|follow[- ]?up|re-?check|revisit|return|come back|repeat|continue|call|contact|"
    r"avoid|keep|apply|give|administer|monitor|restrict|schedule|book|removal|remove)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=\.)\s+(?=[A-Z])")

_DOSE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|µg|g|ml|iu|units?|tabs?|tablets?|caps?|drops?)(?:\s*/\s*kg)?\b",
    re.IGNORECASE,
)
_FREQUENCY_RE = re.compile(
    r"\b(SID|BID|TID|QID|q\d{1,2}h|OD|BD|TDS|QDS|EOD|PRN|once (?:a )?daily|twice (?:a )?daily|"
    r"thrice daily|once a day|twice a day|three times a day|every \d+ hours|[01]-[01]-[01])\b",
    re.IGNORECASE,
)
# Printed frequency -> the app's dropdown value. Anything unmapped stays as printed.
_FREQUENCY_CODES = {
    "sid": "SID", "od": "SID", "once daily": "SID", "once a daily": "SID", "once a day": "SID",
    "bid": "BID", "bd": "BID", "twice daily": "BID", "twice a daily": "BID", "twice a day": "BID",
    "tid": "TID", "tds": "TID", "thrice daily": "TID", "three times a day": "TID",
    "qid": "QID", "qds": "QID",
}
_DRUG_PREFIX_RE = re.compile(r"^(?:[-•*\d.)\s]+)?(?:tab|cap|syp|inj|oint|susp)\.?\s+", re.IGNORECASE)


def _symptoms(complaint_lines: list[str]) -> tuple[list[str], list[str]]:
    """(symptoms, leftover lines for clinical notes). Negated and 'well' statements are
    kept as notes so 'No emesis' never shows up as a symptom chip."""
    symptoms, leftover = [], []
    for line in complaint_lines:
        for sentence in _SENTENCE_SPLIT_RE.split(line):
            sentence = sentence.strip()
            if not sentence:
                continue
            if _NEGATED_RE.search(sentence) or _NORMAL_STATUS_RE.match(sentence):
                leftover.append(sentence)
                continue
            text = _REPORT_LEADIN_RE.sub("", sentence).rstrip(".").strip()
            symptoms.append(text[:1].upper() + text[1:])
    return symptoms, leftover


def _medication(line: str) -> dict[str, Any] | None:
    dose = _DOSE_RE.search(line)
    frequency = _FREQUENCY_RE.search(line)
    if dose is None and frequency is None:
        return None
    cut = min(m.start() for m in (dose, frequency) if m is not None)
    drug_name = _DRUG_PREFIX_RE.sub("", line[:cut]).strip(" -,:")
    if not drug_name:
        return None
    frequency_text = frequency.group(1) if frequency else None
    return {
        "drug_name": drug_name,
        "dosage": dose.group(0) if dose else None,
        # Never defaulted: an unstated frequency is None, not SID.
        "frequency": _FREQUENCY_CODES.get(frequency_text.lower(), frequency_text) if frequency_text else None,
        "frequency_text": frequency_text,
    }


# ── Vaccinations ──────────────────────────────────────────────────────────

_VACCINE_NAME_RE = re.compile(
    r"\b(anti-?rabies|rabies|dhpp\w*|da2pp\w*|dhlpp\w*|parvo\w*|distemper|leptospir\w*|lepto|"
    r"bordetella|kennel cough|corona\w*|canine influenza|tricat\w*|fvrcp|felv|feline leuk\w*|"
    r"panleuk\w*|calici\w*|rhinotrach\w*|nobivac|canigen|megavac|vanguard|defensor|eurican|"
    r"recombitek|biocan|purevax|felocell|\d\s?-?in\s?-?1)\b",
    re.IGNORECASE,
)
_VACCINE_LABEL_RE = re.compile(r"^\s*(?:vaccines?|vaccinations?|immuni[sz]ations?)\s*[:\-–]\s*", re.I)
# Where a vaccine's name ends on a "Vaccine: X  Batch No: ..." line.
_VACCINE_FIELD_RE = re.compile(
    r"\s{2,}|\b(?:batch|lot|next due|due date|due on|due|next dose|date|given on|given|"
    r"administered|expiry|exp|mfg|manufacturer)\b",
    re.IGNORECASE,
)
_BATCH_RE = re.compile(r"\b(?:batch|lot)\s*(?:no\.?|number|#)?\s*[:\-]?\s*(?P<b>[A-Za-z0-9][\w/-]*)", re.I)
_NEXT_DUE_RE = re.compile(
    r"\b(?:next due|due date|due on|next dose|next vaccination|booster due|due)\s*[:\-]?\s*", re.I
)


def _vaccination(sentence: str) -> dict[str, Any] | None:
    labelled = _VACCINE_LABEL_RE.match(sentence)
    if labelled is None and not _VACCINE_NAME_RE.search(sentence):
        return None
    rest = sentence[labelled.end() :] if labelled else sentence

    next_due = None
    due = _NEXT_DUE_RE.search(rest)
    if due:
        next_due = _parse_date(rest[due.end() : due.end() + 25])
    batch = _BATCH_RE.search(rest)
    given_text = rest[: due.start()] if due else rest
    date_given = _parse_date(given_text)

    # The name is everything before the first other field or date on the line.
    cut = len(rest)
    for regex in (_VACCINE_FIELD_RE, *_DATE_RES):
        m = regex.search(rest)
        if m and m.start() > 0:
            cut = min(cut, m.start())
    name = re.sub(r"\bvaccin\w*\b", "", rest[:cut], flags=re.I).strip(" .,:-–")
    has_details = date_given or next_due or batch or "vaccin" in sentence.lower()
    if not name or (labelled is None and not has_details):
        return None
    return {
        "vaccine": name,
        "date_given": date_given,
        "batch": batch.group("b") if batch else None,
        "next_due": next_due,
    }


def _vaccinations(lines: list[str]) -> list[dict[str, Any]]:
    found = []
    for line in lines:
        if _TITLE_RE.fullmatch(line.strip()):
            continue
        for sentence in _SENTENCE_SPLIT_RE.split(line):
            vaccination = _vaccination(sentence.strip())
            if vaccination is not None:
                found.append(vaccination)
    return found


# ── Assembly ───────────────────────────────────────────────────────────────


def _body_lines(lines: list[str]) -> list[str]:
    """Lines from the first heading/section on, without boilerplate. The letterhead above
    it lists the clinicians' specialties ("Veterinary Surgery & Radiology"), which would
    otherwise tag every record from that clinic."""
    start = 0
    for i, line in enumerate(lines):
        if _section_label(line) or _TITLE_RE.search(line) or is_panel_title(line) or is_header_line(line):
            start = i
            break
    return [line for line in lines[start:] if not _is_boilerplate(line)]


def build_record_card(
    text: str,
    table_sections: list[dict[str, Any]] | None = None,
    extraction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The form-shaped record. `table_sections` are lab tables already read from the PDF's
    geometry; without them, tables are parsed from `text`."""
    lines = _lines(text)
    if not table_sections:
        table_sections = sections_from_lines(lines)
    for section in table_sections:
        annotate_parameters(section["parameters"])

    body = _body_lines(lines)
    body_text = "\n".join(body)
    sections = _labelled_sections(lines)

    parameters = _table_parameters(table_sections)
    pair_lines: set[str] = set()
    for line in body:
        pair = _pair_parameter(line)
        if pair is not None:
            parameters.append(pair)
            pair_lines.add(line)

    symptoms, complaint_leftover = _symptoms(sections.get("complaint", []))

    notes, directives = [], []
    for line in sections.get("notes", []):
        for sentence in _SENTENCE_SPLIT_RE.split(line):
            (directives if _DIRECTIVE_RE.search(sentence) else notes).append(sentence.strip())

    medications = [m for m in (_medication(line) for line in sections.get("medications", [])) if m]

    clinical_notes = complaint_leftover + [
        line for line in sections.get("observations", []) if line not in pair_lines
    ] + notes
    diagnosis = " ".join(sections.get("diagnosis", [])).strip()
    results_notes = " ".join(sections.get("results_notes", [])).strip()

    vaccinations = _vaccinations(body)
    document_type = _classify(
        body_text,
        _title_lines(lines),
        sections,
        sum(len(s["parameters"]) for s in table_sections),
        len(vaccinations),
    )

    return {
        "document_type": document_type,
        "visit_date": _find_date(text),
        "document_title": _find_title(lines, table_sections),
        "clinic_provider": _find_provider(lines),
        "tags": _tags(body_text, parameters, document_type),
        "primary_diagnosis": diagnosis or None,
        "parameters": parameters,
        "results_notes": results_notes or None,
        "symptoms": symptoms,
        "medications": medications,
        "clinical_notes": "\n".join(clinical_notes) or None,
        "clinical_directives": directives,
        "vaccinations": vaccinations,
        "procedures": sections.get("procedures", []),
        "extraction": extraction or {"source": "text", "strategy": "flattened_text"},
        # Previous card shape, kept until the app reads the fields above.
        "type": document_type if document_type in ("lab_report", "vet_visit") else "unknown",
        "sections": table_sections,
    }


def build_record_from_pdf(pdf_bytes: bytes, fallback_text: str | None = None) -> dict[str, Any] | None:
    """Reads both the PDF's tables and its own text layer, so a PDF needs nothing from
    the app. Returns None only for a scanned PDF (no text layer, no tables) whose OCR
    text hasn't been submitted yet."""
    table_sections, strategy = extract_lab_sections_from_pdf(pdf_bytes)
    pdf_text = extract_pdf_text(pdf_bytes)
    text = pdf_text if pdf_text.strip() else (fallback_text or "")
    if not text.strip() and not table_sections:
        return None
    extraction = {
        "source": "pdf" if (pdf_text.strip() or table_sections) else "text",
        "strategy": strategy or ("pdf_text" if pdf_text.strip() else "flattened_text"),
    }
    return build_record_card(text, table_sections, extraction)


def build_record_from_text(text: str) -> dict[str, Any]:
    return build_record_card(text)
