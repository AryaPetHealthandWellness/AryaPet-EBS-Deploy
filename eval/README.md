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

All 40 sample LFT/KFT reports in `tests/dummy_reports/`: every printed number is
extracted.

## Open lapses (next iterations)

Found by the lapse check and not fixed yet:

- **Results without a reference range are dropped.** "pH 6.5" and "Colour Pale yellow"
  on a urine report, and "Ova of Ancylostoma seen" on a stool report.
- **Medicine duration is dropped.** In "Amoxyclav 250mg BID x 5 days", the "x 5 days" is
  lost because the form has no duration field.
- **Letterhead specialities** ("Veterinary Surgery & Radiology") aren't kept. This is
  probably fine.
- **Header details have no field:** patient name, breed, age, the signing vet,
  registration number.
- **Photos are unmeasured.** Every case is a PDF or clean text. Photo OCR adds line
  order and character errors, and needs its own cases from real photos.

## The real constraint

Only **3 distinct real reports** are in `eval/private/`. A 100% score on 3 reports
proves very little. The next most valuable step is more real reports, ideally 20 or
more, covering:

- different labs and clinic software
- CBC, urine, stool, thyroid, imaging, prescriptions and vaccination cards
- phone photos of paper reports
