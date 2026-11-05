# Offline extraction comparison

This package runs an isolated comparison against private documents. It does not
connect to a working database, call SROArbitr, migrate data or run the office
service. Only a loopback llama.cpp endpoint is accepted for inference. Document
results, labels, renders and weights belong under ignored `.local-run/` storage.

The measured run is described in `docs/research/EXTRACTION_PILOT_2026-11-05.md`.
All quality claims use the explicitly scoped agent visual reference; they are
not human office acceptance. No employee review-time claim is made.

## Pinned runtime

Use the repository Python environment with its existing local OCR dependencies:

```sh
.venv/bin/python scripts/dev/extraction_pilot/setup_runtime.py --verify-only
```

Omit `--verify-only` only to install the exact public artifacts when absent.
The installer pins one Qwen3.5-4B Q4_K_M, its F16 projector, and the official
llama.cpp b12128 Mac arm64 archive by URL, byte count and SHA256. It never
launches a server or overwrites mismatching artifacts. It downloads no other
quantizations. Linux would need a separately qualified runtime build.

The measured Metal launch uses the binary inside
`.local-run/extraction-pilot-20261105/runtime/llama-b12128/`:

```sh
.local-run/extraction-pilot-20261105/runtime/llama-b12128/llama-server \
  -m .local-run/extraction-pilot-20261105/models/Qwen3.5-4B-Q4_K_M.gguf \
  --mmproj .local-run/extraction-pilot-20261105/models/mmproj-F16.gguf \
  --host 127.0.0.1 --port 18091 --offline --no-webui --jinja \
  --reasoning off -c 8192 -np 1 -t 4 -tb 4 -ngl all \
  --mmproj-offload --image-min-tokens 1024 --image-max-tokens 4096 -lv 3
```

CPU-only uses `-ngl 0 --device none --mmproj-device none --no-mmproj-offload
--no-kv-offload --no-op-offload`. Inspect actual language-model, recurrent/KV
state and CLIP backend placement in startup logs, not just requested flags. Do
not run two servers or send concurrent requests in the measured configuration.
Stop only the server process launched for this experiment, after checking its
PID and command.

The tested schema API uses `response_format.json_schema.schema`, with
`name` and `strict` in that wrapper. The README-shaped direct `schema` member
was silently ignored by this pinned build in the synthetic smoke. Server
`--reasoning off` disables thinking; the request also carries
`enable_thinking:false`. Neither JSON validity nor a valid checksum proves a
value's ownership.

## Matched resource control

The primary comparison used llama.cpp's default 8192 MiB prompt-cache limit and
idle-slot caching. A separate resource control disables both with
`--cache-ram 0 --no-cache-idle-slots`; it does not change the frozen prompt,
schema, image bounds, context, thread count, or output settings. Keep the
default batch sizes unchanged (`-b 2048`, `-ub 512` as recorded in the pinned
Metal startup log).

For one owner-authorized private C+D case, capture process memory and host swap
immediately before and after the single request with
`scripts/dev/extraction_pilot/resource_snapshot.py`. It reads `ps` RSS,
`vmmap -summary` physical footprint/current peak and `sysctl vm.swapusage`; it
does not connect to the server. Record request elapsed from the local harness
beside those snapshots. Keep all source images, prompts, labels and completion
payloads under ignored `.local-run/`.

Cache-zero Metal launch:

```sh
.local-run/extraction-pilot-20261105/runtime/llama-b12128/llama-server \
  -m .local-run/extraction-pilot-20261105/models/Qwen3.5-4B-Q4_K_M.gguf \
  --mmproj .local-run/extraction-pilot-20261105/models/mmproj-F16.gguf \
  --host 127.0.0.1 --port 18091 --offline --no-webui --jinja \
  --reasoning off -c 8192 -np 1 -t 4 -tb 4 -ngl all \
  --mmproj-offload --image-min-tokens 1024 --image-max-tokens 4096 \
  --cache-ram 0 --no-cache-idle-slots
```

After the Metal control completes, the matched CPU launch replaces the last
offload settings as follows:

```sh
.local-run/extraction-pilot-20261105/runtime/llama-b12128/llama-server \
  -m .local-run/extraction-pilot-20261105/models/Qwen3.5-4B-Q4_K_M.gguf \
  --mmproj .local-run/extraction-pilot-20261105/models/mmproj-F16.gguf \
  --host 127.0.0.1 --port 18091 --offline --no-webui --jinja \
  --reasoning off -c 8192 -np 1 -t 4 -tb 4 -ngl 0 --device none \
  --mmproj-device none --no-mmproj-offload --no-kv-offload --no-op-offload \
  --image-min-tokens 1024 --image-max-tokens 4096 \
  --cache-ram 0 --no-cache-idle-slots
```

## Private inputs and frozen reference

The private bundle retains `corpus/manifest-private.json`, `gold/labels-private.json`,
`gold/FROZEN.json`, blind `gold/independent-review.json` and separate errata.
The manifest maps source hashes, manual orientations, crops, page-image hashes,
coordinate transforms, act groups and tune/eval partitions. It is an experiment
map, not a general automatic page-segmentation service.

`corpus/build_corpus.py` is preserved and hashed in the private bundle. Do not
rerun it over the frozen manifest: it records new timings and would change the
freeze hash. Existing immutable pages can be replayed directly. Rebuilding is a
new run requiring a new manifest/freeze. If the split cannot be verified or a
seam crosses text outside the overlap, retain the full source and use route A
for review; do not silently discard the page edge.

A and B never load gold labels. C and D require the pre-model freeze and verify
its artifact hashes plus every source and page image hash. Gold identity is
used only by the evaluator to align predicted occurrences for measurement;
it is not a deployable primary-party selector. `reconcile.py` does not use gold
and keeps equal-name links explicitly unconfirmed.

## Reproduce the four routes

Use new output directories to preserve the original experiment. The following
commands replay the frozen private corpus; start the local server before C/D.
Substitute a new run output root consistently for all routes when replaying.

```sh
.venv/bin/python scripts/dev/extraction_pilot/harness.py --route A \
  --manifest .local-run/extraction-pilot-20261105/corpus/manifest-private.json \
  --output .local-run/extraction-pilot-20261105/replay/A
.venv/bin/python scripts/dev/extraction_pilot/harness.py --route B \
  --manifest .local-run/extraction-pilot-20261105/corpus/manifest-private.json \
  --output .local-run/extraction-pilot-20261105/replay/B
.venv/bin/python scripts/dev/extraction_pilot/harness.py --route C \
  --manifest .local-run/extraction-pilot-20261105/corpus/manifest-private.json \
  --prepared .local-run/extraction-pilot-20261105/replay/B \
  --output .local-run/extraction-pilot-20261105/replay/C \
  --config scripts/dev/extraction_pilot/run-config.json --split eval
.venv/bin/python scripts/dev/extraction_pilot/harness.py --route D \
  --manifest .local-run/extraction-pilot-20261105/corpus/manifest-private.json \
  --prepared .local-run/extraction-pilot-20261105/replay/B \
  --output .local-run/extraction-pilot-20261105/replay/D \
  --config scripts/dev/extraction_pilot/run-config.json --split eval
```

Run `--split tune` separately for controls; these are not independent evaluation.
All PDF inputs in this corpus have useful text, so C/D use direct PDF text.
An identical completed C PDF control can be reused in D only with the same
source hash, prompt, schema and settings. This is one control observation, not
a second independent result. D raster input contains images only; independently
obtained B OCR is used after inference to corroborate quotations and is charged
to D's cycle. D does not claim exact word coordinates from approximate vision.

B uses one prepared PSM3 pass per manually oriented page, with PSM6 retry only
when average word confidence is below 70; select retry text only above a 3-point
confidence improvement. It is an experimental routing rule, not accepted
replacement behavior. The reference is never used to choose OCR text.

C/D explicitly budget context, cap output and subdivide on context/output limits
up to depth two. Failed work and residual incompleteness remain in results.
The same-name occurrence merge is not treated as proof of identity; conflicting
values and address/type associations require review.

## Evaluation and inspection

```sh
.venv/bin/python scripts/dev/extraction_pilot/evaluate.py \
  --gold .local-run/extraction-pilot-20261105/gold/labels-private.json \
  --manifest .local-run/extraction-pilot-20261105/corpus/manifest-private.json \
  --results .local-run/extraction-pilot-20261105/results \
  --output .local-run/extraction-pilot-20261105/evaluation/final-private.json \
  --summary .local-run/extraction-pilot-20261105/evaluation/final-summary.json
.venv/bin/python -m unittest tests.test_extraction_pilot \
  tests.test_extraction_pilot_evaluate tests.test_extraction_pilot_reconcile \
  tests.test_extraction_pilot_rebind tests.test_extraction_pilot_type_guard \
  tests.test_extraction_pilot_transport
```

The evaluator retains main, displayed review and same-identity proposal-pool
results separately. New model discovery excludes values already available for
the same person in A. Exact name/annotated aliases align occurrences; no fuzzy
identity matching or gender-collapsing morphology is used. Address matching
ignores punctuation/spacing, but retains every alphanumeric component. Apartment
and postcode metrics are additional checks, not a substitute for full address.

Known source type missing from output counts as a miss. A genuinely unspecified
source type can remain unknown without being invented as actual. Conflicting
same-key values are alternatives, not last-value-wins. Syntactically valid raw
quotes, supported normalization, participant ownership and role truth are
separate measurements. The finite partial actor reference cannot qualify all
secondary roles or establish a zero wrong-owner rate.

Private per-source JSON contains personal data. Only aggregate summaries and
non-content hashes may be copied into tracked research documentation. Weights,
real identifiers, extracted quotes and private labels must never enter Git.

## Matched resource controls and offline postprocessing

The main quality run used the pinned runtime's default 8192 MiB RAM prompt cache.
The separate resource controls add `--cache-ram 0 --no-cache-idle-slots` to BOTH
Metal and CPU launches. They replay only tuning source `IMG_3584`, first C then D,
with new output directories (`--ids IMG_3584`), the same frozen config and B data.
Do not mix those timings/answers into the held-out quality table. Capture process
RSS, macOS physical footprint and whole-system swap before/after each request:

```sh
.venv/bin/python scripts/dev/extraction_pilot/resource_snapshot.py \
  --pid OWNED_SERVER_PID --label before --output PRIVATE_SNAPSHOT_PATH
```

The snapshot command is Mac-specific. A CPU-only Mac is not a VPS benchmark.
Installed unified RAM, current free pages, RSS, physical footprint and system swap
are distinct quantities. Do not attribute unrelated applications' swap to the LLM.

Completed saved model assertions can be rebound without inference, preserving
before/after hashes and an unchanged assertion digest:

```sh
.venv/bin/python scripts/dev/extraction_pilot/rebind.py \
  --results .local-run/extraction-pilot-20261105/results \
  --prepared .local-run/extraction-pilot-20261105/results/B-final \
  --manifest .local-run/extraction-pilot-20261105/postprocessing/rebind-manifest.json
.venv/bin/python scripts/dev/extraction_pilot/restore_inputs.py \
  --results .local-run/extraction-pilot-20261105/results \
  --prepared .local-run/extraction-pilot-20261105/results/B-final \
  --output .local-run/extraction-pilot-20261105/postprocessing/reconstructed-inputs
```

`reconcile.py --input PRIVATE_RESULT --output PRIVATE_SIDECAR` preserves terminal
retry occurrences with lineage, metadata conflicts, uncertain name links and
incomplete coverage without loading gold. `ui_supplement.py` replays the existing
explicit-copy INN suggestion separately; it does not exercise a browser click.
`type_guard.py` is an additional deterministic diagnostic only, not part of B or
an application change. `split_probe.py` isolates full-upright vs split PSM3 on the
already declared tuning spread; it must not be used to tune held-out answers.


Rebuild the expanded aggregate after `evaluate.py` (no inference):

```sh
.venv/bin/python scripts/dev/extraction_pilot/summarize.py \
  --metrics .local-run/extraction-pilot-20261105/evaluation/final-private.json \
  --manifest .local-run/extraction-pilot-20261105/corpus/manifest-private.json \
  --gold .local-run/extraction-pilot-20261105/gold/labels-private.json \
  --results .local-run/extraction-pilot-20261105/results \
  --output scripts/dev/extraction_pilot/results-summary.json \
  --rebind-seconds 0.270246 --reconcile-seconds 0.039113
```

The last two arguments record the separate measured offline repair/projection
phase, not a timing estimate for a future replay. Use its newly measured times
for a new run. The aggregate includes all-source controls and the 17-group
holdout separately; C/D PDF controls are the same reused text observation.

Optional diagnostics use new output paths to preserve completed evidence:

```sh
.venv/bin/python scripts/dev/extraction_pilot/ui_supplement.py \
  --results .local-run/extraction-pilot-20261105/results \
  --output .local-run/extraction-pilot-20261105/replay/ui-extra-private.json
.venv/bin/python scripts/dev/extraction_pilot/type_guard.py \
  --prepared .local-run/extraction-pilot-20261105/results/B-final \
  --output .local-run/extraction-pilot-20261105/replay/type-guard
```

`split_probe.py` reads the private hashed `split-probe/markers-private.json`.
Its marker booleans are diagnostic reference checks, never OCR routing criteria.
The preserved split-only result used one tuning spread; do not interpret it as
a general automatic split detector or an independent evaluation set.
