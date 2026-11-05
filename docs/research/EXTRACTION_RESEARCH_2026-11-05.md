# SRO Office document extraction research

Recommendation: retain page-aware PDF text extraction and local OCR with word geometry, and evaluate one local Qwen3.5-4B model for participant identification, roles and field ownership. Use vision selectively on unresolved regions, rather than requiring vision to read every page. Start with a shadow evaluation, not a pipeline replacement. Direct vision remains a comparator, not a demonstrated improvement.

This decision addresses the observed reading failures and the separate, more important limitation that the current output represents one debtor rather than all participants. A single semantic model with deterministic preparation, validation and card projection fits the project. Its tools should be bounded read-only functions controlled by the application, not an autonomous external-service agent.

## Scope and evidence

Source inspected on 2026-11-05: clean `develop`, HEAD `f4ad5d9b03d8014b5b6c12862c6aa9ef286407f3`. Applicable repository AGENTS.md, README, memory-bank current-state/index/processing references, pinned requirements, cloud systemd and local startup configuration were inspected. README and historical sections of `.now.md` contain superseded limitations; current code takes precedence.

No production access/change, working-database read/write, SRO interaction, document upload to inference APIs, model download or installation was performed. Existing local services were preserved. Research invokes the local core directly; the review projection uses synthetic database configuration pointing to port 1 and in-memory task objects, with an empty private environment file. No database session is opened.

Local hardware: Apple M4 Pro, 12 CPU cores, 24 GiB RAM; Python 3.13 environment, Tesseract 5.5.3 with `rus` and `osd`, Pillow HEIF decoder and Poppler installed. These are Mac CPU OCR measurements, not VPS measurements. No suitable GGUF/safetensors weights were found in checked standard local caches; no Ollama/llama CLI or torch/transformers/MLX/llama_cpp package was available in the project environment. This does not constitute an exhaustive search of every user directory.

Private reproducible inputs, diagnostic source, hashes, renders, OCR outputs and detailed card projections are retained under ignored `.local-run/extraction-research-20261105/`. Source documents and extracted personal values are excluded from Git. `probe.py`, `project_review.py`, and `pool_probe.py` contain the exact local commands; compact results are retained there as JSON. Initial diagnostics failed on an entity dataclass lacking `to_dict`, and on the empty diagnostic env file's permissions; dataclass serialization and mode 0600 resolved these diagnostic errors without changing application code. Failed output remains private.

## Actual corpus and visual inspection

`samples` contains exactly 21 files, IMG_3583.HEIC through IMG_3603.HEIC, all single-frame HEIF, each 4032 by 3024 or 3024 by 4032 pixels. There are no PDFs, JPEGs or PNGs in this directory at inspection. The inventory hashes every original.

All 21 originals were decoded locally and visually inspected in contact sheets. Eight representative originals were additionally inspected at larger resolution: 3583, 3584, 3588, 3594, 3597, 3600, 3602, 3603. The main photographed material has 3 single-page views and 18 two-page spreads, approximately 39 main printed page faces, excluding background-sheet fragments. These are not 39 independently uploaded pages or proof of complete acts. 3584 and 3603 photograph the same first page, with different framing; a train/test split by file would leak this act.

Most pages are human-readable when rotated and enlarged. Difficulties include sideways orientation, perspective, page folds, shadows, low-contrast photocopy text, tiny glyphs in two-page spreads, blue certification stamps, handwritten marks over headings/identifiers, and background pages. A simple image resize cannot recover occluded characters. Several acts visibly extend beyond the photographed pages.

Ordinary personal bankruptcy acts typically have one debtor/applicant, judge/secretary and an SRO or manager, plus agencies and service recipients. The corpus does not have a manually counted participant census. The company act in 3583 is much more complex: nested company names, an entrepreneur creditor, former managers, a proposed manager and former company officials. Role changes over time matter. References to a requested future manager are not an appointed manager.

Personal identifiers and residential addresses often occur in the introductory parenthesis after the person's name; apartments wrap onto the next line. Court addresses occur in headers and hearing directions; SRO and agency addresses occur later. Birthplace is another easily confused location. Multiple addresses occur for different actors. In the supplemental PDF, individual SROs also have several explicitly typed addresses; the supplied photo subset does not establish the frequency of multiple residential addresses for the same debtor.

A supplemental real local PDF, `.local-run/private-task10/source.pdf`, contains the same company act as 3583: 7 A4 pages. Page 1 was rendered at larger resolution and pages 2-7 as a contact sheet. Extracted text per page has 3846, 4124, 3790, 4150, 4189, 4420 and 1206 nonblank characters. Its text layer is useful and agrees with the inspected content; this is one real PDF, not a qualification of all PDF types. Pages 5-6 enumerate many SROs, identifiers and addresses; reducing the document to the first page would omit important parties. A complete participant model therefore needs page coverage and repeated-mention reconciliation.

## Existing processing map

| Stage | Current implementation and evidence |
| --- | --- |
| Intake | `app/api/routes_tasks.py:801-928`: admission limits, idempotent operation, source storage, intake item/task per file, OCR outbox, commit before dispatch. `app/services/tasks.py:773` records initiator. Browser selection is chunked; byte deduplication is distinct from a business occurrence. |
| Worker | `app/workers/jobs.py:50-136`: task claim, source-hash validation, parser with budgets, fenced completion. `app/services/leases.py` supplies owner/generation fencing and reconciliation. |
| PDF | `sro_robot/core/ocr.py:669-778`: `pdftotext -layout`, per-page heuristic, OCR for short pages, two-page raster batches at 300 DPI. Fast text route collects `-bbox-layout` geometry. The 120-character page / 400-character document tests are heuristics, not semantic quality checks. Long but corrupt hidden text can pass. |
| Photos | `ocr.py:231-263,445-535,785-819`: decode HEIC, proportional resize to max side 3400, OSD orientation, grayscale/autocontrast/unsharp mask; PSM 3/4/6/11 prepared passes and one original-resolution PSM3. Text and TSV share each reading invocation. |
| Extraction | `extract.py:1635-1734`: one typed card; explicit entity marker selects `entity_extract.parse_entity`, otherwise `extract.parse`. Optional INN recovery can run additional OCR, and PDF low-resolution date cross-check can parse another view. |
| Ownership | `extract.py:225,1206,1278,1436,1503`: bounded debtor sections, conflict withholding, application-person review candidates, separately bound applicant. `entity_extract.py:109,262-351,447-520`: one explicit organization/IP debtor section and cross-variant agreement. |
| Normalization | `morph.py:71` normalizes oblique FIO; `validators.py` validates identifiers and case strings; `person_attributes.py` and address rules bound text. `address.py:19` later splits the selected address for the person external form; leftovers go into street rather than being dropped. |
| Provenance | `models.py:31-51`: raw excerpt separate from value, variant/span/page/boxes. `source_locations.py:193` binds parser spans to word geometry. A source variant is not a page. |
| Review | `app/services/tasks.py:839-877` persists fields, suggestions and applicant/route evidence. `app/services/fields.py:55-330` chooses a coherent candidate occurrence, respects employee values and conditionally keeps apartment extensions. The displayed card can differ from authoritative parser fields. `app/web/static/field-source.js` draws source underlines/connectors. |
| Cards | `app/services/assignments.py:279,320-442,549` handles guarded person preparation, reviewed entity cards and applicant assignment drafts. `app/db/models.py:499` models one debtor card, not a document-wide party graph. External actions remain separate. |

Four OCR workers are supported by `scripts/dev/run_worker.py`, with leases required for the pool. Default code/cloud service command is one worker; `.now.md` reports a production drop-in running four on an upgraded 8-vCPU/16-GiB host. That is repository-recorded operational evidence, not a fresh live host measurement. No deployment or root environment values were read. `OMP_THREAD_LIMIT=1` prevents each Tesseract child from independently occupying every core. Four workers run passes sequentially per document: this is four simultaneous OCR children, not twenty. Poppler, decoding, Python and the service add load.

Limits include 80 PDF pages, 12 million raster pixels, 80 TIFF frames, 540-second core deadline and 600-second RQ timeout. TIFF and scanned/hybrid PDF paths do not currently collect word geometry through the photo collector. Fast text-PDF geometry is implemented; scanned/hybrid PDF arrows remain a gap.

## Reproduced errors and useful existing behavior

The following are fresh `parse_file` results and the actual pure review projection, compared against enlarged originals. No office-wide accuracy percentage is claimed. Examples refer to the left/right printed page within the photograph; the current HEIC source model calls the entire image page 1.

| Input and region | Observation | Cause classification |
| --- | --- | --- |
| 3583, main page heading and debtor paragraph | Header says 22 October 2026, but output date is 02.05.2023 with warning and a box on the historical date. Company name is visibly damaged and OGRN absent; INN is retained with warning. | 1 + 2: handwriting damages OCR; date regex cannot consume `22\\октября`, then finds a historic date. `find_doc_date` at `extract.py:718` and `_header:694` choose text patterns, not act semantics. Arrow can be geometrically correct for the wrong semantic date. |
| 3584, introductory person parenthesis | OCR has SNILS and full apartment address. Authoritative card omits both; review projection correctly restores them as uncertain candidates, including apartment. INN has an extra digit in the shown prepared OCR modes and remains empty. | 1 for INN; role/field admission affects the authoritative result, while review recovery is useful. Do not count every blank parser field as a blank employee card. |
| 3588, left heading and person paragraph | Wrong birth-date-like output 10.02.1900, missing case, damaged patronymic; original act heading is 23 October 2026. Prepared modes include sideways/noisy reading, original pass recovers some text. | 1 + 2: orientation/layout/character errors followed by selection of another date. Morphology cannot restore an OCR-damaged name reliably. No separate proven normalization corruption was established here. |
| 3594, left page middle | A good OCR variant contains labeled INN, SNILS and registration address, matching the original. Review shows name but leaves these fields blank with debtor-role warning. Other modes interleave text from the right page into the same sentences. | 1 for spread reading order; 2/3-boundary omission for conservative association. Correct strings exist but the current grammar cannot establish a usable coherent debtor section. This is not a demonstrated transfer of fields to a wrong person. |
| 3597, left page introduction | Manager requests release from duties concerning a different person; the review debtor identity is blank. | 3 risk handled conservatively: withholding is safer than assigning the manager's name to the debtor. General roles and an explicit relation are needed. The second person's identity paragraph does not by itself prove the nearest role label applies to them. |
| 3600, left introduction | Review has matching name, identifiers and registration address; warnings remain. No apartment is printed in the inspected address. | Useful existing behavior; do not invent an apartment. Not every photo requires a replacement OCR engine. |
| 3602, left introduction | Review has a full address with apartment, but omits the printed postcode and region prefix. It assigns `actual` to a source labeled only `адрес`. Case is absent. | 1/2 for truncation and case; 4/semantic normalization for invented address type. `person_attributes.py:43-45,73` and `extract.py:1216,1275` map generic address labels to actual. This conflicts with the requested unknown-type behavior. |
| 3603 versus 3584, same printed page | 3603 finds INN but review loses SNILS/address; 3584 retains SNILS/address but loses INN. | 1 + 2: framing changes OCR variants and chosen evidence. Neither photo alone is a full exact card; duplicate files are not independent evidence of broad quality. |
| Supplemental text PDF, page 1 | Correct full nested company name, legal form, INN, OGRN, case and 22.10.2026, with sources. | Useful direct-text route. Short name and debtor address remain blank; a field absent in this card is not proof that no address appears anywhere in the seven-page act. |

No fresh wrong-owner accepted record was created or observed. The findings establish misses, dangerous semantic choices and limitations of the output contract, not a measured wrong-owner rate. No independent human gold annotation or timed employee correction session exists. Source boxes were inspected as data and against the corresponding visible regions; this research did not repeat browser end-to-end arrow rendering acceptance.

## Measured cost

Fresh serial `parse_file`, eight representative HEIC originals, one process, `OMP_THREAD_LIMIT=1`, same source SHA256s:

| File suffix | Total seconds | Recognition seconds |
| --- | ---: | ---: |
| 3583 | 11.641 | 11.628 |
| 3584 | 9.816 | 9.761 |
| 3588 | 22.209 | 22.198 |
| 3594 | 8.361 | 8.351 |
| 3597 | 11.104 | 11.089 |
| 3600 | 15.592 | 15.569 |
| 3602 | 15.674 | 15.649 |
| 3603 | 10.382 | 10.369 |

Total 104.780 seconds, median 11.373 seconds. Recognition consumes 104.616 seconds; post-recognition extraction and source binding consume 0.164 seconds, about 0.16% of this serial total. Every file launches six Tesseract subprocesses: OSD plus five reading passes. No optional recovery OCR was triggered on these eight. Pure person review projection takes approximately 0.1-0.5 ms per object. These timings do not justify deleting validation or morphology as a speed optimization.

A second fresh run of the same eight files using four spawned Python processes completed in 31.421 seconds, including pool startup. Sampled sum RSS of the owned process tree peaked at 1,890,746,368 bytes (1.76 GiB), sampled through `ps` every 100 ms. This sums shared pages repeatedly and includes the sampler; it is not unique physical memory or a production capacity bound. Relative to the single serial run, observed batch speedup is 3.33x; neither run was repeated enough to estimate variance. No model or service ran in this pool.

Parent-process maximum RSS reaches 346,390,528 bytes (330 MiB), excluding children; this is not total OCR memory. The supplemental seven-page text PDF takes 0.091 seconds for `parse_file`, after a separate text-layer inspection. It is a warm local run, not an average production PDF latency.

The measured path includes image preparation, OSD, five passes, text/TSV, parsing and source binding, plus separately measured review projection. Upload/network time, queue waiting, database commit/internal-card transactions, browser rendering and human review are not included. Full upload-to-card latency cannot be derived by dividing these totals by four. No model latency, model memory, tokens/second or VPS throughput was measured.

## Approach comparison

| Route | Quality and verification | CPU and memory implications | Project fit |
| --- | --- | --- | --- |
| A OCR plus semantic model | Word evidence readily verifies strings; model may resolve roles and ownership but cannot recover unreadable OCR without another view. Correct citations still need ownership/role evidence. | OCR cost plus text prefill/generation; sending all five variants multiplies context and noise. | Smallest change: reuse provenance, validators, queue, review and recovery. |
| B Images directly to multimodal model | May use layout/context where OCR interleaves pages, but exact digits, small print, omission and fabricated completion require corpus tests. Model boxes alone are not trusted word geometry. | Image encoding, visual tokens, crops, repeated calls and source alignment replace OCR cost. CPU speed is unknown; blanket removal of OCR has no demonstrated resource benefit. | Requires more source alignment and coverage machinery; a two-page spread should be split before either reader. |
| C Page-aware mixed route | Direct PDF text where valid; OCR words for photos/scans; same semantic model, with selective image/crop recovery. Preserve all candidate readings and ambiguity. | Avoids rasterization and image inference on good PDF text. Can progressively reduce photo passes if measured accuracy is retained. | Preferred: useful existing paths remain, semantic limitation is addressed explicitly. |

Retain OCR as the default evidence-producing reader for raster pages in the first pilot. It is unnecessary for good text PDFs and a selective helper in the future vision route. Five unconditional photo passes are candidates for measured reduction, not an architectural requirement. First compare a good single PSM3 pass with conditional extra modes/crops; 3594 proves that PSM6-style interleaving can add misleading evidence rather than independent confirmation. Agreement among OCR passes is correlated, not independent confidence.

Do not concatenate every OCR variant into one model prompt. Supply a canonical page/line/token view, reference alternative readings by region, and ask for recovery only where ambiguity matters. Short text-length thresholds should be supplemented with text/render coverage and plausibility checks. An unreadable or unprocessed page must survive as a coverage failure.

## Preferred model and deployment candidate

Preferred pilot: Qwen3.5-4B, GGUF Q4_K_M, CPU `llama-server`, one model instance and one inference slot, context 8192 tokens, output budget initially 2048 tokens, thinking disabled using the model's supported chat-template setting. Validate this exact configuration before integration. Long acts must be chunked by page/section with overlapping identity context and deterministic cross-page reconciliation; reserve prompt/output budget and never silently truncate the final pages. If output hits its limit, mark incomplete and continue bounded work, never interpret it as absence.

The official model card identifies a 4B language model with vision encoder and Apache-2.0 license. llama.cpp has merged Qwen3.5 text/vision support; pin an exact runtime commit and model revision/hash in the experiment, rather than depending on `latest`. Local runtime installation and compatibility smoke testing remain necessary. The selected GGUF conversion is distributed by its converter, not by Qwen itself.

Published artifact sizes: Unsloth Q4_K_M 4B is 2.74 GB, matching F16 mmproj 672 MB; Q5_K_M is 3.14 GB. The 9B Q4_K_M alternative is 5.68 GB plus 918 MB F16 mmproj. These are decimal download sizes, not resident memory. Plan about 4 GB disk for the 4B visual pair, plus runtime/cache/workspace; allow around 8 GB for 9B pair and overhead. Do not download the entire multi-quantization repository.

Use 4B text mode first without loading mmproj. For a vision comparison load the matching mmproj on CPU with `--no-mmproj-offload`, and initially allow one cropped page/region per request with a recorded pixel/token cap. Start at roughly one megapixel per page image plus bounded high-resolution crops; test 1 versus 2 MP on tiny digits, because shrinking an entire spread can erase the apartment/identifier. Keep HEIC decoding and orientation outside the model.

Illustrative text launch after installing a pinned compatible build:

`llama-server -m Qwen3.5-4B-Q4_K_M.gguf -ngl 0 -c 8192 -np 1 -t 4 -tb 4 -b 256 -ub 128 --host 127.0.0.1 --port 8091 --jinja`

Visual comparison adds `--mmproj mmproj-F16.gguf --no-mmproj-offload`. Chat-template thinking and schema constraints must be set in the request/build-supported configuration, not assumed from this launch line. Bind only to a private interface; block outbound document traffic. JSON Schema constrains syntax, not factual truth. Use an explicit request timeout, bounded recovery attempts, one durable model queue and idempotent result attachment to source hash/version.

Planning memory envelopes, NOT measurements: reserve 6 GiB for one 4B text slot at 8K; reserve 8 GiB for the visual pilot including transient image buffers. Reserve 10-12 GiB for a 9B text pilot and 12-14 GiB for vision. Actual working set depends on quantization, recurrent/KV state, batch size, image tokens and implementation; measure process/cgroup peak and swap. These are conservative allocations for admission, not minimum requirements or proof of throughput.

Alternative: Qwen3.5-9B Q4_K_M only if 4B makes ownership/role errors with correctly supplied text, and only on a separate 16-GiB machine initially. Its potential quality advantage on these acts is unmeasured. It does not repair missing photo characters by being larger in text mode. The same 4B vision mode is the first alternative for OCR reading failures, avoiding an additional semantic model or new OCR stack. Paddle/GLM/document-only OCR replacements are not justified before measuring whether reading or semantic errors dominate the labeled cohort.

Primary sources checked 2026-11-05:

- [Qwen3.5-4B model card](https://huggingface.co/Qwen/Qwen3.5-4B), [Qwen3.5-9B model card](https://huggingface.co/Qwen/Qwen3.5-9B): architecture, license, modes and chat-template behavior.
- [Merged llama.cpp Qwen3.5 support](https://github.com/ggml-org/llama.cpp/pull/19468): text and vision implementation support.
- [llama.cpp multimodal documentation](https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md): projector and CPU configuration.
- [llama-server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md): CPU, slots/context and structured JSON interfaces.
- [4B GGUF files](https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/tree/main), [9B GGUF files](https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/tree/main): exact published quantization/projector sizes. Converter support is not a measured Russian legal extraction benchmark.

## CPU tariff suitability

Prices/specifications are the owner's supplied inputs, not a live provider quote. CPU model, instruction set, contention and memory bandwidth remain unknown. Treat both as no-GPU hosts.

| Placement | MSKs-3 4 cores 8 GB at 2373 RUB | MSKs-4 8 cores 16 GB at 4746 RUB |
| --- | --- | --- |
| Service and model together | Do not select for this target workload. 4B may load, but service/Postgres/Redis/OCR plus model transients leave little memory and CPU headroom. No batch-throughput qualification. | Conditional low-cost pilot, admitted only after measuring baseline and preserving at least 2 GiB headroom: 4B text, one model slot, initially 2 OCR workers with OMP1 and 4 model threads, leaving nominal headroom for service/DB. Vision must use admission/scheduling that avoids coincident OCR peaks. Suitability for office volume remains contingent on latency and memory tests. |
| Dedicated model host | 4B text, one slot, four threads is a viable capacity experiment; visual operation is tight under the proposed 8-GiB reservation. Not a qualified bulk service. 9B is not recommended here. | Preferred isolated 4B comparison host, up to 6 threads initially; can test 9B serially if quality warrants. Isolation protects service latency but cannot make slow CPU inference fast. |

For the shared 16-GB pilot, provisionally reserve 5-6 GiB for OS/service/Postgres/Redis, approximately 1-2 GiB for two OCR workers and children, and 6 GiB for text inference. Adding the required 2 GiB headroom produces a 14-16 GiB envelope before transient peaks: the upper bound does not fit even a true 16-GiB host, and advertised decimal 16 GB is only about 14.9 GiB. Therefore shared placement is admissible only at the measured lower envelope with the headroom gate satisfied; otherwise use separate placement or reduce concurrency and remeasure. Actual service baseline and advertised memory units must be established first. Four OCR workers plus an eight-thread model would compete for at least twelve CPU execution lanes, before the web/DB. Do not add a model to the current four-worker pool unchanged.

Two MSKs-3 machines cost 4746 RUB/month, equal to one MSKs-4, but split RAM cannot be pooled into one model and isolation does not establish throughput. MSKs-4 service plus MSKs-3 model totals 7119 RUB; two MSKs-4 total 9492 RUB. Separation is operational isolation, not an established cost/performance win. Use private transport and keep original documents local unless the selected visual task needs them. NVMe capacity is unlikely to be the model bottleneck; source retention and raster caches need their own storage budget and cleanup.

For a serial model stage, a 21-file batch has a lower bound near 21 times average inference time, plus reader service time and queue effects. If mean model work is 20 seconds, inference alone adds 420 seconds; this is an illustration, not a prediction. Decide using whole-batch makespan and last-card latency, not tokens/second or fit in RAM.

## Structured result and source contract

Add a document-level extraction result alongside existing cards before migrating any operational schema. It should contain page coverage, participants, mentions, roles and typed arrays of fields/addresses. Keep separate stable IDs for a document occurrence, a participant hypothesis and each mention. Do not merge people by FIO alone or an organization substring. One person can be both applicant and debtor; roles can be proposed, current or historical, with effective date/event evidence.

Each value needs `raw_quote`, `normalized_value`, validation outcome, `document_sha256`, page/region and token IDs. It also needs `owner_evidence` linking that mention to the participant and `role_evidence` linking the participant to the asserted role/event. A quote containing an INN alone proves neither ownership nor role. Company legal form, full/short names and a nested referenced organization must remain distinct. Address arrays retain postcode, region, street, building, corpus, apartment/unit and an unknown type when not explicit. Preserve partial values and conflicting candidates without completing them from a directory or model memory.

Model output should reference supplied immutable token IDs; the host verifies that the quote/span exists and matches the selected source. For vision-only values, map the proposed region to local OCR tokens or a cropped region and verify exact transcription. If there is no reliable word match, show a region-level source and an explicit alignment warning; never manufacture a precise word arrow. Propagate crop, split, deskew and rotation transforms back to original coordinates.

For two-page photos, keep original image page 1 plus explicit left/right logical regions; a crop is not an independently supplied original page. For PDFs retain true page numbers. Reuse the current source-box and viewer transform system, extending scanned/hybrid PDF geometry explicitly. Draw separate evidence for value and relationship when needed. Employee edits remain distinct from the source transcription; clearing/mutating a field must not inherit an unrelated exact-match pointer.

Coverage states should distinguish processed, unreadable, skipped/limit-exceeded and not yet processed; value states should distinguish found, ambiguous, invalid, not found and explicitly absent. `not_found` can be upgraded to a scoped absence statement only with sufficient inspected coverage and an explicit evidential basis. A model refusal, timeout, truncated context or output limit makes the result incomplete. Source text is untrusted data: it cannot authorize tool calls or external operations. All checksum/date/schema checks and all card filling remain application functions; no model gets SRO credentials, Save or Send.

## Minimal next experiment and acceptance

No model comparison has been run. The remaining decisive experiment is a pinned 4B comparison across the same private corpus, then a CPU staging run. It needs approximately 3.42 GB of model/projector downloads plus a pinned llama.cpp build; no paid machine is needed for the first Mac quality comparison. A later CPU trial requires owner-provided access to an isolated 8-vCPU/16-GiB host or equivalent staging capacity, not an assumption based on the Mac.

1. Have an office reviewer independently annotate the 21 photos plus the seven-page PDF, with raw values, ownership, roles/events, full address components/type, absence/coverage and source regions. Group 3584/3603 and photo/PDF versions of the same act together. Freeze labels before model output review. Add at least one genuine scan PDF, a hybrid PDF and another multi-address participant example, since samples alone do not cover these paths.
2. Run the current pipeline, A with 4B on canonical OCR text, B with the same 4B on split page images plus required source alignment, and C with direct PDF text / OCR / bounded visual recovery. Same schema, image budget, context coverage and validation rules; include every retry/crop/alignment pass in timing. Run at least three repetitions, record cold load separately from warm service, and retain failed/incomplete outputs.
3. Compare participant-value precision/recall, wrong-owner and wrong-role counts, apartment/full-address exactness, invented values/type, missing pages, validated-source accuracy, and employee correction time. Report warning-bearing and silently wrong suggestions separately. Do not score JSON validity or populated-field count as extraction success.
4. Proposed bounded pilot gate: zero silent wrong-owner/role assignments, zero invented identifiers/apartments/address types, 100% of displayed source links verified, explicit incomplete coverage for every unread page. Every available apartment must be exact or visibly withheld. Require at least 95% precision on nonempty participant-value relations, no lower recall than baseline and at least 30% lower measured employee correction time. A small corpus passing does not prove production rates; expand the holdout before automatic use.
5. On CPU staging, measure upload acknowledgement to persisted editable-card readiness and batch last-card time for 21/50 mixed files with concurrent ordinary web/DB use. Include OCR/model queue waits, prefill/decode, bounded retries, provenance, transactions and failures. Measure summed worker/cgroup peaks, CPU, swap I/O, web latency and queue recovery. Suggested initial targets to validate with office needs: 21-file batch within five minutes, no OOM or sustained swap, at least 2 GiB available memory headroom on 16 GiB, no more than 20% web p95 latency regression. These are proposed acceptance budgets, not measured capacity or established office SLA.
6. Only if 4B fails ownership with correct text, compare 9B on that frozen subset; only if visual C/B beats A after alignment and recovery costs, reduce raster OCR. Keep the first integration shadow-only, feature-flagged and reversible. Preserve existing parser output and employee values. Production promotion and external workflows remain separate owner-authorized work.

The architecture recommendation is sufficiently supported now. Exact model accuracy, acceptable CPU throughput, number of remaining OCR passes and final tariff capacity require this one controlled comparison; they cannot be inferred from model benchmark marketing or the current OCR timings.

Independent Sol High review confirmed the measured numbers and generic-address typing finding. It identified the shared-host upper memory envelope as exceeding the headroom budget; the tariff recommendation now explicitly conditions admission on measured baseline and available headroom. No remaining blocking finding was reported for the shadow-only recommendation. This is source/evidence review, not independent human gold annotation or deployment acceptance.
