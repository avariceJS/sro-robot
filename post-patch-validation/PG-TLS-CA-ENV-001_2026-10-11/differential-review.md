# PostgreSQL Connection Guard Differential Security Review

## Executive Summary

| Severity | Count |
|---|---:|
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low / coverage note | 0 |

**Overall risk:** Medium, because this configuration guard sits on the production database trust boundary; no supported exploit was found in the reviewed commits.

**Recommendation:** Conditional acceptance of the two reviewed commits. Keep deployment qualification blocked on actual CA provenance, server identity/TLS evidence, least-privilege credentials and production runtime review.

## What Changed

**Commit range:** `6f60d17..de537a1`

| File | Added | Removed | Risk | Blast radius |
|---|---:|---:|---|---|
| `app/settings.py` | 41 | 0 | High: production connection validation | One settings load per application process; config also feeds runtime/migration engines and local diagnostics |
| `tests/test_settings.py` | 75 | 0 | Low: regression evidence | Test suite only |

Commits:

- `180fd97` — Reject hidden libpq connection overrides in production
- `1965b5b` — Reject ambient PostgreSQL TLS root overrides
- `de537a1` — Test runtime and migration database paths independently

The PostgreSQL TLS-root patch SHA-256 (the exact patch validated against base `180fd97`) is `4e01842c63c5e07d6e4912203e43e4979b71ed19645bfe886e0b1ef426e18ded`.

## Findings

No supported exploitable failure was found in the reviewed commits.

No supported security failure or remaining test-coverage issue was found in the reviewed range. The initial review noted the empty database-path test changed both URLs together; commit `de537a1` replaced it with separate runtime-only and migration-only subcases. `tests.test_settings` passes with the independent assertions.

## Change and Trust-Boundary Analysis

Production `Settings.load()` parses runtime and migration SQLAlchemy URLs, validates each connection input, compares their PostgreSQL target and distinct usernames, then enforces transport settings before `app/db/base.py` constructs the engines. The new checks reject URL query overrides for `hostaddr`, service files, username, database name and PostgreSQL `options`, require database names in both URL paths, reject selected ambient libpq endpoint/identity overrides, and reject ambient `PGSSLROOTCERT`.

Concrete previously vulnerable scenarios were reproduced against the baseline and rejected by the patch:

1. A `localhost` URL plus query `hostaddr` routed libpq to a network address while the old transport check classified the URL host as local.
2. URL query `user`/`dbname`, service-file settings, and ambient `PGHOSTADDR` allowed the effective libpq connection inputs to differ from those compared by the application.
3. An ambient `PGSSLROOTCERT` was accepted alongside remote URLs with `sslmode=verify-full`; the patched loader rejects it for both runtime and migration configuration.

The setting loader does not open a PostgreSQL connection during these validations. The patch therefore cannot prove that a deployed CA file is trusted, that a server presents the expected certificate, or that TLS is actually negotiated. Explicit URL `sslrootcert` content and certificate provenance are not verified here.

## Test Coverage

- `tests.test_settings`: 28 tests passed after the latest patch.
- Baseline-versus-patch post-patch validation is saved in `results/`. The ambient trust-root baseline assertion failed as expected and the patch rejected the value in both runtime and migration URL scenarios; valid TLS and debug controls passed. Harness assessment: complete, no findings or gaps, human review required.
- `SRO_ROBOT_PYTHON=/tmp/sro-robot-hash-verify/bin/python SRO_DRILL_TASKS=100000 bash scripts/dev/verify.sh` passed at `1965b5b`: 263 tests discovered, 28 PostgreSQL integration tests passed in disposable drill, 100,000 synthetic task/event/document identities, 100,001 restored files, zero checked identity mismatches, and migration, worker, queue and role recovery checks passed.
- This is not a live PostgreSQL TLS handshake, a CA provisioning review, a production identity review, or a production deployment test.

## Blast Radius

`Settings.load()` is invoked once by `app/settings.py` module initialization in a normal process. The returned URLs feed the runtime and migration engines in `app/db/base.py`; CLI and worker utilities also import the same settings module. Production misconfiguration now fails closed during import/startup. Debug-mode development configuration remains unchanged and is covered by controls.

## Historical Context

The new lines add validation; no prior guard was removed. `git blame 6f60d17` shows the preceding URL transport/target validation was introduced in `19534c0` and the distinct runtime/migration role requirement in `68159e22`. The reviewed changes extend those checks after reproduction demonstrated that libpq accepts parameters and defaults outside the SQLAlchemy URL fields inspected by the existing guard.

## Recommendations

### Before production

- Verify the configured CA trust chain and database server identity against the actual deployment.
- Verify the runtime role, migration role, PostgreSQL ACLs, secret injection and network policy on the real target.
- Close the shared unversioned database adoption gate only after an independently verified backup, exact schema comparison and approved maintenance window.
- Resolve task visibility, assignment and administrative role policy before enforcing task-level ownership.
- Obtain hosted CI evidence for the current commit and owner-approved workload/concurrency/RPO/RTO targets.

### Test improvement

- Test runtime and migration database-path omissions separately.

## Analysis Methodology

**Strategy:** Focused review of the two changed files in a 200+ file repository; risk-first analysis of `app/settings.py` and its direct engine consumers.

**Techniques:** Commit range inspection, baseline `git blame`, caller search, before/after analysis, exploit-path review, exact-patch hash comparison, focused tests, full disposable verifier, and saved runtime baseline-versus-patch evidence.

**Limitations:** No production PostgreSQL endpoint, hosted Actions result, deployment environment or TLS certificate chain was available. Review did not inspect `.env` or real document data. The finding list applies only to commits `6f60d17..de537a1`; it is not a repository-wide security audit.

**Confidence:** High for the demonstrated configuration guard paths; medium overall because deployment behavior and CA trust are unobserved.
