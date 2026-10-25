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
