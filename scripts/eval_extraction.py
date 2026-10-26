"""Scores record extraction against hand-checked expectations, and lists lapses.

    .venv/bin/python scripts/eval_extraction.py                 # every case
    .venv/bin/python scripts/eval_extraction.py -k biopsy       # cases matching "biopsy"
    .venv/bin/python scripts/eval_extraction.py --save          # record as the new baseline
    .venv/bin/python scripts/eval_extraction.py --lapses-only a.pdf b.pdf   # no expectations needed

Two checks per document:

  score   each field listed in the case's "expect" block is compared exactly (after
          whitespace/case normalisation for text). Parameters are matched by name and
          then compared value / unit / reference range / abnormal flag, so a single
          wrong digit shows up as its own miss.

  lapses  needs no expectations. Every number printed in the document must appear
          somewhere in the output, and every content line must be mostly reflected in
          some field. Anything left over is printed - that is what the extractor
          dropped. Header lines (patient, breed, signature, contact) are listed apart:
          the form has no field for them yet.

Cases live in eval/cases/*.json (synthetic, committed) and eval/private/*.json (real
reports with real pet and owner details - gitignored, never committed). A case is:

  {"name": "...", "source": {"pdf": "eval/private/x.pdf"} | {"text": "..."},
   "expect": {"document_title": "...", "parameters": [{"name": "ALP", "value": "1071.4",
              "unit": "U/L", "reference_range": "14-111", "abnormal": true}], ...}}

--save writes eval/baseline.json; later runs print the change per field against it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.pdf_table_extractor import extract_pdf_text  # noqa: E402
from app.services.record_extractor import (  # noqa: E402
    _HEADER_FIELD_RE,
    _is_boilerplate,
    build_record_from_pdf,
    build_record_from_text,
)

CASE_DIRS = [ROOT / "eval" / "cases", ROOT / "eval" / "private"]
BASELINE = ROOT / "eval" / "baseline.json"

# ── Running a case ──────────────────────────────────────────────────────────


def run_source(source: dict[str, str]) -> tuple[dict[str, Any], str]:
    if "pdf" in source:
        data = (ROOT / source["pdf"]).read_bytes()
        return build_record_from_pdf(data) or {}, extract_pdf_text(data)
    return build_record_from_text(source["text"]), source["text"]


# ── Scoring ─────────────────────────────────────────────────────────────────


def _norm(v: Any, keep_case: bool = False) -> Any:
    if isinstance(v, str):
        v = re.sub(r"\s+", " ", v.replace("–", "-").replace("—", "-")).strip()
        v = re.sub(r"\s*-\s*", "-", v)
        return (v if keep_case else v.lower()) or None
    if isinstance(v, list):
        return [_norm(x, keep_case) for x in v]
    return v


# Shown to the user as printed, so "Cbc" for "CBC" is a miss.
_CASE_SENSITIVE = {"document_title"}


_PARAM_KEYS = ("value", "unit", "reference_range", "abnormal")


def score(expect: dict[str, Any], got: dict[str, Any]) -> list[tuple[str, bool, str]]:
    """(check, passed, detail) for every expected field / parameter attribute."""
    out: list[tuple[str, bool, str]] = []
    for field, want in expect.items():
        if field == "parameters":
            out += _score_parameters(want, got.get("parameters") or [])
            continue
        have = got.get(field)
        keep = field in _CASE_SENSITIVE
        ok = _norm(want, keep) == _norm(have, keep)
        out.append((field, ok, "" if ok else f"want {want!r}\n{'':14}got  {have!r}"))
    return out


def _score_parameters(want: list[dict], have: list[dict]) -> list[tuple[str, bool, str]]:
    out = []
    by_name = {}
    for p in have:
        by_name.setdefault(_norm(p.get("name")), p)
    wanted_names = set()
    for w in want:
        key = _norm(w["name"])
        wanted_names.add(key)
        h = by_name.get(key)
        if h is None:
            out.append((f"param {w['name']}", False, "missing"))
            continue
        for k in _PARAM_KEYS:
            if k not in w:
                continue
            hv = h.get(k)
            if k == "unit" and not hv:
                hv = None
            ok = _norm(w[k]) == _norm(hv)
            out.append((f"param {w['name']}.{k}", ok, "" if ok else f"want {w[k]!r} got {hv!r}"))
    for name, p in by_name.items():
        if name not in wanted_names:
            out.append((f"param {p.get('name')}", False, "extra (not in the report)"))
    return out


# ── Lapses ──────────────────────────────────────────────────────────────────

_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w])")
_WORD_RE = re.compile(r"[a-z]{4,}")
_HEADERISH_RE = re.compile(
    r"@|www\.|\+?\d[\d\s/-]{8,}|^dr\.?\s|\bdr\.|registration|page \d+ of|generated on|signature|"
    r"\b(?:name|breed|species|age|gender|sex|patient|pet id|lab id|ref by|c/o)\b|"
    r"\b(?:b|m)\.v\.sc\b|ph\.?d\b|years/",
    re.IGNORECASE,
)
# Table column headings ("Parameter Value Normal range") carry no data.
_COLUMN_HEADING_RE = re.compile(
    r"^(?:test(?: description)?|parameters?|investigation)\b.*\b(?:value|result)s?\b", re.IGNORECASE
)


def _flatten(v: Any) -> str:
    if isinstance(v, dict):
        return " ".join(_flatten(x) for k, x in v.items() if k not in ("sections", "extraction"))
    if isinstance(v, list):
        return " ".join(_flatten(x) for x in v)
    return "" if v is None else str(v)


_DATE_OR_PAGE_RE = re.compile(
    r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b|\bpage \d+(?: of \d+)?", re.IGNORECASE
)
_NOTICE_START_RE = re.compile(r"^(?:note\b|conditions of reporting|\(cid:\d+\)|•)", re.IGNORECASE)


def lapses(text: str, got: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """(numbers not in the output, content lines dropped, header lines with no field)."""
    out_text = _flatten(got).lower()
    out_numbers = {n.replace(",", ".") for n in _NUMBER_RE.findall(out_text)}
    # Dates come out as ISO; count their parts as captured.
    for iso in re.findall(r"\d{4}-\d{2}-\d{2}", out_text):
        y, m, d = iso.split("-")
        out_numbers |= {y, m, d, str(int(m)), str(int(d)), y[2:]}

    missed_numbers, dropped, header = [], [], []
    seen = set()
    in_notice = False
    for line in (raw.strip() for raw in text.split("\n")):
        if not line or line in seen:
            continue
        seen.add(line)
        # Printed disclaimers run over several lines; their wrapped tails ("legal
        # purposes", "Referring Physician.") start lower-case or are a bullet's rest.
        if _is_boilerplate(line) or _NOTICE_START_RE.match(line):
            in_notice = True
            continue
        if in_notice and (line[0].islower() or len(line.split()) <= 3 or line.endswith(".")):
            continue
        in_notice = False
        if re.fullmatch(r"[-_*•.\s]+|- end of report -", line, re.I) or _COLUMN_HEADING_RE.match(line):
            continue
        is_header = bool(_HEADER_FIELD_RE.match(line) or _HEADERISH_RE.search(line))
        # Dates are checked as dates (visit_date), page numbers aren't data.
        numeric = _DATE_OR_PAGE_RE.sub(" ", line)
        nums = [n.replace(",", ".") for n in _NUMBER_RE.findall(numeric)]
        lost = [n for n in nums if n not in out_numbers]
        words = _WORD_RE.findall(line.lower())
        covered = sum(w in out_text for w in words) / len(words) if words else 1.0
        if is_header:
            if covered < 0.5 or lost:
                header.append(line)
            continue
        if lost:
            missed_numbers.append(f"{', '.join(lost):14} in: {line}")
        elif covered < 0.5:
            dropped.append(line)
    return missed_numbers, dropped, header


# ── Report ──────────────────────────────────────────────────────────────────


def load_cases(pattern: str | None) -> list[dict[str, Any]]:
    cases = []
    for d in CASE_DIRS:
        for f in sorted(d.glob("*.json")):
            for case in json.loads(f.read_text()):
                if not pattern or pattern.lower() in case["name"].lower():
                    cases.append(case)
    return cases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", help="only cases whose name contains this")
    ap.add_argument("--save", action="store_true", help="save as the new baseline")
    ap.add_argument("--quiet", action="store_true", help="summary only")
    ap.add_argument("--lapses-only", nargs="*", metavar="PDF", help="lapse report for these PDFs")
    args = ap.parse_args()

    if args.lapses_only is not None:
        for path in args.lapses_only:
            data = Path(path).read_bytes()
            got, text = build_record_from_pdf(data) or {}, extract_pdf_text(data)
            _print_lapses(Path(path).name, *lapses(text, got), got)
        return 0

    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    results: dict[str, dict[str, bool]] = {}
    totals = [0, 0]
    for case in load_cases(args.k):
        got, text = run_source(case["source"])
        checks = score(case.get("expect", {}), got)
        results[case["name"]] = {c: ok for c, ok, _ in checks}
        passed = sum(ok for _, ok, _ in checks)
        totals[0] += passed
        totals[1] += len(checks)
        if args.quiet:
            continue
        mark = "PASS" if passed == len(checks) else "FAIL"
        print(f"\n{mark}  {case['name']}  ({passed}/{len(checks)})")
        for check, ok, detail in checks:
            if not ok:
                print(f"   ✗ {check:30} {detail}")
            prev = baseline.get(case["name"], {}).get(check)
            if prev is not None and prev != ok:
                print(f"   {'↑ fixed' if ok else '↓ REGRESSED'}: {check}")
        if case.get("lapses", True):
            numbers, dropped, header = lapses(text, got)
            if numbers or dropped:
                _print_lapses(None, numbers, dropped, [], got)

    pct = 100 * totals[0] / totals[1] if totals[1] else 0
    old = [ok for case in baseline.values() for ok in case.values()]
    trend = f"   (baseline {100 * sum(old) / len(old):.1f}%)" if old else ""
    print(f"\n{'=' * 70}\nOVERALL {totals[0]}/{totals[1]} checks = {pct:.1f}%{trend}")
    for name, checks in results.items():
        fails = [c for c, ok in checks.items() if not ok]
        print(f"  {'✓' if not fails else '✗'} {name:45} {len(checks) - len(fails)}/{len(checks)}")

    if args.save:
        BASELINE.write_text(json.dumps(results, indent=1, sort_keys=True) + "\n")
        print(f"\nsaved baseline -> {BASELINE.relative_to(ROOT)}")
    return 0


def _print_lapses(name, numbers, dropped, header, got) -> None:
    if name:
        print(f"\n### {name}   title={got.get('document_title')!r} type={got.get('document_type')}")
    for n in numbers:
        print(f"   ⚠ number not extracted: {n}")
    for line in dropped:
        print(f"   ⚠ line not extracted:   {line}")
    for line in header:
        print(f"   · header (no field):    {line}")


if __name__ == "__main__":
    sys.exit(main())
