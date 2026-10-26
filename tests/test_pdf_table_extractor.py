from app.services.pdf_table_extractor import _is_section_title, _rows_to_parameters

# Rows in the shape pdfplumber returns them: the header row, then one row per result.
_LFT_ROWS = [
    ["Parameter", "Value", "Normal range"],
    ["Albumin", "2.5", "2.2 – 4.0 g/dl"],
    ["ALP", "1071.4", "14-111 U/L"],
    ["A/G Ratio", "1.1", "0.7-2.0"],
]


def test_rows_to_parameters_reads_every_row():
    params = _rows_to_parameters(_LFT_ROWS)

    assert [p["name"] for p in params] == ["Albumin", "ALP", "A/G Ratio"]
    assert [p["value"] for p in params] == ["2.5", "1071.4", "1.1"]


def test_rows_to_parameters_flags_out_of_range_values():
    params = {p["name"]: p for p in _rows_to_parameters(_LFT_ROWS)}

    assert params["Albumin"]["abnormal"] is False
    assert params["ALP"]["abnormal"] is True  # 1071.4 against 14-111
    assert params["ALP"]["unit"] == "U/L"


def test_rows_to_parameters_handles_a_unitless_ratio():
    ratio = next(p for p in _rows_to_parameters(_LFT_ROWS) if p["name"] == "A/G Ratio")

    assert ratio["unit"] == ""
    assert ratio["normal_range"] == "0.7-2.0"
    assert ratio["abnormal"] is False


def test_rows_to_parameters_locates_columns_by_header_not_position():
    # Same data, columns in a different order and named differently - a lab that
    # formats its reports another way should still parse.
    reordered = [
        ["Bio. Ref Interval", "Test", "Result"],
        ["0.6 – 1.4 mg/dl", "Creatinine", "1.5"],
        ["12.8-53.5 mg/dl", "Blood Urea", "72.0"],
    ]
    params = _rows_to_parameters(reordered)

    assert [p["name"] for p in params] == ["Creatinine", "Blood Urea"]
    assert [p["value"] for p in params] == ["1.5", "72.0"]
    assert all(p["abnormal"] for p in params)


def test_rows_to_parameters_skips_non_result_rows():
    rows = [
        ["Parameter", "Value", "Normal range"],
        ["Albumin", "2.5", "2.2 – 4.0 g/dl"],
        ["", "", ""],
        ["Comments", "see overleaf", ""],  # non-numeric value
        ["", "9.9", "1-2"],  # no analyte name
    ]
    params = _rows_to_parameters(rows)

    assert [p["name"] for p in params] == ["Albumin"]


def test_rows_to_parameters_ignores_a_table_that_is_not_results():
    assert _rows_to_parameters([["Notes"], ["Sample haemolysed"]]) == []


def test_is_section_title_accepts_real_panel_headings():
    assert _is_section_title("LIVER FUNCTION TEST")
    assert _is_section_title("COMPLETE BLOOD COUNT")


def test_is_section_title_rejects_prose_and_metadata():
    assert not _is_section_title("WBC test result was 7.2 within panel.")
    assert not _is_section_title("SAMPLE RECEIVED DATE: 21.09.2025")
    assert not _is_section_title("Parameter Value Normal range")


# ── Real PDFs from tests/dummy_reports (synthetic data) ──

from pathlib import Path  # noqa: E402

from app.services.pdf_table_extractor import extract_lab_sections_from_pdf  # noqa: E402

_DUMMY_REPORTS = Path(__file__).parent / "dummy_reports"


def _sections(filename: str) -> list[dict]:
    sections, _ = extract_lab_sections_from_pdf((_DUMMY_REPORTS / filename).read_bytes())
    return sections


def test_pdf_with_separate_unit_column_reads_every_panel_under_its_own_title():
    sections = _sections("dummy_report_01_mochi_rottweiler.pdf")

    assert [(s["title"], len(s["parameters"])) for s in sections] == [
        ("Liver Function Test", 10),
        ("Kidney Function Test", 5),
    ]
    albumin = sections[0]["parameters"][0]
    assert (albumin["name"], albumin["value"], albumin["reference_range"], albumin["unit_text"]) == (
        "Albumin", "3.2", "2.2 - 4.0", "g/dL",
    )


def test_feline_pdf_reads_feline_ranges():
    sections = _sections("dummy_report_21_whiskers_scottish_fold.pdf")
    alt = next(p for s in sections for p in s["parameters"] if p["name"] == "ALT")

    assert alt["reference_range"] == "12 - 130"
