# Post-patch validation artifact retention

Post-patch validation records are evidence, not a transcript archive. Keep the plan, check/helper sources, patch or source revision identity, `result.json`, human-readable report, and an integrity manifest for each retained run. Keep at most one canonical successful run per validation revision unless a later run answers a distinct question. Record why a superseded or incomplete run was replaced before removing it.

Raw `.stdout` and `.stderr` files are local diagnostics. Keep them while a run is active or while a failed/incomplete run is being diagnosed. For a complete run with no findings or evidence gaps, invoke:

```sh
python scripts/dev/prune-validation-logs.py --dry-run post-patch-validation/CASE_DATE/results
python scripts/dev/prune-validation-logs.py --apply post-patch-validation/CASE_DATE/results
```

The helper refuses incomplete or non-clean assessments, validates each recorded output hash, writes a compact `pruned-outputs.json` containing the removed paths, sizes, and hashes, and refreshes `artifact-manifest.json`. `result.json` keeps the execution statuses and stdout/stderr hashes needed to compare a future rerun. Preserve output for failed or incomplete attempts until the reason is recorded and resolved; then remove only the superseded run directory after checking that the canonical run and required helper/plan sources remain.

Do not store real personal data, credentials, browser profiles, or full source documents in validation artifacts. Use synthetic fixtures. Do not stage all of `post-patch-validation/` by directory; stage only a reviewed compact evidence set when it is useful to keep in Git.

## Cleanup record — 2026-10-11

The repository audit found 525 untracked files: 514 under eight validation IDs, seven new sample images, and four KAD source/test files. The sample images and KAD files were retained untouched. The seven complete, gap-free runs had 198 raw stream files (35,308 bytes); all hashes were verified before those streams were removed. The resulting reports, result JSON, plans, patches, helper sources, and refreshed manifests remain the canonical evidence.

Canonical successful runs retained:

- `HTTP-ERROR-LOG-PATH-001_2026-10-11/results-r4` (latest complete run; same clean assessment and check set as `results-r3`).
- `MIGRATION-ROLE-SPLIT-001_2026-10-11/results-rerun` (complete rerun; `results/` was incomplete because its broad suite failed on baseline and two checks could not reach expected markers).
- `PG-TLS-CA-ENV-001_2026-10-11/results` (only complete run).
- `PG-URL-TARGET-001_2026-10-11/results-rerun` (complete rerun; the earlier attempt had execution errors, and `results-r2` is superseded by this run with the same clean check assessment).
- `REDIS-QUERY-HOST-001_2026-10-11/results-rerun` (complete rerun; the first run had missing markers and a baseline suite failure).
- `TASK-FAILURE-FENCE-001_2026-10-11/results-rerun` (complete rerun; the first broad suite failed on baseline due to the isolated-worktree environment and owner extraction overlay).
- `UVICORN-ACCESS-LOG-001_2026-10-11/results` (only complete run).

The superseded result directories just described contained 250 files (606,126 bytes). They were removed after preserving their assessment and disposition here. The plan, helper/check sources, and finding-level patch remain at each validation ID root. The retained result records keep each removed stream's SHA-256 and execution status; `pruned-outputs.json` keeps a compact removed-file inventory. In total, 448 untracked files (641,434 bytes) were removed. The resulting status contains 86 untracked files: compact PPV evidence and policy files plus the untouched sample/KAD additions. This cleanup does not delete tracked historical validation evidence.
