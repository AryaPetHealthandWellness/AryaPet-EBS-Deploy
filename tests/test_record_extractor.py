from app.services.record_extractor import UI_TAGS, build_record_card

# Layout of a real Practo-generated clinic note (text layer as pdfplumber reads it), with
# the owner's name and phone numbers replaced.
_VISIT_TEXT = """\
Cornerstone Pet Clinic
Dr. D. Neelima Prabha Dr. Abhishek Kalundia
B.V.Sc. & A. H., M.V.Sc., Ph.D B.V.Sc. & A. H., M.V.Sc., Ph.D
Veterinary Surgery & Radiology Veterinary Clinical Medicine
cornerstonepetclinic@gmail.com www.cornerstonepetclinic.com
Daisy(Indian) C/o Owner Female, 2 Years
Patient Id: P0000
+910000000000
By: Dr. Alluri Upendhar Registration Number: 1978
Clinical Notes Date: 16 Dec, 2022
Complaint Reported that pet has redness noticed on Rt lateral abdomen & Left ventral thigh area noticed today.
Licking over the inner thigh area.
Active & alert.
No emesis or diarrhoea
Observations Bright, alert, and responsive, with normal vital signs.
Rectal Temp = 101 *F
Body Weight = 17.8 kg
Pustule noticed on Rt lateral abdomen area & erythema noticed on Left lateral thigh area.
Diagnosis Hematoma due to allergic reaction secondary to mosquito /insect bite suspected.
Notes Antiseptic dressing done today.
Review after 5 days for follow up.
Requested to call for an appointment before coming to prevent from crowding and longer waiting.
Next Visit on: _________________
Authorized Signature
(PLEASE CONTACT THE CONCERN VETERINARIAN AFTER THE MEDICATION COURSE IS OVER)
(* PLEASE NOTE THAT IN RARE EVENT OF UNFORESEEN CIRCUMSTANCES OR SPECIFIC TREATMENT OF OTHER
PETS AT THE CLINIC, THERE MAY BE A LONGER WAIT AT CHECK-IN)
Generated On: 16 Dec 2022 Page 1 of 1 Powered by Practo
"""

# A real lab report's text layer, where page 2 prints each value on the line *after*
# its reference range.
_LAB_TEXT = """\
PET ID: Daisy (Canine) GENDER: Female
PET AGE: 9Y PET BREED: Indian
SAMPLE RECEIVED DATE: 21.09.2025 DATE OF TEST RESULT: 21.09.2025
LIVER FUNCTION TEST
Parameter Value Normal range
Albumin 2.5 2.2 – 4.0 g/dl
ALP 1071.4 14-111 U/L
A/G Ratio 1.1 0.7-2.0
*****End of Report*****
Dr.B.Sushma
PET ID: Daisy (Canine) GENDER: Female
PET AGE: 9Y PET BREED: Indian
SAMPLE RECEIVED DATE: 21.09.2025 DATE OF TEST RESULT: 21.09.2025
KIDNEY FUNCTION TEST
Parameter Value Normal range
Creatinine 0.6 – 1.4 mg/dl
1.5
BUN/Creatinine ratio 4-27
22.4
*****End of Report*****
Dr.B.Sushma
Note -
• The results depend on the quality of the sample submitted to the laboratory.
"""


def test_card_has_exactly_the_form_fields():
    card = build_record_card(_VISIT_TEXT)

    assert list(card)[:12] == [
        "document_type",
        "visit_date",
        "document_title",
        "clinic_provider",
        "tags",
        "primary_diagnosis",
        "parameters",
        "results_notes",
        "symptoms",
        "medications",
        "clinical_notes",
        "clinical_directives",
    ]


def test_visit_note_header_fields():
    card = build_record_card(_VISIT_TEXT)

    assert card["document_type"] == "vet_visit"
    # Previously the first spaCy DATE entity won, which was the pet's age ("2 Years").
    assert card["visit_date"] == "2022-12-16"
    assert card["document_title"] == "Clinical Notes"
    assert card["clinic_provider"] == "Cornerstone Pet Clinic"


def test_visit_note_clinical_fields():
    card = build_record_card(_VISIT_TEXT)

    assert card["primary_diagnosis"] == (
        "Hematoma due to allergic reaction secondary to mosquito /insect bite suspected."
    )
    assert card["symptoms"] == [
        "Redness noticed on Rt lateral abdomen & Left ventral thigh area noticed today",
        "Licking over the inner thigh area",
    ]
    # Negated and "well" statements stay in notes, never as symptom chips.
    assert "No emesis or diarrhoea" in card["clinical_notes"]
    assert "Antiseptic dressing done today." in card["clinical_notes"]
    assert card["clinical_directives"] == [
        "Review after 5 days for follow up.",
        "Requested to call for an appointment before coming to prevent from crowding and longer waiting.",
    ]
    assert card["medications"] == []


def test_name_equals_value_lines_become_parameters_not_notes():
    card = build_record_card(_VISIT_TEXT)

    assert [(p["name"], p["value"], p["unit"]) for p in card["parameters"]] == [
        ("Rectal Temp", "101", "*F"),
        ("Body Weight", "17.8", "kg"),
    ]
    assert "Rectal Temp" not in card["clinical_notes"]


def test_identifier_lines_are_not_parameters():
    card = build_record_card("Registration Number: 1978\nPatient Id: 4848\nPET AGE: 9Y\nTemp: 102 F")

    assert [p["name"] for p in card["parameters"]] == ["Temp"]


def test_letterhead_specialty_and_footer_do_not_tag_the_record():
    # "Veterinary Surgery & Radiology" (letterhead) and "...TREATMENT OF OTHER PETS"
    # (footer) are not about this pet.
    assert build_record_card(_VISIT_TEXT)["tags"] == []


def test_lab_report_fields_and_every_table_row():
    card = build_record_card(_LAB_TEXT)

    assert card["document_type"] == "lab_report"
    assert card["visit_date"] == "2025-09-21"
    assert card["document_title"] == "Liver Function Test; Kidney Function Test"
    assert card["tags"] == ["Bloodwork"]
    rows = [
        (p["section"], p["name"], p["value"], p["unit"], p["reference_range"]) for p in card["parameters"]
    ]
    assert rows == [
        ("Liver Function Test", "Albumin", "2.5", "g/dl", "2.2 – 4.0"),
        ("Liver Function Test", "ALP", "1071.4", "U/L", "14-111"),
        ("Liver Function Test", "A/G Ratio", "1.1", "", "0.7-2.0"),
        # Value printed on the next line - these rows used to be dropped silently.
        ("Kidney Function Test", "Creatinine", "1.5", "mg/dl", "0.6 – 1.4"),
        ("Kidney Function Test", "BUN/Creatinine ratio", "22.4", "", "4-27"),
    ]


def test_unrecognisable_text_abstains_rather_than_guessing():
    card = build_record_card("Thanks for visiting, see you soon!")

    assert card["document_type"] is None
    assert card["tags"] == []
    assert card["parameters"] == []


def test_medication_frequency_is_never_defaulted():
    card = build_record_card(
        "Clinical Notes\nRx\nTab. Apoquel 5.4 mg BID\nCephalexin 250 mg\nOtomax drops twice a day"
    )

    assert card["medications"] == [
        {"drug_name": "Apoquel", "dosage": "5.4 mg", "frequency": "BID", "frequency_text": "BID"},
        {"drug_name": "Cephalexin", "dosage": "250 mg", "frequency": None, "frequency_text": None},
        {"drug_name": "Otomax drops", "dosage": None, "frequency": "BID", "frequency_text": "twice a day"},
    ]


def test_tags_only_use_the_apps_chips():
    card = build_record_card(
        "Clinical Notes\nNotes Dental scaling done. Annual check-up. Rabies vaccine given."
    )

    assert set(card["tags"]) <= set(UI_TAGS)
    assert card["tags"] == ["Dental", "Vaccines", "Routine"]


# ── Reports other than liver/kidney panels (synthetic text, no real patients) ──

_CBC_TEXT = """\
Paws Diagnostics Lab
Pet Name: Bruno  Species: Canine  Breed: Labrador
Sample Collected: 03/02/2025
COMPLETE BLOOD COUNT
Test Result Unit Reference Range
Haemoglobin 9.8 g/dL 12.0 - 18.0
Total WBC Count 18500 /cumm 6000 - 17000
Lymphocytes 12 % 12 - 30
Remarks: Leucocytosis with neutrophilia. Mild anaemia.
"""


def test_cbc_from_photo_text_reads_every_row_with_its_unit_and_flag():
    card = build_record_card(_CBC_TEXT)

    assert card["document_type"] == "lab_report"
    assert card["visit_date"] == "2025-02-03"
    assert card["document_title"] == "Complete Blood Count"
    assert card["tags"] == ["Bloodwork"]
    assert card["results_notes"] == "Leucocytosis with neutrophilia. Mild anaemia."
    rows = [
        (p["name"], p["value"], p["unit"], p["reference_range"], p["abnormal"]) for p in card["parameters"]
    ]
    assert rows == [
        ("Haemoglobin", "9.8", "g/dL", "12.0 - 18.0", True),
        ("Total WBC Count", "18500", "/cumm", "6000 - 17000", True),
        ("Lymphocytes", "12", "%", "12 - 30", False),
    ]


def test_qualitative_urinalysis_is_not_tagged_bloodwork_or_routine():
    card = build_record_card(
        "URINE ROUTINE EXAMINATION\nParameter Result Reference\n"
        "Specific Gravity 1.012 1.015 - 1.045\nProtein Trace Negative\nGlucose Negative Negative\n"
    )

    assert card["document_type"] == "lab_report"
    assert [(p["name"], p["value"], p["abnormal"]) for p in card["parameters"]] == [
        ("Specific Gravity", "1.012", True),
        ("Protein", "Trace", True),
        ("Glucose", "Negative", False),
    ]
    assert card["tags"] == []


def test_rapid_test_outcomes_become_parameters():
    card = build_record_card(
        "Rapid Test Report\nDate: 4 March 2025\nCanine Parvovirus Antigen Test: Positive\n"
        "Canine Distemper Antigen: Negative\nRemarks: Consistent with parvoviral enteritis.\n"
    )

    assert card["document_type"] == "lab_report"
    assert [(p["name"], p["value"]) for p in card["parameters"]] == [
        ("Canine Parvovirus Antigen Test", "Positive"),
        ("Canine Distemper Antigen", "Negative"),
    ]


def test_vaccination_record_lists_each_vaccine():
    card = build_record_card(
        "Vaccination Record\nDate: 15/06/2024\n"
        "Vaccine: Feline Tricat Trio  Batch No: A1234  Next Due: 15/06/2025\n"
        "Vaccine: Anti-Rabies (Nobivac Rabies)  Batch No: R998  Next Due: 15/06/2025\n"
    )

    assert card["document_type"] == "vaccination"
    assert card["vaccinations"] == [
        {"vaccine": "Feline Tricat Trio", "date_given": None, "batch": "A1234", "next_due": "2025-06-15"},
        {
            "vaccine": "Anti-Rabies (Nobivac Rabies)",
            "date_given": None,
            "batch": "R998",
            "next_due": "2025-06-15",
        },
    ]


def test_imaging_report_is_typed_by_its_own_title():
    card = build_record_card(
        "City Pet Imaging Centre\nRadiology Report\nDate: 22.08.2024\n"
        "Study: Thoracic radiographs, lateral and VD views\n"
        "Findings: Diffuse bronchial pattern noted.\nImpression: Chronic bronchitis.\n"
    )

    assert card["document_type"] == "other"
    assert card["primary_diagnosis"] == "Chronic bronchitis."
    assert "Diffuse bronchial pattern noted." in card["clinical_notes"]


def test_discharge_summary_separates_procedure_from_diagnosis():
    card = build_record_card(
        "Sunshine Animal Hospital\nDischarge Summary\nDiagnosis: Pyometra\n"
        "Procedure performed: Ovariohysterectomy under general anaesthesia\n"
        "Instructions: Keep the e-collar on. Suture removal after 10 days.\n"
    )

    assert card["document_type"] == "surgery"
    assert card["primary_diagnosis"] == "Pyometra"
    assert card["procedures"] == ["Ovariohysterectomy under general anaesthesia"]
    assert card["clinical_directives"] == ["Keep the e-collar on.", "Suture removal after 10 days."]


# A real histopathology report's text layer (DiagnoPet), names replaced. The letterhead
# is an image, so the text starts at the patient block; page 2 is the lab's conditions.
_BIOPSY_TEXT = """\
NAME : DOG DAISY AGE/GENDER: 9 LAB ID :
years/Female
BREED : Indie 001723225
DATE : Aug. 20, 2025
SPECIES : CANINE
REF BY : Dr. A Referrer, M.V.Sc, PhD
(DOG)
Test Description Value(s) Unit(s) Reference Range
HISTOPATHOLOGY, BIOPSY SOFT TISSUE
HISTORY : Swelling at right stifle joint.
LOCATION OF SAMPLE : Stifle joint.
PATHOLOGIST INTERPRETATION : Mast cell tumour, Low Grade
MICROSCOPIC DESCRIPTION :
Infiltrating, separating, and surrounding collagen bundles was an
unencapsulated, poorly circumscribed neoplasm of low cellularity.
mitotic figures average less than 3 per 10 high power field (mitotic index <2/hpf).
- End of Report -
Page 1 of 2
CONDITIONS OF REPORTING
It is presumed that the tests performed on the specimen belong to the patient; named or identified.
Test results are not valid for medico or vetro-legal purposes.
Page 2 of 2
"""


def test_biopsy_report_fills_the_form_from_its_labels():
    card = build_record_card(_BIOPSY_TEXT)
    assert card["document_type"] == "lab_report"
    assert card["visit_date"] == "2025-08-20"
    assert card["document_title"] == "Histopathology, Biopsy Soft Tissue"
    assert card["primary_diagnosis"] == "Mast cell tumour, Low Grade"
    assert card["symptoms"] == ["Swelling at right stifle joint"]
    assert card["parameters"] == []
    notes = card["clinical_notes"].split("\n")
    assert notes[0] == "Location of sample: Stifle joint."
    # Wrapped description lines are one paragraph, and the footer isn't part of it.
    assert notes[1].startswith("Microscopic description: Infiltrating, separating")
    assert notes[1].endswith("(mitotic index <2/hpf).")
    assert len(notes) == 2


def test_patient_fields_are_never_the_provider():
    # "LAB ID :" says "lab" but is a patient field; the letterhead here is an image.
    assert build_record_card(_BIOPSY_TEXT)["clinic_provider"] is None


# A generated PDF whose text layer runs bottom-up, so header fields follow "Diagnosis:".
_REVERSED_VISIT_TEXT = """\
Notes: All vitals normal. Next visit in 12 months.
Medications: Heartgard 30mg SID
Diagnosis: Annual wellness exam - healthy
Patient: Simba
Date: 15/06/2026
Clinic: Happy Paws Veterinary Centre
VETERINARY CLINIC REPORT
"""


def test_header_fields_end_the_section_before_them():
    card = build_record_card(_REVERSED_VISIT_TEXT)
    assert card["primary_diagnosis"] == "Annual wellness exam - healthy"
    assert card["clinic_provider"] == "Happy Paws Veterinary Centre"
    assert card["visit_date"] == "2026-06-15"
    assert card["document_title"] == "Veterinary Clinic Report"


def test_visit_with_medicines_is_a_visit_and_next_visit_is_a_directive():
    card = build_record_card(_REVERSED_VISIT_TEXT)
    assert card["document_type"] == "vet_visit"
    assert card["medications"][0]["drug_name"] == "Heartgard"
    assert card["clinical_notes"] == "All vitals normal."
    assert card["clinical_directives"] == ["Next visit in 12 months."]


def test_filled_in_next_visit_line_is_kept_and_a_blank_one_is_not():
    filled = build_record_card("Diagnosis: Otitis externa\nNext Visit on: 21.10.2025\n")
    assert filled["clinical_directives"] == ["Next Visit on: 21.10.2025"]
    blank = build_record_card("Diagnosis: Otitis externa\nNext Visit on: _________________\n")
    assert blank["clinical_directives"] == []
