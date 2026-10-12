# Validation notes

## Exact-tree verifier run 1

The verifier reached populated-schema downgrade protection and stopped because `scripts/dev/postgres-restore-drill.sh` did not yet list the new migration's intentional forward-only refusal message. The code's refusal was correct; the drill's accepted-reason list was stale. Full-log SHA-256: `2cf5edeffc07e0a9b9788ee0ecec35b628b6db9adc706877b2ac15a25f9dbcbc`.

## Exact-tree verifier retry

The restore drill now accepts the new migration's forward-only downgrade refusal and continues to verify preserved rows, revision and guard objects. `bash scripts/dev/verify.sh` passed 502 discovered tests and all 85 disposable PostgreSQL cases, migration rollback/retry and runtime-role checks, populated restore/adoption, 10,000 task/event rows, 10,001 file/reference integrity checks, Redis recovery and both 1,000-task thread/process claims with zero unclaimed. Full-log SHA-256: `723bf34c6a6e751ddc71e55a2c939433a2bc96b6162cd58831a4f99d725ca503`.

## Runtime PPV

Baseline `619260a` accepted an empty `approved` event payload and all 14 required event kinds with empty payloads. Patch `71d5df8` rejected all of them with SQLSTATE `23514`; valid approval serialization and the existing payload allowlist behavior remained unchanged. Focused regression tests passed on both revisions, and the complete deterministic unit suite passed on patch. Assessment complete, with no supplied-check findings or gaps; human review required. Result SHA-256: `a1aeeae6f79fcc0c92b36c35927d372d8f86247345b1ff96aac18327ff2d3640`; report SHA-256: `d3784e1fece0c10d992de352a21e1a01f645f0af55e9bd95b7034ae979e437c2`.

The safe pruner dry-run listed 26 streams totaling 29,640 bytes for the complete PPV run. After review, `--apply` removed those completed raw streams and refreshed the manifest. No shared database, queue, worker, account or SRO record was used. This evidence is synthetic and does not establish exhaustive coverage or production qualification.

The failed and successful verifier raw logs were kept outside Git at `/tmp` until both SHA-256 values and the harness disposition were recorded above, then removed. The reproducible command remains `bash scripts/dev/verify.sh`.
