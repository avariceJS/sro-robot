# Court-document quality and full-pipeline follow-up — 2026-10-16

This follow-up starts from published `f58ff93` and uses only generated
synthetic corpora for scored accuracy and HTTP/PostgreSQL workflow checks. The
two office samples were used for local smoke testing only; they have no
independently confirmed labels and do not contribute to accuracy rates.

## Local office samples and gold workflow

`samples/designation/` contained two raster images. Both were parsed locally;
processing took 4.14 s and 6.17 s. The parser did not establish the debtor role
in either image, so neither was eligible for automatic preparation. No field
values, OCR text, or source hashes are retained in this report.

The blank, mode-0600 human-label template is at the private local path
`/Users/root1/Documents/sro-office-quality/quality-gold.json`. It is outside
Git. Once a reviewer has checked each source image, record only visually
confirmed values, list their field names in `confirmed_fields`, set the
expected disposition, reviewer, and `independent_visual_review=true`, then run:

```sh
.venv/bin/python scripts/dev/quality/evaluate_office_corpus.py evaluate \
  --documents-dir samples/designation \
  --labels /Users/root1/Documents/sro-office-quality/quality-gold.json \
  --report /Users/root1/Documents/sro-office-quality/quality-report.json
```

Reports are aggregate-only on stdout. The private JSON report uses opaque IDs
and contains field counts, not values or filenames. No real-document quality
rate is available until a person completes those two labels. Two images are
also too small and too narrow a sample to characterize the office workload.

## Conditional INN recovery

When all primary OCR variants yield a debtor role but only a damaged,
checksum-invalid INN token, the parser performs one local grayscale PSM-6 pass
on the scan page containing the INN label. An absent INN label does not trigger
the retry. A recovered candidate is added as partial role-bound evidence with
a warning; it cannot satisfy the automatic-card gate. Optional-pass failures
retain the original OCR result and do not fail the task.

| Frozen synthetic set | INN exact before | INN exact after | Remaining missing | Auto decision before → after | Unsafe auto cards |
| --- | ---: | ---: | ---: | ---: | ---: |
| Dev, 24 | 22/24 | 24/24 | 0 | 4/9 → 4/9 | 0 |
| Heldout, 24 | 22/24 | 23/24 | 1 | 5/9 → 5/9 | 0 |

All other fields (document type, date, case number, debtor name) remained exact
24/24 on both sets. No nonempty field mismatch occurred. The manual-review
false negatives remained five on dev and four on heldout. After recovery they
were caused by OCR-partial debtor identifiers and mixed-document date warnings;
the gate correctly withheld these candidates. The sets are synthetic and share
a generator family; these rates are not real-office accuracy.

The known low-quality `synthetic-091.pdf` date remains unresolved. Across the
original four binarization/segmentation variants, every reported date agreed on
the wrong value; alternate header checks at gray/185/200/215 thresholds did
not produce the expected date (some produced no date). No safe
correction rule was supported, so the document remains review-only in the
50-document run. The reviewer page now explains field warnings and labels empty
fields as “not extracted from recognized text,” explicitly saying that this
does not establish absence in the original.

## Performance and complete bulk path

A three-run paired component profile of the base parser (before this optional
recovery pass) against archived `8c46c9a` source did not reproduce the earlier
single-pair +6.7% OCR/extraction change. Median scan
OCR-plus-extraction times were 1.995/1.971/1.977 s before and
2.019/1.995/1.990 s after for high/medium/low scan samples (changes of
approximately +1.2%/+1.2%/+0.7%). Text samples were 0.042 s versus 0.044 s,
only 0.002 s absolute. The prior +6.7% remains unexplained and is consistent
with single-run timing variation; no general OCR speedup is claimed.

The conditional retry added 0.592 s to the 24-file dev run and 0.614 s to the
24-file heldout run, restoring three of four previously missing INNs without
creating a false nonempty value. The 50-file HTTP benchmark contained one
damaged-INN retry opportunity and remained within one run's timing spread.

The full path was run twice on the same pinned 100-document synthetic
manifest: multipart upload → HTTP intake → fresh PostgreSQL → Redis RQ queue →
four OCR workers → field extraction → debtor gate → internal-card readback.
The manifest SHA-256 is
`59581d8a1b46f38441c3f58edb7ce8fdba7ea97843864fba5ec5191c8f191aeb`.
Every task reached `needs_review`; zero failed. Exact prepared-card field
readback matched for all cards.

| Files | Before retry (s) | After retry (s) | After throughput (docs/min) | Peak worker RSS after (MiB) | Prepared cards | Failed |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 5.100 | 5.246 | 114.4 | 1,214.8 | 5/5 exact | 0 |
| 20 | 7.636 | 7.859 | 152.7 | 1,375.3 | 10/10 exact | 0 |
| 50 | 18.055 | 18.682 | 160.6 | 1,441.9 | 25/25 exact | 0 |

The final 50-file run extracted 49/50 exact INNs, zero wrong nonempty INNs, and
49/50 exact dates (one wrong date stayed in review). All 25 text-derived cards
matched debtor name, INN, address, case, and date. All 25 scans remained in
review. Four workers remain the tested maximum; the roughly 1.44 GiB sampled
RSS and local workstation results do not qualify VPS capacity. The 3–4% runtime
differences between paired full-batch runs are too small and too few to claim
that the optional retry caused a general slowdown.

## UI and verification

The task review page now gives the reason automatic preparation was withheld,
shows warnings next to the affected field, and distinguishes an empty parse
from a value without parser warnings. It never displays heuristic confidence
as a calibrated percentage. Person-card tasks retain the withheld reason even
when the parser could not establish a debtor role. Empty fields explain that
not being extracted does not prove absence from the original. Synthetic HTTP
tests cover the explanation; staff can open a task from the batch dashboard
using “Проверить”.

Validation after the changes: `scripts/dev/verify.sh` passed 706 Python tests
(97 PostgreSQL-only tests were deferred in the unit phase and then all 97
passed on the disposable PostgreSQL drill), all 137 Node tests, compileall,
the diff check, migration/restore drills, database-role probes, and the
concurrency claim diagnostics. A regression pass also caught and fixed a
missing optional `scenario` attribute in a KAD-import test double; the final
gate was rerun after that compatibility correction.

## Remaining evidence needed

- Complete the two private labels and add a permissioned, representative office
  set with searchable PDFs, mixed pages, clean/weak scans and photos.
- Independently review a frozen heldout subset before treating any ratio as
  operational accuracy.
- Obtain a higher-resolution or independently scanned source for the known
  date case; current OCR evidence cannot disambiguate it safely.
- Keep date conflicts, partial debtor identity, missing required identifiers,
  and cross-role ambiguity on employee review.
