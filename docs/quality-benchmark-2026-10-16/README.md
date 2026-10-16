# Local synthetic document-quality benchmark — 2026-10-16

This benchmark contains only generated fictional court documents. No office
documents, external AI, registry records or SROArbitr writes were used. The
corpus generator and evaluator are under `scripts/dev/quality/`.
For PDFs with no text-layer page separators, Poppler `pdfinfo` supplies the page
count so an all-scan multipage file cannot be silently truncated to page one.

## Reproduce

Generate the two splits into separate local directories (outside the repository
is recommended), then evaluate each manifest independently:

```sh
python scripts/dev/quality/generate_quality_corpus.py --split dev --output-dir /tmp/sro-quality-dev
python scripts/dev/quality/generate_quality_corpus.py --split heldout --output-dir /tmp/sro-quality-heldout
APP_DEBUG=1 APP_SECRET_KEY='synthetic-verification-session-key-0000000000000000' \
  DATABASE_URL='postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/sro' \
  MIGRATION_DATABASE_URL='postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/sro' \
  REDIS_URL='redis://synthetic:synthetic@127.0.0.1:1/0' \
  python scripts/dev/quality/evaluate_quality_corpus.py --manifest /tmp/sro-quality-dev/dev-manifest.json
```

Run the evaluator again with the heldout manifest only after fixing and freezing
the candidate code. It reports aggregate field matches and mismatch filenames;
it never writes OCR text or identifier values. The checked-in manifests contain
synthetic gold labels and per-file SHA-256 values. Reproduction requires the
same Python, ReportLab, local Cyrillic font and PDF/OCR toolchain; generation
prints a new manifest digest to compare before evaluation.

## Corpus and result

Each split has 24 PDFs: four heading types (definition, decision, resolution,
application) crossed with six cases (clear text, clean scan, searchable cover
plus scanned act page, debtor with no INN, applicant-only evidence, and two
conflicting debtor identities). The heldout split uses separate fictional
names, identifier prefixes, case numbers and year. Gold fields and expected
disposition are declared by the generator, not inferred from OCR output.

| Split | Manifest SHA-256 | Exact fields (type/date/case/FIO) | INN exact / false nonempty / missing | Disposition exact | Incorrect automatic cards | OCR + parse time |
| --- | --- | --- | --- | --- | --- | ---: |
| Dev, 24 | `81b69875c9eca467d01ef79c588230156e8ae0730d50cd3ebd811b46a944fb8a` | 24/24 each | 22 / 0 / 2 | 19/24 | 0 | 6.02 s |
| Heldout, 24 | `e28ef56e6196dc33233c70b3e0beca9012ce44e5c2df1d6864f311a3a2d470c1` | 24/24 each | 22 / 0 / 2 | 20/24 | 0 | 5.99 s |

The expected disposition is `auto_prepare` only for explicit person-debtor acts
with a source INN, outside the application scenario. Scans are not forced to
review: they may prepare when all required evidence is exact and unambiguous.
In both splits, wrong-debtor evidence (applicant only), missing INN and
conflicting debtors remained manual work. Two scan INNs were not recovered per
split; there were no incorrect nonempty INNs in these 48 generated cases.

## Previously used 50-document cohort

The final bulk-stage cohort is a regression set, not an independent evaluation.
Its pinned corpus manifest is
`59581d8a1b46f38441c3f58edb7ce8fdba7ea97843864fba5ec5191c8f191aeb`.

- At `20541c6`, the stored task audit had 34/50 exact INNs, 16 blank and zero
  incorrect nonempty values. All 25 generated scan tasks needed employee review;
  25 internal cards were automatically prepared, all exact.
- With the damaged-OCR suggestion path and page-streaming OCR, the same 50 PDFs
  yield 49/50 exact INNs, zero incorrect nonempty values and one missing value.
  A valid candidate is preserved only when other variants show a damaged token;
  distinct valid INNs remain blank and ambiguous. The preparation gate admitted
  26 candidates (one more than the previous 25); all 26 matched name, INN,
  address, case and date. This was a local parse/gate audit, not a repeated
  database-backed 50-task workflow run.
- Name and case remain 50/50; date remains 49/50. `synthetic-091.pdf` still has
  a wrong unanimous OCR date at cutoff 215. Cutoffs 155 and 170 withhold the date
  and warn; 185 and 215 return the same wrong date without a warning; 200 also
  withholds it. Threshold changes were not adopted because this one sample does
  not establish a safe general improvement.
- A paired serial OCR+parse timing on these same 50 files was 46.46 s for the
  prior whole-document text-layer shortcut and 49.59 s for the final page-aware
  path (+6.7% in this single pair). These were field-extraction timings, not
  HTTP/DB batch timings. The prior end-to-end 50-task bulk benchmark remains
  17.96 s at four workers; no end-to-end speedup is claimed from this change.

## Confirmed mixed-PDF and long-scan behavior

A separate synthetic two-page PDF had a 665-character searchable cover and an
image-only act page with the actual debtor, case and INN. The former whole-PDF
shortcut returned only the cover; page-aware OCR extracted all three fields
from the scanned page. The fixture hash is
`9fb72e861d79f6c4f6647e85d59ebebc07fd77705ce84460800141eed92e9d54`; it remains
in local `/tmp/sro-bulk-ocr-audit-20261016/` and is reproducible from that
audit's synthetic generator.

A ten-page sparse synthetic scan (one text page, nine blank pages) took 2.79 s
and 504 MiB peak process RSS with bounded four-page raster batches; the previous
all-pages materialization plus OCR took 6.10 s and 921 MiB. This is one sparse
synthetic case, not a production workload profile. Photo uploads still use the
existing upscale/sharpen path; real phone-photo accuracy remains unqualified.

## Limits

The holdout is a deterministic synthetic set produced by the same local
generator family. It is useful for regression coverage across document type,
role and text/scan/mixed layout, but it does not establish office accuracy,
confidence calibration, handwriting tolerance or representative photo quality.
No permissioned real office corpus was available, so real-document performance
and error rates remain unknown. The wrong date in the previous cohort and the
scan INN misses show that page-awareness does not solve low-quality OCR by
itself.
