# SRO Office local extraction pilot

**Decision:** retain current route A as the office default. B is much faster but loses existing useful proposals. Qwen C adds 17 new personal facts beyond A but introduces wrong ownership/roles and qualifies too few sources for automatic main-card use. D does not justify replacing complete raster OCR in this configuration. First fix deterministic reading/review and document-occurrence/source reconciliation; do not make 9B the next step.

Status: local A/B/C/D inference and matched Metal/CPU controls completed; application behavior and production are unchanged.

## Decision scope

This pilot compares a frozen current parser, economical page OCR with the same parser, OCR plus one local Qwen3.5-4B, and direct image reading by that same model. It does not change the office application, production, working database, SROArbitr, or server infrastructure. No employee participated in a timed review session. A model result is a review proposal, not an accepted external record or a production-ready card.

The baseline code is `c27bde0e1e6ccc3822c9c8dd412ca4136c1ac5db` on `develop`; the application/core source is unchanged from that checkpoint. The earlier research is [EXTRACTION_RESEARCH_2026-11-05.md](EXTRACTION_RESEARCH_2026-11-05.md). Reproduction commands are in [the pilot README](../../scripts/dev/extraction_pilot/README.md).

## Corpus and reference

There are 21 original HEIC images and one locally stored seven-page PDF. The photographs contain 39 principal printed page faces; with the PDF this gives 46 logical pages and 22 source files. These are 20 principal act groups, not 22 independent documents. IMG_3584/IMG_3603 are the same photographed page; IMG_3583 and the PDF are the same act. Three complete act groups were assigned to tuning, leaving 17 other photographed-act groups for fixed evaluation. Background sheet fragments are not treated as additional documents and their membership is not exhaustively resolved.

The private manifest records source and page-image SHA256s, manual rotation, crops, logical-page IDs and affine transforms back to the original Pillow-decoded photograph. A spread has 80 pixels of margin on each side of the center seam, giving 160 shared pixels. The original is retained. This is a bounded experiment with an inspected corpus map, not a general automatic page detector. Uncertain production splits must retain the full original and fall back to unsplit review.

Primary reference fields were transcribed from original images before Qwen answers were examined. They cover name, INN, SNILS, OGRN where visible, full address, explicit/unknown address type, document date, case number and court. Secondary participants and temporal roles are only partially annotated. Another agent independently checked the first page of all 22 sources against the originals. The reference is **agent visually checked, not human verified**. It is not office acceptance or a statistically representative accuracy estimate.

Frozen labels were never overwritten. A separate blind-review erratum corrects one transcribed INN, synchronizes printed/normalized slots, corrects three printed-name slots that had been stored in nominative instead of the visible oblique form, and excludes one uncertain surname normalization from name scoring. These changes and hashes are preserved. Null labels mean not established in the inspected scope, not proven absence. Raw-name exactness is not scored against the known imperfect printed-name slots.

The seven-page PDF is a control for clean text, not independent data from its photograph. C and D both use direct PDF text. Identical PDF inference can be reused between these two routes only after verifying matching source, prompt, schema and settings. It remains one control observation.

## Routes and fixed settings

| Route | Reader and extraction | Employee evidence included |
| --- | --- | --- |
| A | Current `parse_file`, including orientation, five photo reading modes, any native optional recovery, validation and source binding | Primary parser fields, existing `review_fields`, all same-identity review suggestions; explicit-copy alternate INN projection replayed separately |
| B | Inspected page crops; one prepared PSM3 per page; PSM6 only below mean word confidence 70, selected only above a 3-point gain; unchanged parser | Same core and review projection as A |
| C | B text with line IDs sent to Qwen; B coordinates used afterward for quote localization | Structured participants and fields, raw/normalized values, role/owner quotes, checks, candidates and source verification |
| D | The same prepared page images, color JPEG capped at 1800 pixels on the long side, read by Qwen | Page-level vision citations, checked against B OCR after inference; retry-crop quotes remain unverified at page level; full B reader cost is charged, and approximate vision is never claimed as exact word geometry |

B source text is never chosen using gold or model answers. PDFs retain their text layer in every route. Corpus preparation, decoding and crop writing are measured separately. Annotation-only PDF previews are distinguished from direct-text processing.

One Qwen3.5-4B Q4_K_M and its matching F16 visual projector were downloaded. No other model or quantization was fetched. Model snapshot: `e87f176479d0855a907a41277aca2f8ee7a09523` from the converter repository. Model size is 2,740,937,888 bytes; projector size is 672,423,616 bytes. Both SHA256s match Hub metadata. These are file sizes, not memory requirements.

Runtime: official llama.cpp b12128 Mac arm64 archive, commit `a01dff908b9c9311d7af316bba3c918063749bd0`, version 0.9.0-dev. One active request, context 8192, four generation and batch threads, effective batch 2048 and microbatch 512, temperature 0, seed 42. Thinking is disabled through `--reasoning off`; the request also carries `enable_thinking:false`. The schema is constrained through the tested `response_format.json_schema` wrapper.

An initial synthetic smoke found that the direct `response_format.schema` shape shown in the pinned README was ignored. The standard nested wrapper produced the required schema. A separate synthetic answer invented a postcode in normalized output despite a correct raw quote; the pilot therefore checks raw quotation, normalization support and formal validation separately. These synthetic trials did not become reference labels.

The initial 2500-token output cap was reached on a tuning act with many actors. The final cap is 3072, evidence quotes are requested to be short, and context/output-limit failures trigger subdivision up to depth two. Failed work is retained and charged. Residual incompleteness is visible. Prompt/schema and inference settings were fixed before the 17 evaluation groups; they were not adjusted to individual holdout answers.

Metal startup logs confirm 33/33 language-model layers on MTL0 and the visual encoder using MTL0. Quality evaluation uses this acceleration. A separate CPU-only control disables language, visual, KV/recurrent-state and operation offload and is verified from actual backend logs.

Primary sources: [official Qwen model card and license](https://huggingface.co/Qwen/Qwen3.5-4B), [pinned converter files](https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/tree/e87f176479d0855a907a41277aca2f8ee7a09523), [official runtime release](https://github.com/ggml-org/llama.cpp/releases/tag/b12128). The actual tested request behavior governs where upstream documentation and implementation differ.

## Evaluation interpretation

C/D “main” in the evaluator is an analytical normalized candidate projection, without a source-acceptance gate; it is not an application main-card output. The evaluator uses explicit reference names/aliases to align predicted participants for scoring. **This is not an operational primary-party selector.** It cannot be presented as a ready automatic card-filling path. No fuzzy or gender-collapsing morphological identity matching is used. Role correctness and owner evidence are reported separately from field-string correctness.

A's main fields, displayed review, raw same-identity suggestion pool, and explicit-copy INN proposal remain separate. A model value already available in these baseline sources is a promotion opportunity, not a new discovery. Suggestions associated with another identity do not receive credit.

Address exactness ignores punctuation/spacing but retains every alphanumeric component. Separate postcode and apartment checks help explain misses; they do not make an incomplete address exact. A genuinely unknown source type can be withheld. Missing an explicitly printed type is a miss. A generic address label never proves actual residence.

Correction counts are a proxy: wrong plus missing target slots, not employee time or clicks. They omit some work selecting participants, resolving roles and comparing alternatives. The partial actor reference does not support exhaustive secondary-role precision or a zero wrong-owner claim.

A verified quote confirms raw text presence. It does not prove normalized value, participant ownership, role or temporal status. Source/owner quote qualification is syntactic only. D's page-only citations remain distinct from C's verified word or line localization where available. Every experimental output remains review-only; `reconcile.py` preserves occurrences, unconfirmed same-name links, candidates, metadata/role conflicts and coverage without using gold or merging business identities.

## Technical corrections after freeze

The frozen inputs, prompt, model and inference parameters remain unchanged. Separate ledgers disclose these technical corrections:

- An evaluator denominator bug had treated a missing explicit address type as unknown abstention. All routes now count it as missing.
- Repeated distinct keys within one predicted participant had been last-value-wins. Main is now withheld and alternatives retained. The original assertions remain available.
- A valid source ID could hide an additional nonexistent ID. Every cited ID must now exist. Saved answers are rebound offline with before/after file hashes and unchanged assertion hashes, without new model inference.
- Offline rebinding originally used the full page for a retry child. It now reconstructs the exact C/PDF line subset recursively and inherits merged-parent checks only from children. For D image crops, whole-page OCR corroboration is downgraded to unverified page evidence because it cannot prove the quote was visible inside that crop. Raw assertions are unchanged.
- A diagnostic image filename could be reused by different recursive branches after their sequential requests completed. This did not change the images already sent, but earlier crop artifacts require deterministic reconstruction from the frozen original and saved call tree. Reconstructed inputs are labeled as reconstructed, not historical wire-image captures; future names include branch lineage and input hash.

A final isolated transport hardening disables environment proxies and redirects for document-bearing loopback inference. The measured host proxy configuration was empty; no external transfer was observed. Synthetic local HTTP checks cover the hardening. Prompt/schema/inference settings are unchanged.

An initially suspected whitespace-coordinate defect was independently retracted: real TSV/PDF word offsets and line offsets use the same cleaned-text coordinate basis. The reviewer checked 16,844 stored offsets and reproduced the real generator. No unnecessary coordinate repair was made.

## Confirmed diagnostic failures

Independent agent post-hoc visual adjudication (not human verification) is preserved separately from frozen scoring labels. In the company tuning act (IMG_3583 logical page 1; the matching PDF begins with the same act), D correctly associates the company's INN/OGRN but also calls a former manager a debtor and labels former company officials as managers. C misses the explicit company debtor and makes other role errors. This is not just OCR character loss.

On IMG_3597 logical page 1, in the introductory participant paragraph and following filing-manager paragraph, C assigns another person's INN, SNILS, registration address and address type to the manager who filed the request; it also turns a birthplace into an address. D assigns the other person's address to that manager and misses the visible identifiers. A/B have no nonempty personal main fields or displayed personal suggestions on this file; A marks its suggestion search incomplete. Thus these are newly introduced wrong assertions relative to the available baseline outputs, alongside baseline omissions. The introductory parenthesis contains the attributes of the other named person. A following paragraph explicitly identifies the filing manager. Cited text can be present while the owner link is wrong.

These scoped diagnostics must accompany any automated wrong-owner counter, which detects only a limited subset of errors covered by annotated actors. They are not a corpus-wide error rate. Awkward wording and temporal uncertainty are preserved separately rather than converted into confident legal conclusions.

A separate deterministic B diagnostic withholds actual address type when the nearest source label is only generic `адрес`. It changes two displayed suggestions without consulting gold/model output. That correction belongs to program logic, not neural-model benefit; it has not been applied to the office application.

## Measurements and final results

The independent-comparison slice is 17 act groups, with 128 known nonempty target facts, two genuinely unspecified address-type states, one excluded name normalization and 22 not-expected slots. Count populated facts separately from correct abstention. Unknown-type inventions count as errors and are not additional expected facts. This is a finite, agent-checked corpus measurement, not a population accuracy estimate.

| Route / projection | Correct populated facts | Wrong / unsupported expected-state choices | Missing known facts | Correction-slot proxy | Charged local cycle, 17 groups |
| --- | ---: | ---: | ---: | ---: | ---: |
| A primary parser fields | 53 | 3 | 72 | 75 | 251.63 s |
| A displayed review fields | 64 | 16 | 50 | 66 | included above |
| B primary parser fields | 48 | 3 | 77 | 80 | 54.97 s |
| B displayed review fields | 56 | 11 | 63 | 74 | included above |
| C normalized candidate projection* | 54 | 7 | 67 | 74 | 1,475.93 s |
| D normalized candidate projection* | 36 | 8 | 84 | 92 | 2,072.03 s |

*Gold-name alignment is used only to measure C/D fact extraction. These are optimistic candidate projections, without an operational owner selector or source-acceptance gate. They are not completed application main cards. Formal checks, normalization gates, quote status and confirmed wrong-owner assertions are separate; a correct string does not qualify its role/owner link. Main fields and displayed review are alternative views, not additive counts. The full same-identity proposal pool is evaluated separately below; a promotion is not a new discovery.

B reduces charged local reading/extraction time by about 78% against A, but loses five correct primary facts and eight correct displayed review facts. The pilot does not accept that quality trade. Only two B sources trigger the confidence-based second OCR attempt across the complete corpus; mean confidence alone does not establish that all necessary identifiers/addresses were read.

C has 34 correct personal-field values versus A's six primary personal-field values; metadata has 20 correct versus A's 47. D has 24 correct personal-field values and 12 correct metadata values. The model therefore exposes more personal material, while whole-document date/case/court reconciliation loses many metadata values. It does not deliver an overall corrected card. C/D each finish 11 holdout sources without an incomplete flag and retain six incomplete sources. These sources stay in the denominator, with missing target facts counted as misses.

Strict case comparison leaves one wrong C case number and three wrong D case numbers. Two D failures are only Latin/Cyrillic homoglyph normalization, not different case digits. A uniform host normalizer could correct those two without neural-model credit; it would not change the route decision.

The clean-text PDF control takes 1,222.12 s of C calls for seven pages, with five incomplete after bounded retries. D reuses this identical text control; it is not a second observation. Thus output/reconciliation problems persist with good text and cannot all be assigned to OCR.

### Existing proposals versus model benefit

A already contains correct same-identity suggestions for 20 held-out field slots missing from its primary parser output: seven names, eight INNs, four SNILS and one address type. This pool overlaps displayed review and must not be added to the 64 correct displayed fields. The union of A primary and displayed review contains 80 known correct field slots; the same-identity raw pool makes 81. These are available field-level opportunities, not 81 automatically coherent or accepted owner/role links. Values in suggestions for another identity are listed diagnostically and do not receive useful-owner credit. The separately replayed existing alternate-INN explicit-copy projection adds one tuning proposal (IMG_3584), no new holdout fact; it is not a browser-click measurement or model discovery.

Among C's 34 correct personal candidate fields, 17 are new beyond A's main/displayed/same-identity proposal pool, 16 are opportunities to promote values already available elsewhere in A, and one was already correct in A primary. D's corresponding split is 12 new, 11 already available elsewhere, one already primary. Including metadata, C has 18 new correct values (the extra is court); D has 14 (case number and court). C's new personal values are two names, five SNILS, seven complete addresses and three explicit address types. No C INN is a new discovery against A. These are oracle-aligned fact opportunities, not automatically acceptable cards.

| Personal field, correct values | A displayed review | B displayed review | C candidate | D candidate |
| --- | ---: | ---: | ---: | ---: |
| Name | 11 | 11 | 10 | 7 |
| INN | 12 | 9 | 6 | 6 |
| SNILS | 7 | 4 | 7 | 5 |
| Complete address | 1 | 0 | 7 | 5 |
| Explicit address type | 2 | 3 | 4 | 1 |

A displayed review already has 33 correct personal-field values versus C's 34 and D's 24. The model benefit is complementary coverage, especially addresses, rather than a demonstrated overall replacement. C also loses existing useful values and adds wrong candidate associations. Keep the baseline pool in any future comparison.

### Addresses, roles and source qualification

For 17 expected full addresses, A primary has one exact complete value, B zero, C seven, D five. A displayed review has one complete address and seven wrong/incomplete strings; B displayed review has zero complete and five wrong/incomplete strings. Apartment and postcode components are not substitutes for a complete address. Of nine expected apartment/unit components, C preserves five and D three; of eleven expected postcodes, C preserves four and D five. These are primary/candidate component metrics; they do not assert that every existing raw suggestion is an accepted address. C correctly gives four explicit address types and D one; A primary gives one and B zero. A/B each invent a type for two unspecified-type displayed slots. The separate generic-label guard withholds these two B suggestions without model credit.

The partial primary-role reference finds 8/17 expected roles in C and 5/17 in D. False-positive roles for all secondary actors cannot be scored exhaustively. The separate four-response visual diagnostic contains 43 raw role assertions including repeats, 37 right role types and six wrong types. Temporal-status uncertainty is separate. On IMG_3597, the new wrong-owner fields described above are present even though the raw strings may occur in the source. No automated zero counter establishes safety.

Only eight of C's 34 correct personal candidate fields have a syntactically verified value quote with word localization; ten have a verified owner quote; just three have both, and those three also have supported normalization. This still does not prove semantic ownership or role. D has two correct personal candidates with page-level value/owner corroboration; only one also has supported normalization. D cropped-retry quotes remain unverified at page level. Thus value-only correctness is much broader than the actually traceable result. The limited source performance is a concrete integration blocker, not a license to draw precise arrows from model-generated coordinates.

The source-preserving path is: immutable document hash → logical page/fragment → supplied line IDs and raw quote → programmatically verified OCR/PDF word spans → crop inverse transform → original photograph coordinates. C quotes with only line evidence must display a line, and invalid/unmatched citations must display a warning without a word arrow. D currently permits page navigation only; a later exact-word arrow requires a fresh local crop OCR/match and measured cost. Raw assertion, normalized value, formal result, owner quote, role quote, alternatives and incomplete coverage remain separate in private JSON and review-only reconciliation sidecars. No missing value is converted into source absence.

### Separating reading failure from aggregation failure

First-page metadata diagnostics materially change the interpretation of the poor full-source score. C reads a gold-matching date on 15/17 first logical pages, but full-source projection retains it on only 1/17; case number is 14/17 versus 8/17 and court 15/17 versus 11/17. D's date is 16/17 first-page versus 1/17 full-source. These diagnostics are not additional tuned route results. Later fragments include historical acts, and the experimental merger cannot determine which heading is authoritative, so conflicting metadata is withheld; within a subdivided page its earlier parent projection can retain the first candidate with a warning. The gold-free sidecar preserves all terminal alternatives and lineage. This is a document/occurrence aggregation problem in addition to model mistakes, not evidence that 4B cannot read a date or that OCR replacement alone will fix the card.

The remaining disputed-reference list is short: IMG_3592 surname normalization is excluded; IMG_3596 has a separately documented blind-review INN transcription correction; tuning IMG_3588 has disputed case/INN markers excluded from the split-only diagnostic; IMG_3597's debtor role is partly contextual but ownership of the introductory parenthetical identifiers/address is much firmer. Background sheet fragments and incomplete photographed acts never establish exhaustive absence. No human has accepted this reference.

### Stage timing

| Stage, 17 groups | A | B | C | D |
| --- | ---: | ---: | ---: | ---: |
| Current parser including OCR/source collection | 251.623 s | — | — | — |
| Current review projection | 0.011 s | — | — | — |
| Source decode/rotation/crop writing, charged from manifest | inside parser | 3.882 s | 3.882 s | 3.882 s |
| Prepared-page preprocessing | — | 2.910 s | same B reader charged | same B reader charged |
| Primary OCR | inside parser | 46.378 s | same B reader charged | same B reader charged |
| OCR retry | inside parser | 1.414 s | same B reader charged | same B reader charged |
| Extraction/source/review projection | inside parser | 0.138 s | same B reader charged | same B reader charged |
| Model calls including failed attempts | — | — | 1,420.080 s | 2,012.198 s |
| Recorded source binding inside calls | inside parser | above | 0.114 s | 0.064 s |
| Complete model-call wrapper including image/template work | — | — | 1,420.954 s | 2,017.061 s |

Do not add the complete wrapper to its model/binding components. Minor reader/wrapper overhead explains totals. Offline final evidence revalidation plus conflict projection costs a further 0.309 s for all 44 saved C/D source results, with zero inference calls; it is reported separately from the original measured cycle. Reference annotation, test execution and evaluator analysis are not card-processing time.

The complete A 22-source run takes 308.06 s. B takes 64.25 s plus 6.37 s of recorded corpus preparation, 70.62 s total. Those totals include duplicate/related controls and are not 22 independent documents. A and initial B competed for CPU; B-final partly overlapped tuning inference. The primary C/D holdout model requests were sequential. These are shared-Mac component measurements, not isolated production queue throughput.

### What splitting alone changed

A matched PSM3 probe on tuning spread IMG_3588 uses the same preprocessing once on the full upright image and once per frozen crop. Full OCR: 2.922 s; two crops: 2.994 s. Including preparation, 3.336 s versus 3.385 s. This single-run difference is not a speed gain. Splitting recovers an explicit registration label; both forms find name/date/SNILS/role markers, and neither finds the selected street/unit markers. Disputed INN/case markers are excluded from this diagnostic. Non-seam full-frame order is already left-to-right with zero reverse page transitions; 11 seam-ambiguous lines remain. Do not claim that this example proves universal interleaving damage or cures addresses. Crop bounds preserve the full outer margins and 160-pixel seam overlap, with inverse transforms inside the original. The large A/B time difference mainly concerns fewer OCR passes and associated preparation, not a measured speed benefit of splitting alone.

### Matched cache-zero resource control

This separate tuning-page control uses IMG_3584, the same frozen inputs, schema and output cap, one request at a time, and cache disabled in both modes. It does not enter holdout quality scores.

| Mode | C request | D request | Generation speed C / D | Reported footprint after C / D |
| --- | ---: | ---: | ---: | ---: |
| Metal, cache zero | 44.79 s | 42.61 s | 53.17 / 53.26 tokens/s | 1.4G / 1.6G |
| CPU only, cache zero, four threads | 90.73 s | 135.05 s | 30.75 / 28.85 tokens/s | 3.9G / 4.3G |

Outputs differ slightly in length despite fixed temperature/seed: C 2219/2252 generated tokens on Metal/CPU; D 1817/1823. The comparison has matched inference inputs/settings, not bit-identical answers. CPU logging uses verbosity 5 to prove placement, versus Metal verbosity 3; its logging overhead is not separately measured. CPU D prompt processing takes 71.71 s including about 43.65 s of visual encoding; decode takes 63.14 s. Do not charge encoder time a second time. The text-only CPU C prompt takes 17.48 s and decode 73.20 s. No cached prompt tokens are reported.

Sampled server-tree RSS peaks are 4058.9 MiB for cache-zero Metal and 5741.2 MiB for CPU; the latter sampler covers both requests and subsequent idle time. These are sampled resident peaks, not unique system-memory totals. Whole-system swap during the matched request windows changes from 34,923.69 to 34,747.69 MiB (Metal) and 27,278.94 to 26,958.81 MiB (CPU). Earlier main-run sampling spans 28,245.06–40,185.31 MiB used swap, while the host already used 23,668.31 MiB before the pilot. These shared-host changes cannot be assigned entirely to the model. Footprint, RSS, mapped files and compressed/swapped pages use different accounting and must not be summed blindly. Full state/log hashes and sampler windows are in [resource_summary.json](../../scripts/dev/extraction_pilot/resource_summary.json).

Actual CPU placement is verified: 0/33 layers offloaded, all layer assignments CPU, CPU_REPACK model buffer 2599.83 MiB, CPU CLIP backend, CPU KV 256 MiB, CPU recurrent-state 50.25 MiB, CPU scheduler/projector compute buffers 87.02/223.30 MiB. Generic MTL0 device discovery is not offload. These buffers are not a complete application-memory total and are not additive to RSS/footprint without accounting for overlap.

The original quality run retained the runtime default `--cache-ram 8192` MiB and idle prompt-cache saving despite request `cache_prompt:false`. It reached the larger footprint snapshots below. For later bounded experiments, explicitly use `--cache-ram 0 --no-cache-idle-slots`; do not budget only GGUF bytes or assume one active request bounds saved prompts. The cache-zero control is one page, not a full batch memory qualification.


Measured hardware is Apple M4 Pro, 12 CPU cores, 25,769,803,776 bytes installed unified RAM, or 24 GiB. This is not 24 GiB CPU RAM plus separate GPU RAM. Process address-space/data/RSS limits are unlimited. Free-page counters and system memory pressure are snapshots, not installed RAM or a promise of model-available capacity.

The Mac already used approximately 23.7 GiB system swap at pilot start. Whole-system swap activity includes unrelated applications. Reported deltas cannot all be assigned to the model. No unrelated service/application was stopped and caches were not forcibly purged.

RSS alone understates this Metal process: approximately 4 GiB RSS coexists with macOS-accounted physical footprint around 9.4 GiB, later around 9.8 GiB with a 10.4 GiB peak snapshot. These are Mac accounting observations, not a CPU VPS requirement. Cache-zero CPU and Metal observations above use different accounting quantities; neither RSS nor footprint alone is a complete cross-platform memory budget.

Initial process readiness took 6.466 seconds; subsequent warm Metal restarts took about 1.02 seconds and CPU readiness 3.559 seconds. The OS file cache was not flushed, so these are process-load measurements, not cold-disk guarantees. C/D reader timings include B's OCR even when D uses it only after inference for source corroboration. The local cycle excludes office HTTP upload, production queue wait, database/card transactions, browser rendering and human review.

## Implementation decision

Keep A as the office default while fixing deterministic preparation and review behavior in a separately accepted change. Do not replace the full photo reader with B's single-pass rule: its speed gain came with a loss in the existing employee-visible suggestions. Do not promote C/D outputs into authoritative card fields. C is suitable only for a bounded, review-only experiment after occurrence-level ownership and source validation are tightened. D is not a replacement for complete OCR on this corpus.

OCR remains the default raster reader and geometry provider; useful PDF text bypasses OCR. The first implementation should preserve direct PDF text and current recovery, add reversible spread preparation with original-coordinate transforms and an explicit unsplit fallback, and test an adaptive OCR policy against the existing suggestion pool. A generic address label must remain an unknown type; source date selection must distinguish the current heading from cited older acts. These are program/preparation changes and must not be credited to the model. The pilot adapters do not modify application behavior.

Do not download 9B next merely to compensate for this result. First reduce output duplication, represent multiple dated acts and occurrences without destructive metadata merging, and build human-verified ownership/role labels. A later 9B comparison is warranted only if a frozen, smaller extraction task still has semantic ownership errors and a declared CPU latency/memory budget can be tested. Neither loading 4B into memory nor this accelerated Mac experiment qualifies bulk VPS operation.

## Server implications and acceptance gate

Treat MSKs-3 (4 CPU, 8 GB, 120 GB, 2,373 RUB/month) and MSKs-4 (8 CPU, 16 GB, 240 GB, 4,746 RUB/month) as CPU-only; these are owner-provided tariff specifications, not newly rented/tested servers. Disk capacity is not the limiting factor for the approximately 3.18 GiB of model/projector files. Application, database, Redis, OCR subprocesses, inference working buffers, context and cache need memory in addition to those files.

On a shared 8 GB machine, do not add a resident visual model to the office service as a supported configuration. On shared 16 GB, a single text-only inference worker with bounded cache/context and coordinated OCR admission is a candidate for measurement, not an accepted configuration. The current four OCR processes plus a four-thread model can compete for all eight CPU threads before accounting for web/database work. Do not retain four OCR workers and add an independent inference queue with unconstrained scheduling.

A separate 16 GB CPU machine isolates web/database responsiveness and is the more reasonable later qualification target, but doubles the machine count and does not solve inference latency or ownership quality. A separate 8 GB text-only experiment might load the model with carefully bounded settings; it is not qualified for visual batches or a promised service level. This pilot changes no infrastructure and measures neither VPS tariff.

Acceptance of the next small implementation requires a frozen human-checked set of distinct acts, including organizations, multiple addresses and historical/current managers. Report main fields and all employee proposals separately. Require zero observed newly introduced wrong-owner/role assertions in the accepted slice, no address/apartment/type regressions, valid document/page/fragment mappings, explicit unread/incomplete coverage and no unsupported normalized additions. Compare correction-slot counts and measured full-cycle p50/p95 on the actual target machine under a declared batch and web/database load. Employee minutes can be claimed only after an actual employee review study. A held-out failure must remain a failure or move into a newly declared tuning set; never silently retune its prompt.


## Reproduction, verification and retained evidence

[README](../../scripts/dev/extraction_pilot/README.md) records commands; [run-config.json](../../scripts/dev/extraction_pilot/run-config.json) freezes inference settings; [results-summary.json](../../scripts/dev/extraction_pilot/results-summary.json) contains aggregate outcomes only. Public weights/runtime are pinned by byte count, snapshot/commit and SHA256 in `setup_runtime.py` and the resource summary. Existing OCR is Tesseract 5.5.3 (Russian traineddata SHA256 `e16e5e036cce1d9ec2b00063cf8b54472625b9e14d893a169e2b0dedeb4df225`), Python 3.13.6, Pillow 13.0.0, pillow-heif 1.8.1, pymorphy3 2.0.6, pytesseract 0.3.13 and Poppler 26.10.0; the private version snapshot preserves full details.

Private evidence remains under `.local-run/extraction-pilot-20261105/`: immutable originals/page hashes and map, frozen labels plus separate errata, full per-source results, retained failed attempts, reconstructed retry inputs, rebind assertion digests, reconciliation sidecars and resource logs. No real document content, labels, identifiers or weights are committed. The split-only marker configuration is private and hashed; the script emits booleans only. Annotation/gold loading belongs to reference/evaluation, never to inference routing.

All 29 focused synthetic tests pass, including coordinate transforms, repeated source occurrences, invalid/mixed line IDs, fragment-local revalidation, inherited parent evidence, visual-crop evidence downgrade, duplicate field conflicts, unknown address type, loopback proxy/redirect protection and conflict/coverage projection. Python compilation passes. Independent agent review checked reference samples, privacy, result-table totals and evidence interpretation; this is not human acceptance or production qualification. No application parser/service behavior was changed, no working database or external SRO action was executed, and no infrastructure was changed. Only the owned pilot model processes were stopped; weights remain available locally and port 18091 is free.
