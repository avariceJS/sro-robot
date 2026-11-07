# Local extraction quality

Develop and measure locally on the Mac. Ubuntu CI checks functionality; Ubuntu
deployment/performance qualification is a separate final stage. No cloud OCR/LLM,
real database or external SRO operation is part of this workflow.

## Private case-separated dataset

`scripts/dev/quality/court_dataset.py` maintains an appendable private corpus
outside the checkout, with no final document-count limit. Each original is bounded
to 64 MiB and 60 pages. Import defaults to 100 additions per invocation; import
and capture accept `--limit` from 1 to 10,000 and retain a 240-second batch work
bound. Repeat bounded commands to advance the corpus. Existing Pillow/HEIF and
Poppler provide decoding and rendering. Source acquisition is a separate operation:
see [DATA_ACQUISITION.md](DATA_ACQUISITION.md).

```sh
DATASET="$HOME/Library/Application Support/SRO Robot/ocr-evaluation/current-generation"
.venv/bin/python scripts/dev/quality/court_dataset.py --private-root "$DATASET" \
  import --input-dir samples --source existing-local --medium unknown --limit 100
.venv/bin/python scripts/dev/quality/court_dataset.py --private-root "$DATASET" \
  import --input-dir /private/path/to/acquired/inbox \
  --source manual-public-import --medium text_pdf --limit 100
```

Import preserves byte-exact content-addressed originals, private page previews,
canonical page hashes and perceptual duplicate candidates. Existing local/tuned
documents are development-only. Before freezing a new corpus, locally review its
`manifest.json`: set a canonical `case_key` shared by **all acts of the same case**,
`case_reviewed`, `duplicate_reviewed`, any shared `duplicate_cluster`, confirmed
`medium`, and court/year/act-type strata. Establish canonical case identity from
validated official source identity and a matching case candidate in the PDF, or
local human review; ambiguous identities stay unreviewed. Filenames, an OCR guess,
hash agreement or an extension alone do not establish it. Review
reencoded/cropped/rephotographed derivatives too: perceptual hashes can miss them
and can conservatively merge unrelated layouts. Mark prior exposure even if a
different original/format of that case was used during development. Do not call a
screenshot or a rendered PDF a real camera photograph.

`freeze` joins case, original/page hashes, perceptual-page candidates and reviewed
derivative links before deterministic 80/20 assignment of eligible new cases.
Exact indexes and five disjoint dHash bands replace an all-document page-pair
scan; manual crop/rephoto/provenance links remain necessary. Previously exposed
cases are development-only. Unknown case or duplicate review puts a new group in
quarantine, which never qualifies as holdout. Frozen manifests reject edits/imports.
Do not inspect control predictions to choose a split.

Use `extend` for an expanded corpus or changed capture source/runtime. It copies
originals, previews and asset-bound human references into a new unfrozen generation;
captures and audits are recomputed. The flattened registry retains original/page
identities, case/duplicate aliases and previous assignments. Old development and
quarantine material become development; unexposed holdout remains holdout. A
correction to a canonical key cannot remove a historical case/derivative link.
New links between development and holdout, or silent control exposure, fail closed.
Explicitly expose a control in the new generation with `--expose-document-id`;
that transition makes its entire current and historical linked component development.
The previous generation stays unchanged.

```sh
PREVIOUS="$DATASET"
DATASET="$HOME/Library/Application Support/SRO Robot/ocr-evaluation/next-generation"
.venv/bin/python scripts/dev/quality/court_dataset.py --private-root "$DATASET" \
  extend --from-root "$PREVIOUS"
# When a previous control was inspected/tuned, create another new generation with:
# extend --from-root "$PREVIOUS" --expose-document-id LOCAL_CONTENT_SHA256
```

Extend into a fresh dataset directory before importing additional documents. A
continuously growing acquisition inbox is not itself an immutable evaluation split.

```sh
.venv/bin/python scripts/dev/quality/court_dataset.py --private-root "$DATASET" freeze
.venv/bin/python scripts/dev/quality/court_dataset.py --private-root "$DATASET" capture --limit 100
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

The 17 reference fields match implemented card outputs: `fio_raw`, `inn`, `snils`,
`address`, `address_kind`, `case_number`, `doc_date`, `court`, `entity_kind`,
`legal_name`, `short_name`, `legal_form`, `debtor_name`, `ogrn`, `ogrnip`, `kpp` and
`doc_type`. A full literal address includes its apartment/unit when present.
Participant ownership and source occurrence are audited separately for each field.
Old eight-field references remain partial: additional fields are unreviewed, never
assumed absent. Review-only birth-date hints are not invented main-card outputs.

Adopted references cannot be replaced with different values in that generation.
The subsequent audit exposes the frozen reference and exact observed fields for
human participant/source verdicts and error categories: OCR characters, field
detection, participant attribution, normalization, source location, ambiguous
source and other. Audit hashes bind the exact reference and capture. These are
human diagnoses, not automatically inferred error causes. Partially reviewed or
unreadable values are excluded and counted. Missing processing remains in positive
field denominators and never earns a correct-absence score. Unresolved ownership
withholds the combined value/owner/source ratio. Reports include per-field counts,
reference and detection coverage, exact/normalized value accuracy and owner review
coverage. `by_medium` keeps real photos, text PDFs, scans, synthetic distortions and
unknown media separate. New and inherited case/document counts remain distinct.
Repeated reference and audit batches skip already adopted records.

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
Payload logging is suppressed only during the capture CLI and its caller's exact
logging-disable level is restored on success or failure; other checks keep their
normal error/privacy diagnostics.

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
Previously tuned sample images remain development-only across generations. Fresh
PDF acquisition does not by itself create an independent case holdout, camera-photo
evidence or human gold. Human-reference coverage is currently zero: original-image
CER, field/owner accuracy and human error frequencies remain unscored (`null`).
No population accuracy or training benefit is established. Synthetic distortion
experiments cannot replace independent human references or real-photo controls.
Recognizer fine-tuning remains a later decision after measured programmatic limits
and verified training/validation/holdout labels; no model was selected or installed.

## PDF raster experiments with weak references

`ocr_research.py` deliberately renders PDF pages and runs real local Tesseract OCR
rather than measuring the PDF text-reader fast path. The fixed strata are clean,
tilt (+2.5 degrees), shadow, blur, perspective and lighting. These are rendered or
synthetically distorted pages; `real_photo_pages` is zero. Optional paired Sauvola
uses Tesseract's built-in thresholding setting beside the existing PSM 3 baseline;
it does not change the product recognizer or install a model.

```sh
RESEARCH="$HOME/Library/Application Support/SRO Robot/ocr-evaluation/raster-research"
INBOX="/private/path/to/acquired/inbox"
.venv/bin/python scripts/dev/quality/ocr_research.py --private-root "$RESEARCH" \
  --input-dir "$INBOX" --output-name paired-01 --limit-documents 200 \
  --pages-per-document 1 --dpi 200 --batch-seconds 240 --recognizers baseline,sauvola
# Repeat the identical command to resume its frozen input plan.
# For another batch use --output-name paired-02 --exclude-run paired-01.
```

The per-run document limit is 1–200, pages per document 1–60, resolution 72–300 DPI,
and batch work 1–240 seconds. These are operation budgets, not a final corpus cap.
Completed checkpoints survive interruption. A run freezes input hashes, transforms,
recognizer configuration and source/runtime fingerprints, even while the acquisition
inbox grows. Changed inputs/configuration require a new output name. Repeat
`--exclude-run` to exclude byte-identical inputs from multiple previous plans;
within-run raster deduplication counts byte-different PDFs with identical rendered
page hashes once. Case
identity is not established by this raster tool: its independent-case count remains
`null`, with case grouping and exposure qualification owned by `court_dataset.py`.

Embedded text must pass text/geometry and sampled visible-ink alignment checks,
but remains `embedded-text-weak`. Aggregate CER/WER and filtered digit/letter/line
edit rates are preliminary weak-reference comparisons, never human field truth or
observed error causes. Human-verified pages remain zero; missing/unusable references
are excluded with reasons. Keep each transform, recognizer and paired page outcome
separate, including regressions; do not select the best reading against a reference.
Private `metrics.json` and aggregate CLI output provide current counts and edit
numerators/denominators as batches grow. No moving corpus snapshot is a fixed result.

`ocr_illumination.py` replays a frozen research-only raster lighting gate against
already captured baseline/Sauvola pairs. Its private plan must identify source-case
controls disjoint from fitting and previously exposed cases, bind input hashes and
match the frozen algorithm/threshold. Prepare and review that plan locally before
running; a manifest of byte hashes alone does not establish case independence.

```sh
.venv/bin/python scripts/dev/quality/ocr_illumination.py --private-root "$RESEARCH" \
  --plan-name background-gate-control-plan.json
```

Replay verifies producer/runtime, original and raster identities and emits an
aggregate plan-bound replay file. It performs zero new OCR calls. Its synthetic,
weak-reference case control does not qualify real photographs, human field accuracy
or a production threshold change. The CLI supervisor retains the one-thread resource
limits, bounded runtime and private output policy used by the capture tools.

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
