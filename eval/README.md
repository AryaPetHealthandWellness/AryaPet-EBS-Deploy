# Extraction accuracy

How we make report extraction exact, one measured change at a time.

## The loop

1. **Add the report.** Put a real PDF in `eval/private/` and add a case for it to
   `eval/private/real.json`. Type every expected value from the PDF itself, never from
   our output, so a wrong extraction can't become the answer key. Real reports carry
   real pet and owner details, so `eval/private/` is gitignored. Synthetic cases go in
   `eval/cases/` and are committed.
2. **Run it.**
   ```
   .venv/bin/python scripts/eval_extraction.py          # score + lapses, vs baseline
   .venv/bin/python scripts/eval_extraction.py -k cbc   # one case
   .venv/bin/python scripts/eval_extraction.py --lapses-only some.pdf   # no case needed
   ```
   - `✗` means a field differs from the expected value.
   - `⚠` means a lapse: a number or line printed in the report that went into no field.
   - `↓ REGRESSED` means something that used to pass now fails.
3. **Fix the cause** in `app/services/`, not the case.
4. **Re-run.** Nothing may regress. Then run `pytest` and `ruff check`.
5. **Record it.** Run `--save` to update `eval/baseline.json`, and add a line to the log
   below.

## Log

| Iteration | Change | Score |
|---|---|---|
| 0 | Baseline | 126/147 (85.7%) |
| 1 | Titles: any heading naming a document kind; letterhead skipped; abbreviations kept (CBC); generic headings get the diagnosis' gist ("Clinical Notes - Hematoma"); panels joined ("Liver & Kidney Function Tests") | 141/147 (95.9%) |
| 2 | Document type: imaging (ultrasonography, echo, radiograph) is `other`, stool tests are `lab_report`; the printed heading counts as a title signal | 145/147 (98.6%) |
| 3 | Procedures reported as done in the notes ("Antiseptic dressing done today."); provider read from a lab's own conditions ("Neither DiagnoPet, nor its ...") | 147/147 (100%) |
| 4 | **Photos.** A range's lower bound was read as the value when columns were two spaces apart (photo text): "ALT  14-111" gave ALT = 14. Decimal ranges such as "12.8-53.5" and "6.0-17.0" were read as dates, and their rows dropped. The app now turns tilted photos upright before rebuilding rows, using each line's angle from Vision (ported here as `layout_text_from_blocks`). Tilt cases added: 4 real pages at -4°, 0°, 3°, 6° and 10°. | 557/557 (100%) |

All 40 sample LFT/KFT reports in `tests/dummy_reports/`: every printed number is
extracted.

## Open lapses (next iterations)

From five published IDEXX sample reports (`eval/private/net/`), not yet scored
cases:

- **Two-column pages are read across both columns.** IDEXX prints haematology
  beside chemistry. Rows merge ("RBC 7.6 ... L Chloride 99"), values go missing
  (Chloride, Total Protein, Albumin, Creatinine, Phosphorus), and the patient's
  name becomes a panel title. This is the biggest gap: the page has to be split
  into its columns before rows are read.
- **Flags printed before the name** ("H ALP 181", "L Total T4") stay in the name.
- **Ranges in brackets** ("(5.4 - 8.7) x10E12/L") aren't read as ranges.
- **The provider takes the whole letterhead line,** phone numbers and branches
  included ("IDEXX Laboratories Ltd IDEXX Wetherby IDEXX London 44 (0) 203 ...").
- **Urinalysis loses half its rows.** Clarity CLOUDY, WBC 2-5 HPF, Bacteria NONE
  SEEN and page-2 rows are dropped, and "Colour" is taken as a reference range.

Earlier:

- **Results without a reference range are dropped.** "pH 6.5" and "Colour Pale yellow"
  on a urine report, and "Ova of Ancylostoma seen" on a stool report.
- **Medicine duration is dropped.** In "Amoxyclav 250mg BID x 5 days", the "x 5 days" is
  lost because the form has no duration field.
- **Letterhead specialities** ("Veterinary Surgery & Radiology") aren't kept. This is
  probably fine.
- **Header details have no field:** patient name, breed, age, the signing vet,
  registration number.
- **Vision OCR itself is unmeasured.** `eval/tools/vision_ocr` runs the app's exact
  Vision request, but Vision returns nothing in a macOS VM or the iOS simulator. The
  PHOTO cases (from `eval/tools/make_photos.py`) are skipped there. Run them on a
  physical Mac, or log `[OCR raw]` from a device. Tilt is covered by the
  simulation; character errors are not.

## The real constraint

Only **3 distinct real reports** are in `eval/private/`. A 100% score on 3 reports
proves very little. The next most valuable step is more real reports, ideally 20 or
more, covering:

- different labs and clinic software
- CBC, urine, stool, thyroid, imaging, prescriptions and vaccination cards
- phone photos of paper reports
