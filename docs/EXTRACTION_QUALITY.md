# Local extraction quality

Develop and measure locally on the Mac. Ubuntu CI checks functionality; Ubuntu
deployment/performance qualification is a separate final stage. No cloud OCR/LLM,
real database or external SRO operation is part of this workflow.

## References and error classes

`tests/fixtures/extraction_quality_cases.json` contains thirteen independently authored
synthetic text examples. Expected values come from their literal source text,
not an OCR output. They cover date formats, wrapped apartments, numbered prose,
incomplete addresses, participant ownership, organization names and invalid IDs.
Labeled-address examples also verify retained postcode/region/district components;
typed proper-name boundaries reject prose before administrative suffixes.
Field values, correct withholding and partial-address flags are counted separately.
These examples qualify parser behavior, not OCR accuracy on office documents.

Private real-document OCR remains **unverified** until original-image evidence
establishes exact values. Do not populate expected values from a previous card,
OCR agreement, checksum or confidence. The checker deliberately refuses to score
real records as references. Parser replay and fresh OCR capture are separate modes.

An address/unit present in source text but lost during extraction is a parser
defect. Missing/damaged source characters are an OCR problem; ambiguous ownership
is a separate review constraint. Preserve useful prefixes, provenance and review
holds rather than inventing a number or promoting an uncertain participant.

## Reproduce without OCR

```sh
.venv/bin/python scripts/dev/quality/extraction_check.py \
  --revision 2636c86 --output .local-run/extraction-quality/synthetic-before.json
.venv/bin/python scripts/dev/quality/extraction_check.py \
  --output .local-run/extraction-quality/synthetic-after.json \
  --compare .local-run/extraction-quality/synthetic-before.json
.venv/bin/python -m unittest tests.test_extraction_quality tests.test_core \
  tests.test_person_attributes tests.test_person_detection tests.test_entity_extraction
```

The revision option loads committed parser modules in that disposable process;
it never checks out another branch. Comparison requires identical input bytes and
checks values, source locations, warnings and full staff suggestions, including
ownership/copyability. Flat attribute records and metadata flags are included.
Private snapshots are written atomically under ignored `.local-run`; stdout
contains aggregate counts only. `--input` accepts a private JSON list of records
with `variants` (or `text`) and optional parser `kind`. No `expected` means unscored.

## Refresh private OCR inputs when needed

```sh
.venv/bin/python scripts/dev/quality/extraction_check.py --capture-samples \
  --output .local-run/extraction-quality/real-corpus.json
.venv/bin/python scripts/dev/quality/extraction_check.py \
  --input .local-run/extraction-quality/real-corpus.json \
  --output .local-run/extraction-quality/real-parser.json
```

Capture uses existing PNG/HEIC samples, fresh processes and byte-checked private
copies, one-thread limits, 50-second document budgets and a 180-second global work
bound (bounded resource checks/group reaping add allowances). Mac memory/swap/heat
admission uses the existing native-tool supervisor. Completed documents survive a
later stop; temporary image copies are removed. Every capture requires completed
Tesseract calls. Text is associated with the parser object actually returned by
`parse_file`, not its last rejected attempt. Final pipeline observations also
include raster review holds/boxes; a parser-only replay does not apply those OCR
postprocessing steps. Never use replay timings as OCR performance measurements.

## Current evidence and limits

Against `2636c86`, the synthetic set improves from 12 correct / 8 wrong / 0 missing
field values to 20 / 0 / 0; two correct withholdings remain. Three incorrect
partial-address flags become correct. These are synthetic value corrections,
not newly verified office fields. Existing ownership/checksum/review gates remain.

Two local sequential captures of all 25 real documents completed 47 OCR calls
each: 77.42 -> 77.66 seconds. All input hashes, OCR variants and complete main
field metadata match; no field/source/warning was lost. Three staff copyability
lists remove address entries absent from their field dictionaries; useful values
remain unchanged. This single control pair establishes no speed improvement.
Original-image accuracy remains unscored. No Mac result is a VPS measurement.

The subsequent address-prefix change retains source text previously omitted from
seven staff suggestions across five real documents. All additions are contiguous
postcode/country/typed administrative parts after an explicit address label; no
unlabeled/prose/birthplace prefix is promoted. The old address suffix and owner,
kind value, confidence, warnings and copyability remain. Main fields are unchanged
on all 25 frozen texts. This proves text retention, not original-image accuracy.
Five of the 25 displayed review forms now retain those prefixes; the selected
participant, every other displayed value and all warnings/statuses are unchanged.
Three affected documents were freshly recognized again (six completed calls),
with identical OCR variants and main field metadata; expanded word boxes were
checked on five address suggestions. That single control was 9.07 -> 9.40 s and
does not establish a stable speed effect. The tighter grammar leaves all 25 replay
results identical to that validated capture; no redundant OCR rerun was required.
