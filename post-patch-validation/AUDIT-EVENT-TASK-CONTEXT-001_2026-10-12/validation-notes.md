# Validation notes

## Exact-tree verifier first attempt

The first `bash scripts/dev/verify.sh` run passed 505 discovered unit tests, then stopped in the disposable runtime-role smoke. Its direct SQL `recognized` append omitted `task_id` and was correctly rejected by the new guard. Full-log SHA-256: `228b1e92a1ffb5ec64c052c86aa75c0e4e4b459f6b63a506cecf083946922633`. The smoke now creates a synthetic user/document/task and uses that task for its append-only boundary probe.

## Exact-tree default verifier retry

`bash scripts/dev/verify.sh` passed 505 discovered tests and all 87 disposable PostgreSQL cases, including the all-event task-context matrix, fresh/populated migrations, rollback/retry after the new function replacement, runtime-role checks, restore/adoption/downgrade, 10,000 task/event rows, 10,001 storage files, Redis recovery and both 1,000-task thread/process claim diagnostics with zero unclaimed. Full-log SHA-256: `545bc3cae14e1f056c16555c8ab26e88a25102e57c195e5687a8fc831a1ab730`.

## Exact-tree expanded synthetic qualification

`SRO_DRILL_TASKS=100000 SRO_LEASE_LOAD_TASKS=5000 SRO_LEASE_LOAD_WORKERS=20 bash scripts/dev/verify.sh` passed the same 505 discovered tests and all 87 disposable PostgreSQL cases. Restore/read-back matched 100,000 synthetic tasks/events, 100,001 files and identities with zero mismatches; thread claims read back 5,000/5,000 at 536.6 claims/s (p50 36.4 ms, p95 46.8 ms), and process claims read back 5,000/5,000 at 1,016.0 claims/s (p50 13.7 ms, p95 21.4 ms), with zero unclaimed. These are synthetic disposable diagnostics, not office SLAs. Full-log SHA-256: `932fb12e28c3ce12fb5b87e8fd4bd674b4e9254c9e60b1c91b3128f102340af0`.

The separate disposable runtime-role smoke passed after the task-bound append fixture was added; full-log SHA-256: `30594a24d9238061eb7d9a8f31fd5dd8d12c9239a89e3d45275641f4851111c1`.

## Runtime PPV

Baseline `69a49c2` accepted taskless `recognized` and valid-payload `ocr_claimed` events; patch `60284f3` rejected each with SQLSTATE `23514`. A task-linked `recognized`, a task-linked actorless `approval_invalidated`, and a taskless global `session_login_failed` remained accepted. Focused regressions and the full deterministic unit suite passed. Assessment complete with no supplied-check findings/gaps; human review required. Result SHA-256: `373789af374e7f61eae754e46602b1d91ff63c42f25843d0af7dad1f43e85f7e`; report SHA-256: `2e43be009b0832d82aec96f2cc889d21992f2a2aacb8f789cc235ef5904eb5e1`.

The safe pruner dry-run listed 26 completed raw streams totaling 32,407 bytes. After review, `--apply` pruned them and refreshed the artifact manifest. No shared database, queue, worker, account or SRO record was used.

## Standalone smoke invocation attempts

Three initial manual invocations were not valid migration-smoke executions: the first omitted `PYTHONPATH` (SHA-256 `7059aa5bb799a2cc42ccf86816a7383b2bb358768a2cbf51173b3fb22260fe87`); the second omitted `APP_DEBUG=1` and therefore failed settings validation (SHA-256 `839d81b825a6f2dbf99d458ef1381f843b0dcd567076716ba49604f24bf68120`); the third used a placeholder port without a disposable PostgreSQL server (SHA-256 `99c2ca0a24a5cecc7f5f55a1c036e54d7350fedf044596821e02cb223997f3d0`). These did not exercise migration behavior. The exact-tree verifier supplied the disposable database correctly and passed late-migration rollback and retry. Raw logs are kept outside Git until this disposition is recorded, then removed.
