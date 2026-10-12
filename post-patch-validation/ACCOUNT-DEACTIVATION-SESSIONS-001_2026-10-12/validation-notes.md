# Validation run disposition

Pinned baseline: `b5128938659d778b17615a4142631557a3162c25`. Pinned patch: `cdd5b32731f814f0bbde138562cf7945ed76050e`. The final runtime assessment is `results-final-2/report.md` and `result.json`; PPV result SHA-256: `60b9965a5a270a951136be1541befa517f0a262e8f4e38acc92e0a231952df4a`.

The final assessment completed every required check without findings or gaps and still requires human review. The old-session replay exploit reproduced on baseline and passed on patch. A distinct inactive-account session-issuance variant also failed on baseline and passed on patch. Control, exact login-normalization behavior, logout revocation, privacy-safe failed-login audit and patched unit suite passed. The permanent PostgreSQL suite and 100k/5k verifier are separately recorded in `memory-bank/40-operations.md`.

Two earlier local harness attempts remain preserved because they were incomplete:

- `results-final/result.json`, SHA-256 `05c090dc1daf7cd46117d26e21156ff1ebf3eefe53b5d219f57c5b3053d642a8`: argv generation resolved the `.venv/bin/python` symlink to the system interpreter, which lacked SQLAlchemy. No finding was supported. The plan now retains the venv symlink path.
- `results-retry/result.json`, SHA-256 `c3eb50c4eeb9885ebf30d110cfa0eed7128ba8543869cb049de2a6418716109e`: the baseline exploit was reproduced, but the fake SQLAlchemy session rejected the patch's `login_session` update-table object identity. The helper now checks the stable table name, and the full fresh run passed.

The successful final run's 26 raw streams were reviewed and pruned using `scripts/dev/prune-validation-logs.py` after a zero-finding/gap dry-run. Its report, result, pinned plan, patch, helper and refreshed artifact manifest remain. The two incomplete runs and their raw output remain untouched.

Before the PostgreSQL row-lock concurrency assertion was added, the same implementation passed the default 10k verifier (488 tests, 75 PostgreSQL cases; full-log SHA-256 `2adae59d20cf45d4d5949d51c261e05ec8f57857af103a155747a17f5eeaa0be`). After that assertion, the default 10k verifier passed on the exact tested logic (489 tests, 76 PostgreSQL cases; full-log SHA-256 `7ce457af5c90ae1d41464f048d66a0d9d9fae4b4b62d329e9eed589e37291156`). Both runs were superseded by the final cdd5b32 code snapshot and exact 100k/5k verifier documented in `40-operations.md`; each successful raw stream was retained until this disposition and the final stronger run were recorded, then pruned.
