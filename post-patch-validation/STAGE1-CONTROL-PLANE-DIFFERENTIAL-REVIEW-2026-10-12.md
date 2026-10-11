# SRO Robot Stage 1 Control-Plane Differential Security Review

**Review date:** 2026-10-12
**Baseline:** `0d59b2e` — PostgreSQL audit-event contract guard
**Published commits reviewed:** `7f8e898`, `af9af1a`
**Additional worktree changes reviewed:** auth-reservation cleanup after backend errors, exact readiness/schema-check function fingerprints, invoker-only trigger execution checks, and their regression tests
**Scope:** login admission and PostgreSQL runtime-guard readiness. No shared database, production service, or SRO account was accessed.

## Executive summary

| Severity | Open findings |
|---|---:|
| Critical | 0 |
| High | 0 |
| Medium | 1 |
| Low | 0 |

**Overall risk:** Medium
**Recommendation:** Conditional acceptance for the local Stage 1 foundation. The login throttle is intentionally process-local defense in depth; production deployment still needs an ingress or shared limiter and multi-worker qualification. This review does not qualify Stage 1 beyond M1 partial.

**Key metrics:** 12 changed files reviewed; all four changed production security/control-plane files reviewed with their direct callers; one login route consumes the limiter and one readiness route consumes the database-guard checker. No changed production helper has more than one direct production caller. No security validation was removed. Changed security behavior has unit, HTTP, and disposable PostgreSQL coverage. No coverage percentage was measured.

## What changed

The reviewed range starts at `0d59b2e` and includes published commits `7f8e898` and `af9af1a`, plus the local follow-up patch represented by the final verification below.

| File area | Risk | Direct production callers | Coverage |
|---|---|---:|---|
| `app/security.py` login limiter | High: admission and credential-check boundary | 1 route | Unit concurrency/window/privacy-state tests and HTTP integration |
| `app/api/routes_auth.py` login flow | High: authentication and session issuance | 1 HTTP route | HTTP 401/429/500 behavior, failed-login audit, successful flow suite |
| `app/db/schema.py` guard read-back | High: fail-closed integrity/readiness decision | 1 readiness handler | Disposable PostgreSQL normal, disabled-trigger, and changed-function checks |
| `app/main.py` `/readyz` | Medium: operational control | 1 HTTP route | Fake HTTP failures plus current migrated disposable database |
| Tests and memory-bank documentation | Low | N/A | Assertions and evidence note |

The changes are additive. Historical blame on the baseline login route showed authentication and failed-login auditing but no repeated-attempt admission guard; the patch adds a bounded layer before Argon2. Historical readiness already checked exact Alembic head and dependencies; the patch adds managed guard verification. No removed security code or regression pattern was found.

## Findings

### MEDIUM — Per-process login throttle can be bypassed across peers and workers

**Files:** `app/security.py:36-38, 62-85`; `app/api/routes_auth.py:34-56`
**Attacker:** Unauthenticated user able to submit login forms from more than one source peer, or able to churn distinct login names from one peer.
**Exploitability:** Medium.
**Test coverage:** Unit and HTTP behavior covered; multi-process/shared-ingress behavior is not covered by design.

The limiter counts failed checks plus in-flight reservations per direct ASGI peer and normalized login, stores keyed digests in a bounded 4,096-entry LRU, and allows ten failures in a 60-second rolling window. It does not trust forwarded headers. A caller can use changing source peers, or churn more than 4,096 distinct names to evict a target pair, then resume checks against that target. Separate application workers also maintain separate maps. This means the control reduces ordinary repeated attempts in one process but does not establish a global online-guessing limit or protect a multi-worker deployment from distributed Argon2/audit load.

This limitation is documented and does not create a persistent account lockout. Keep the guard as defense in depth. Before production, qualify trusted ingress identity and add an appropriately bounded shared edge control; do not infer a global guarantee from these local tests.

### Resolved during review — Backend failures no longer consume failed-login quota

The first limiter version recorded an attempt before authentication returned. A database outage could therefore consume the same quota as a bad password and temporarily return 429 after the database recovered. The reviewed worktree now reserves capacity atomically, marks the reservation failed only after `authenticate` returns no user, clears prior failures on successful authentication, and releases the reservation when authentication raises. An HTTP regression proves a synthetic backend error returns 500 and the next bad password still reaches authentication and returns 401. The full current-head verifier includes this correction.

## Adversarial analysis

**Login path:** An unauthenticated caller posts to `/login`. The route uses `request.client.host`, canonicalizes whitespace/case in the login identity, and reserves under a process-keyed HMAC digest before password verification. Once ten completed failed checks occupy the window, a further request receives 429 with `Retry-After` before Argon2 and audit persistence. A successful credential check clears recorded failures. A backend exception releases its pending reservation. The request body and concurrent checks remain separately bounded by the existing middleware. The concrete residual attack is the per-process/per-peer bypass described above.

**Readiness path:** A database owner or superuser changes or disables a managed trigger while Alembic still reports the expected revision. `/readyz` checks all seven required trigger identities, relation bindings, operation masks, enabled state, function bindings, and normalized definitions. It checks exact normalized source for fixed capture/append-only functions and SHA-256 fingerprints for the audit validator and task-transition validator. Drift produces `database_guards=unavailable`, HTTP 503, and a safe request-correlated diagnostic without returning catalog contents. Disposable PostgreSQL tests disable a trigger and replace both fingerprinted function bodies inside transactions, verify detection, and roll back. Database owners remain able to tamper with managed objects; the readiness test detects the tested drift but is not a tamper-proof audit system.

The subsequent review pass also covers execution privilege mode and diagnostic parity. Both readiness and `schema-check` require all five managed trigger functions to have `prosecdef=false`. A disposable test marks one function `SECURITY DEFINER`, verifies both diagnostics fail, and rolls the DDL back; an unchanged body does not hide the privilege-mode change. The CLI schema checker now uses the same exact status-transition source fingerprint as readiness instead of relying on selected fragments. A unit negative control changes a legal transition while leaving the rest of the migration body intact and confirms the CLI reports drift.

## Test coverage and validation

The final `bash scripts/dev/verify.sh` run passed:

- 413 discovered tests, with 56 PostgreSQL-only cases skipped during the first discovery run;
- all 56 disposable PostgreSQL cases, including readiness detection of disabled triggers and replaced function bodies;
- runtime-role migration/DML checks, populated-clone adoption and restore, 10,000 synthetic task/event identities, and integrity checks for 10,001 files;
- both thread and process probes claiming 1,000 synthetic tasks with zero unclaimed tasks;
- byte compilation, shell checks, queue-loss recovery, worker restart/read-back, and downgrade refusal/preservation.

Full log SHA-256: `7f0a2c14a25ab8616c37a9fda95e3924d24362c8d65fa5e60cb2211ce61df11e`. Raw output is retained outside Git at `/tmp/sro-stage1-exact-schema-diagnostics.Icm31j/verify.log`.

An earlier full run stopped on a stale readiness JSON assertion before recovery drills (SHA-256 `a4a1c347afd834282bc56e7b6ea4ba9504111b4f29930e166339b612fff98f3c`). The assertion was updated. An intermediate pass before exact task-transition function pinning had SHA-256 `6a31b79a5b8d6e9b2c84d3fb485ec606984c83f61708435d9aafd523690cf4ee`; it is superseded by the final run above. A complete run after function pinning but before the final invalid-check fail-closed branch tightening had SHA-256 `f06e3e00a7c4e317790574cacdf496fdef6f29bbfe7f0da3d96f6a86e7c0fb4f`. The first execution-mode run passed both test suites but stopped at a stale expected old-schema drift count (27 instead of 28); its hash is `fe01a1b2725ecc03ee9541d01f0a3fa4bd073efb56bc1b039c52a4ed54b56643`. The restore fixture was updated to assert the additional missing-runtime-function finding. The final exact-schema-diagnostics run above passed.

## Limitations and recommendation

This is a surgical review of the login-admission and runtime-readiness changes, not a repository-wide security audit. It did not test deployed proxy trust, process restarts during active authentication, multi-worker shared throttling, production network behavior, or an owner-level database incident. The project continues to require production ingress and workload qualification, independent access review, and deployed worker/backup recovery evidence. Do not promote M2–M4 based on this synthetic local verification. The code is suitable to keep in the local Stage 1 branch with the medium deployment limitation recorded above.
