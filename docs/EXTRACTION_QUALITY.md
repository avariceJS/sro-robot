# Local extraction quality

Develop and measure locally on the Mac. Ubuntu CI checks functionality; Ubuntu
deployment/performance qualification is a separate final stage. No cloud OCR/LLM,
real database or external SRO operation is part of this workflow.

## Private case-separated dataset

`scripts/dev/quality/court_dataset.py` prepares a bounded corpus (at most 200
originals, 64 MiB and 60 pages per original) outside the checkout. It uses existing
Pillow/HEIF and Poppler capabilities, not a new OCR framework. Source access is
separate: see [DATA_ACQUISITION.md](DATA_ACQUISITION.md). A public search result,
one successful download or an API subscription is not bulk-collection permission.

```sh
DATASET="$HOME/Library/Application Support/SRO Robot/ocr-evaluation/pilot-2026-11-07"
.venv/bin/python scripts/dev/quality/court_dataset.py --private-root "$DATASET" \
  import --input-dir samples --source existing-local --medium unknown
.venv/bin/python scripts/dev/quality/court_dataset.py --private-root "$DATASET" \
  import --input-dir storage/ocr-research-inbox --source manual-public-import --medium unknown
```

Import preserves byte-exact content-addressed originals, private page previews,
canonical page hashes and perceptual duplicate candidates. Existing local/tuned
documents are development-only. Before freezing a new corpus, locally review its
`manifest.json`: set a canonical `case_key` shared by **all acts of the same case**,
`case_reviewed`, `duplicate_reviewed`, any shared `duplicate_cluster`, confirmed
`medium`, and court/year/act-type strata. Case identity is a local human decision;
no filenames, OCR guesses, hash agreement or extensions establish it. Review
reencoded/cropped/rephotographed derivatives too: perceptual hashes can miss them
and can conservatively merge unrelated layouts. Mark prior exposure even if a
different original/format of that case was used during development. Do not call a
screenshot or a rendered PDF a real camera photograph.

`freeze` joins case/page/duplicate groups before deterministic 80/20 case assignment.
Any prior exposure makes the whole linked group development-only. Unknown case or
duplicate review puts a new group in quarantine. Quarantine is visible and never
qualifies as holdout. Subsequent manifest changes/imports are rejected; an expanded
or corrected corpus needs a new private generation, retaining the prior split as
evidence. Do not inspect control predictions to choose the split.

```sh
.venv/bin/python scripts/dev/quality/court_dataset.py --private-root "$DATASET" freeze
.venv/bin/python scripts/dev/quality/court_dataset.py --private-root "$DATASET" capture --limit 26
.venv/bin/python scripts/dev/quality/court_dataset.py --private-root "$DATASET" review --limit 5
```

Open `$DATASET/review.html` locally. It is an offline, network-disabled form with
blank reference fields and links to full originals; previews are downscaled.
Blind mode does not read predictions, OCR variants or PDF weak labels. Record
present/absent/unreadable/ambiguous/unreviewed separately, physical page and
participant role/occurrence. Use opaque participant labels within the document;
caption fields can use an act/caption occurrence. Values are literal source values,
with a separately reported NFC/whitespace-normalized comparison. The conservative
gold gate chosen for this workflow requires two distinct human reads. Unresolved
disputes cannot qualify. Optional independent transcripts use literal `\f` between
physical pages; blank page slots must remain. Never copy OCR or a previous card
into gold. Save the review JSON locally, then adopt it:

```sh
.venv/bin/python scripts/dev/quality/court_dataset.py --private-root "$DATASET" \
  adopt --input /private/path/to/references.json
.venv/bin/python scripts/dev/quality/court_dataset.py --private-root "$DATASET" review --audit --limit 5
.venv/bin/python scripts/dev/quality/court_dataset.py --private-root "$DATASET" \
  adopt --input /private/path/to/audits.json
.venv/bin/python scripts/dev/quality/court_dataset.py --private-root "$DATASET" score
```

Adopted references cannot be replaced with different values in that generation.
The subsequent audit exposes the frozen reference and exact observed fields for
human participant/source verdicts and error categories: OCR characters, field
detection, participant attribution, normalization, source location, ambiguous
source and other. Audit hashes bind the exact reference and capture. These are
human diagnoses, not automatically inferred error causes. Partially reviewed or
unreadable values are excluded and counted. Missing processing remains in positive
field denominators and never earns a correct-absence score. Unresolved ownership
withholds the combined value/owner/source ratio.

`dataset_capture.py` runs the actual local parser/OCR in an owned subprocess,
sequentially with one-thread limits, 50-second document budgets and a 240-second
batch work bound (resource probes and child cleanup add bounded allowances).
Completed work survives a later limit/failure. Text-only PDFs may complete zero
OCR calls; embedded text is always weak evidence until checked against the visible
original. No software/model/dependency download or service is started. Private
captures retain accepted parser identity, source revision/dirty digest, CLI/model/
reader fingerprints, first-reading physical page slots, observed fields, recovery
reasons and per-call text/TSV. The latter is private diagnostic evidence, not a
guessed call-to-page mapping. Do not edit source while capturing.

The first capture freezes a baseline cohort. Reuse requires the original hash and
matching source/runtime cohort; a changed recognizer/parser/runtime requires a new
generation, not overwriting a baseline. Reader origins are explicit: OCR CER is
computed only on human-transcribed OCR pages, embedded-text comparison separately,
unknown origins counted but not silently scored. NFC and collapsed whitespace
define reading-order CER; no case folding, punctuation repair or best-of-recovery
selection is applied. Per-page edit-distance work is bounded; transcribe shorter
documents if the explicit bound rejects a page. Processing speed is reported
separately from quality and unmeasured values are `null`, never 100%.

`metrics.json` stays private; CLI reports aggregates only. Zero/new/real-photo
holdout coverage is explicit, and the tool does not assert population accuracy.
The initial pilot contains 25 previously tuned images plus one manually selected
PDF: development and quarantined material, not a new independent 100–200-act set.
No real accuracy, observed error distribution or training benefit is established
until independent human references and new real-photo control cases exist.
Synthetic distortion experiments cannot replace that qualification. Recognizer
fine-tuning remains a later decision after measured programmatic limits and
verified training/validation/holdout labels; no model was selected or installed.

## References and error classes

`tests/fixtures/extraction_quality_cases.json` contains fifteen independently authored
synthetic text examples. Expected values come from their literal source text,
not an OCR output. They cover date formats, wrapped apartments, numbered prose,
incomplete addresses, participant ownership, organization names and invalid IDs.
Labeled-address examples also verify retained postcode/region/district components;
typed proper-name boundaries reject prose before administrative suffixes.
Birthplace descriptions are separate from residential addresses. Formatting
blanks do not reset that field type; a next typed field or semicolon does.
Repeated equal address values bind to the selected address label, not the first
occurrence elsewhere. Copyability names only fields actually present.
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
The child environment is restricted to basic process/model settings, the explicit
`SRO_TESSERACT_NATIVE` opt-out and fixed thread limits; unrelated service secrets
are not inherited. `SRO_TESSERACT_NATIVE=0` therefore remains effective in captures.

## Current evidence and limits

Against `2636c86`, the synthetic set improves from 15 correct / 9 wrong / 2 missing
field values to 25 / 0 / 0; correct withholdings increase from two to three. Three incorrect
partial-address flags become correct. These are synthetic value corrections,
not newly verified office fields. Existing ownership/checksum/review gates remain.

Before the later prefix/scope changes, two local sequential captures of all 25 real documents completed 47 OCR calls
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

The field-scope correction removes one residential-address suggestion taken from
an explicitly labeled birthplace in the frozen real corpus. FIO/identifiers and
birthplace evidence remain. A birthplace-only person may still have a bounded,
unconfirmed FIO hint; one such additional hint is not selected or promoted to
debtor authority. All 25 selected occurrences, main fields and other displayed
values remain unchanged. Equal-address provenance is checked on synthetic text.
Two affected real documents were freshly recognized (four calls); OCR/main field
metadata match the earlier capture. This is field-type validation over recognized
text, not adjudication of the original image. Unsupported/ambiguous data retain
review holds rather than acquiring a confident value.
