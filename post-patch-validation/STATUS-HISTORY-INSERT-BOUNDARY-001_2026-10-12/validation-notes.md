# Validation notes

## First run (`results/`)

The runtime exploit, COPY variant, behavior, security and regression checks completed on both revisions, but the full-suite harness failed on baseline and patch because its disposable worktree lacked the project `.venv` path expected by restore-drill subprocesses. This was a harness environment issue, not a patch-attributed failure. The incomplete result and raw streams are retained for diagnosis.

## Retry (`results-retry/`)

The helper now passes the pinned interpreter through `SRO_ROBOT_PYTHON`. The full deterministic unit suite then passed on the patch. Baseline reproduced direct INSERT and COPY history forgery; patch rejected both with SQLSTATE `55000`. Supported status changes continued to produce one history row, append-only UPDATE/DELETE/TRUNCATE checks passed, and focused regression checks passed on both revisions. Assessment complete with no supplied-check findings or gaps; human review is required. Result SHA-256: `1a1e53355141215960fa24eeff9051d3f43c9c828a1bcea7abd01a51bdee0310`; report SHA-256: `7d51c5c85fa5fa81f909eee173c5f6a9aa79d8a035e2b5154b5fd146f7e04b30`.

The safe validation-log pruner dry-run found 26 streams totaling 32,155 bytes for the complete retry. After reviewing that output, `--apply` removed only the completed retry's raw streams and refreshed its artifact manifest. Raw streams from the incomplete first run remain preserved.

Exact-tree `bash scripts/dev/verify.sh` at patch commit `aecafa9` passed 500 discovered tests and all 84 disposable PostgreSQL cases, migration rollback/retry, runtime-role checks, populated adoption/restore/downgrade, Redis recovery, 10,000 task/event rows, 10,001 file/reference checks, and both 1,000-task thread/process claims with zero unclaimed. Full-log SHA-256: `b6955ca7116100546dae6201bb0e2ba3dcd351f44e5e14b2e7dfc75df74e563b`.

No shared database, queue, worker, account, browser profile or SRO record was used. The PPV does not establish exhaustive coverage or production qualification.
