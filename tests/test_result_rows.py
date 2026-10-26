from app.services.result_rows import (
    header_roles,
    is_panel_title,
    outcome_pair,
    parse_result_line,
    row_from_cells,
    sections_from_lines,
)


def _parts(line: str) -> tuple:
    parts = parse_result_line(line)
    assert parts is not None, line
    return parts["name"], parts["value"], parts["unit"], parts["reference"], parts["flag"]


def test_reads_rows_in_any_column_order():
    assert _parts("Albumin 2.5 2.2 – 4.0 g/dl") == ("Albumin", "2.5", "g/dl", "2.2 – 4.0", None)
    assert _parts("Haemoglobin 9.8 g/dL 12.0 - 18.0") == ("Haemoglobin", "9.8", "g/dL", "12.0 - 18.0", None)
    assert _parts("Total WBC Count 18500 /cumm 6000 - 17000") == (
        "Total WBC Count", "18500", "/cumm", "6000 - 17000", None,
    )


def test_reads_one_sided_ranges_and_flags():
    assert _parts("TSH 0.9 ng/mL < 0.6 H") == ("TSH", "0.9", "ng/mL", "< 0.6", "H")
    assert _parts("T4 (Total) 0.6 ug/dL 1.0 - 4.0 L") == ("T4 (Total)", "0.6", "ug/dL", "1.0 - 4.0", "L")


def test_reads_qualitative_results():
    assert _parts("Protein Trace Negative") == ("Protein", "Trace", "", "Negative", None)
    assert _parts("Blood ++ Negative") == ("Blood", "++", "", "Negative", None)


def test_range_without_a_value_is_left_for_the_next_line():
    parts = parse_result_line("Creatinine 0.6 – 1.4 mg/dl")

    assert parts is not None
    assert parts["value"] is None
    assert parts["reference"] == "0.6 – 1.4"


def test_patient_details_and_prose_are_not_results():
    for line in (
        "Age 6.0 years Dummy patient ID TEST-27611",
        "Sample date 26.10.2026 Report date 26.10.2026",
        "Review after 5 days for follow up.",
        "Female, 2 Years",
        "Parameter Value Normal range",
    ):
        parts = parse_result_line(line)
        assert parts is None or parts["value"] is None or not parts["reference"], line


def test_named_test_outcomes():
    row = outcome_pair("Canine Parvovirus Antigen Test: Positive")

    assert row is not None
    assert (row["name"], row["value"]) == ("Canine Parvovirus Antigen Test", "Positive")
    assert outcome_pair("Registration Number: 1978") is None
    assert outcome_pair("Remarks: Negative") is None


def test_header_roles_include_unit_and_flag_columns():
    roles = header_roles(["Parameter", "Dummy result", "Example reference interval", "Unit", "Flag"])

    assert roles == {"name": 0, "value": 1, "range": 2, "unit": 3, "flag": 4}
    assert header_roles(["Albumin", "2.5", "2.2-4.0"]) is None


def test_separate_unit_column_is_used():
    roles = {"name": 0, "value": 1, "range": 2, "unit": 3}
    row = row_from_cells(["Albumin", "3.2", "2.2 - 4.0", "g/dL"], roles)

    assert row is not None
    assert (row["reference_range"], row["unit_text"], row["abnormal"]) == ("2.2 - 4.0", "g/dL", False)


def test_panel_titles_beyond_liver_and_kidney():
    titles = ("COMPLETE BLOOD COUNT", "Thyroid Profile", "URINE ROUTINE EXAMINATION", "Rapid Test Report")
    for title in titles:
        assert is_panel_title(title), title
    not_titles = (
        "Antiseptic dressing done today.",
        "PET ID: Daisy (Canine)",
        "Test Result Unit Reference Range",
    )
    for line in not_titles:
        assert not is_panel_title(line), line


def test_sections_split_by_title_and_trust_rows_under_a_header():
    lines = [
        "URINE ROUTINE EXAMINATION",
        "Parameter Result Reference",
        "Glucose Negative Negative",
        "THYROID PROFILE",
        "TSH 0.9 ng/mL < 0.6 H",
    ]
    sections = sections_from_lines(lines)

    assert [(s["title"], [p["name"] for p in s["parameters"]]) for s in sections] == [
        ("Urine Routine Examination", ["Glucose"]),
        ("Thyroid Profile", ["TSH"]),
    ]
