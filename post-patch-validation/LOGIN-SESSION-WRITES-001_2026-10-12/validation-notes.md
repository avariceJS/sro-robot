# Validation notes

## Run 1 (`results/`)

The pinned runtime checks reproduced the inactive-account insert and revocation-reset variants on the baseline and passed them on the patch. The full verifier failed on both sides because the helper passed its disposable fake `MIGRATION_DATABASE_URL` into later verifier subprocesses that changed `DATABASE_URL`; the resulting same-database configuration check failed before the remaining suite completed. Result SHA-256: `995138bfb1286f9493abf0961d261b5e3f4f6f13dc9c0878845334907dc49e84`. This is harness evidence, not a patch finding. Raw streams remain preserved.

## Run 2 (`results-retry/`)

The same full-verifier check still failed on both sides at a later schema-check command because the verifier's own subprocess environment uses a distinct disposable URL while inheriting the helper-level setting. No patch-attributed failure was reported; targeted exploit, variant, controls and unit tests matched. Result SHA-256: `14b79874a3756496f452304e8fc6fbad95f56d36582d814a717191ed3dd2d1b2`. Raw streams remain preserved. To avoid weakening the environment isolation or editing project runtime configuration just for this harness, the pinned PPV suite category was changed to the full deterministic unittest suite. The exact-tree disposable verifier had already passed independently at the pinned patch commit.

## Final run (`results-final/`)

Runtime PPV pinned baseline `7e4b279d8b1755847aa445dad43715f2f4fd13f2` and patch `f8844b02113d1de4c3b195558e79926e01ff77a7`. The direct inactive-account insert exploit and revocation-reset variant both reached their assertion markers, failed on baseline and passed on patch. Active-session control, byte-identical supported issue/revoke behavior, unknown-user rejection, the auth-session regression suite on both revisions, and the complete 497-test unit suite on patch passed. Assessment is `complete`, with no supplied-check findings or gaps; human review is required. Result SHA-256: `e1812e0a59a4d02ebee4cc6d3ff2a721d46626a5a4efe8c687252ef35aa2a0f9`; report SHA-256: `a8101498f25acc508508a3a7c9ceb41be8d7321ca60478a212e49a841f80a880`. The safe pruner dry-run reported 26 streams / 26,638 bytes and a complete assessment; after review, `--apply` removed only those raw streams and refreshed the artifact manifest. The two incomplete result trees are intentionally retained unchanged.

The exact-tree full `scripts/dev/verify.sh` run at the pinned patch passed separately: 497 discovered tests, all 82 disposable PostgreSQL cases, runtime-role, migration rollback/retry, restore/adoption/rolling-worker/downgrade, Redis recovery, 10,000 task/event rows, 10,001 file/reference checks and 1,000 thread/process claims with zero unclaimed work. Full-log SHA-256: `32b98ad05490589b7a05fe852f9f92bb5fcf34c4922f3f684fbe8bf5b6308488`.

No shared database, queue, worker, account, browser profile or SRO record was used. The PPV does not claim exhaustive coverage or production qualification.
