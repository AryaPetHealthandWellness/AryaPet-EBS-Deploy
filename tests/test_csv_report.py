from app.services.csv_report import is_spreadsheet_report, read_spreadsheet_report
from app.services.record_extractor import build_record_from_spreadsheet, build_record_from_text

# A chemistry analyzer's "UserReport_*_Abs.csv" export (real layout, pet and ids
# replaced). The flag column has no header; Absorbance/Reactivity are the machine's own.
_COMMA_CSV = """\
Madhuroha Pet Care
RESULTS\t
NAME:,Test Dog\t
Patient ID:,\t
AGE:,4\t
LAB.:,Biochemistry\t
MACHINE ID:,000000000\t
TEST TIME:,2026-08-14 12:04:38\t
------------------------------------------------------------------------
Assay,Result,Absorbance,Reactivity, ,Ref,Unit
ALB,2.4,1.5017813,1.4997904,L,3.2--4.3,g/dL
GLOB,2.1,,0,,2.0--4.0,g/dL
A/G,1.15,,0,,0.90--1.90,
ALT,> 650,1.3126713,0.1593443,H,17--95,U/L
UA,< 0.2,0.1129999,0.0151998,L,3.5--7.2,mg/dL
------------------------------------------------------------------------
ALT、CK>Detection limit, please retest after dilution.\t
------------------------------------------------------------------------
WAT:0,EMP:0,CHE:0
------------------------------------------------------------------------
""".encode()

# The same machine's tab-separated variant: values quoted, with a stray tab inside.
_TAB_CSV = (
    "Madhuroha Pet Care\t\t\t\t\t\t\n"
    '"RESULTS\t"\t\t\t\t\t\t\t\n'
    'NAME:\t"Test Dog\t"\t\t\t\t\t\t\n'
    'LAB.:\t"Biochemistry\t"\t\t\t\t\t\t\n'
    'TEST TIME:\t"2026-08-17 14:48:48\t"\t\t\t\t\t\t\n'
    "------------------------------------------------------------------------\t\t\t\t\t\t\n"
    "Assay\tResult\tAbsorbance\tReactivity\t \tRef\tUnit\n"
    "ALB\t2.4\t1.5234298\t1.5232173\tL\t3.2--4.3\tg/dL\n"
    "AMY\t2826\t2.1789906\t1.5151218\tH\t322--1310\tU/L\n"
    "eGFRcr\t--\t\t0\t\t\t\n"
    "IBIL\t0.4\t\t0\t\t0.0--17.0\tumol/L\n"
    "------------------------------------------------------------------------\t\t\t\t\t\t\n"
    '"-- Indicates that it cannot be calculated.\t"\t\t\t\t\t\t\t\n'
).encode()


def _by_name(card):
    return {p["name"]: p for p in card["parameters"]}


def test_comma_export_reads_every_row_by_its_columns():
    rows = _by_name(build_record_from_spreadsheet(_COMMA_CSV))
    assert list(rows) == ["ALB", "GLOB", "A/G", "ALT", "UA"]
    # The value, never the machine's absorbance/reactivity numbers.
    assert rows["ALB"]["value"] == "2.4"
    assert rows["ALT"]["value"] == "> 650"
    assert rows["UA"]["value"] == "< 0.2"
    # "3.2--4.3" stored with one dash, so every range reader understands it.
    assert rows["ALB"]["reference_range"] == "3.2-4.3"
    assert rows["ALB"]["unit"] == "g/dL"
    assert rows["A/G"]["unit"] == ""


def test_the_unheaded_flag_column_is_read_as_flags():
    rows = _by_name(build_record_from_spreadsheet(_COMMA_CSV))
    assert rows["ALB"]["flag"] == "L" and rows["ALB"]["abnormal"] is True
    assert rows["ALT"]["flag"] == "H" and rows["ALT"]["abnormal"] is True
    assert rows["GLOB"].get("flag") is None and rows["GLOB"]["abnormal"] is False


def test_header_fields_fill_the_form():
    card = build_record_from_spreadsheet(_COMMA_CSV)
    assert card["document_type"] == "lab_report"
    assert card["visit_date"] == "2026-08-14"
    assert card["clinic_provider"] == "Madhuroha Pet Care"
    assert card["document_title"] == "Biochemistry"
    assert card["tags"] == ["Bloodwork"]
    assert card["extraction"] == {"source": "csv", "strategy": "csv_columns"}


def test_a_note_under_the_table_is_kept_but_instrument_counters_are_not():
    card = build_record_from_spreadsheet(_COMMA_CSV)
    assert card["results_notes"] == "ALT、CK>Detection limit, please retest after dilution."
    assert "WAT" not in (card["clinical_notes"] or "")


def test_tab_separated_export_with_quoted_cells():
    card = build_record_from_spreadsheet(_TAB_CSV)
    rows = _by_name(card)
    # "--" (not calculated) is no reading, so eGFRcr is left out rather than given 0.
    assert list(rows) == ["ALB", "AMY", "IBIL"]
    assert rows["AMY"]["value"] == "2826" and rows["AMY"]["flag"] == "H"
    assert rows["IBIL"]["reference_range"] == "0.0-17.0"
    assert card["visit_date"] == "2026-08-17"
    assert card["document_title"] == "Biochemistry"
    # The legend for "--" is not a note about this pet.
    assert card["results_notes"] is None


def test_header_fields_become_label_value_lines():
    text, sections = read_spreadsheet_report(_TAB_CSV)
    assert "NAME: Test Dog" in text.split("\n")
    assert sections[0]["title"] == "Biochemistry"


def test_spreadsheet_files_are_recognised_by_extension():
    assert is_spreadsheet_report("user/abc-UserReport_134000448_Abs.csv")
    assert is_spreadsheet_report("report.TSV")
    assert not is_spreadsheet_report("report.pdf")
    assert not is_spreadsheet_report(None)


def test_a_photo_of_the_same_report_reads_the_same_rows():
    # What the app sends for a screenshot of this table: Vision text with the row
    # layout rebuilt, cells two spaces apart, the empty flag cell simply absent.
    photo_text = """\
Madhuroha Pet Care
NAME:  Test Dog
LAB.:  Biochemistry
TEST TIME:  2026-08-13 11:11:54
Assay  Result  Absorbance  Reactivity  Ref  Unit
ALB  3.1  1.8653935  1.8644206  L  3.2--4.3  g/dL
TP  6.5  0.5090235  0.6161443  6.4--8.5  g/dL
A/G  0.92  0  0.90--1.90
Crea  1.14  0.0670899  0.0731924  H  0.40--0.91  mg/dL
LDL  15  0  L  19--121  mg/dL
WAT:0  EMP:0  CHE:0
"""
    card = build_record_from_text(photo_text)
    rows = _by_name(card)
    assert list(rows) == ["ALB", "TP", "A/G", "Crea", "LDL"]
    assert rows["ALB"]["value"] == "3.1" and rows["ALB"]["flag"] == "L"
    assert rows["TP"]["reference_range"] == "6.4-8.5" and rows["TP"]["abnormal"] is False
    assert rows["Crea"]["flag"] == "H"
    assert rows["LDL"]["value"] == "15" and rows["LDL"]["flag"] == "L"
    assert card["visit_date"] == "2026-08-13"
