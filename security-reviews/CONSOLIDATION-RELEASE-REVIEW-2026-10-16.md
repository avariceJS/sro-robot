# Consolidation release safety review — 2026-10-16

## Executive summary

**Initial recommendation: reject the first activation draft. The follow-up below records the fixes and qualification boundary.** This is a focused, static differential review of the release path in the uncommitted `codex/sro-single-auth` integration worktree at `3743db4ed637a71e289d30afa5433d9429dde2e3`. It is not a VPS execution or a restore qualification.

| Severity | Count | Finding |
| --- | ---: | --- |
| High | 2 | Incomplete queue/outbox preflight; maintenance removed before the last fallible activation step |
| Medium | 2 | Rollback loses preview service; Nginx change is not rolled back with activation |
| Release gate | 1 | One historical preview job remains queued on the last recorded VPS state |

The proposed release touches all application users, PostgreSQL public tables and sequences, Redis queues, document storage, public ingress and three application units. The migration is forward only; the new rollback path uses a root-only PostgreSQL checkpoint and a destructive `pg_restore --clean --create`, so a successful isolated restore and a quiescent production state are essential.

## Scope and baseline

Reviewed `scripts/ops/activate_vps_release.sh`, `scripts/ops/deploy_vps.sh`, the new `scripts/ops/release_database.py`, `deploy/cloud/nginx.conf.template`, web/preview/OCR/migration/backup/health systemd units, `tests/test_release_database.py`, and their direct callers and health/queue dependencies. Compared committed release scripts at `cdb80f974934a0a8d81dbded6b27acc22b886c8f` and the cloud Nginx template at `2a1ad6` with the current worktree. The old guarded release had no database rollback and deliberately had no preview worker; the new path adds a quiesced checkpoint and unconditional preview service start. The existing public site is already live over HTTPS according to `memory-bank/.now.md` (2026-10-15), so this review does not treat Nginx installation as first-time publication.

No source, secret, queue, database, SROArbitr record or VPS was changed in this review. The root agent owns functional verification and deployment. No tests were run here.

## Findings

### High — preflight ignores outstanding outbox and the status command's failure

**Evidence:** `scripts/ops/activate_vps_release.sh:78-84`; `app/cli.py:1155-1170`.

`ops-status` returns nonzero when any outbox row remains pending, including policy-withheld historical jobs. Activation suppresses that result with `|| true`, then accepts a few grepped queue and registry lines. It never checks the printed `pending`, `eligible`, `leased`, `delayed` or `withheld` counts. A quiescent Redis queue can therefore pass while durable work is waiting in PostgreSQL. Once the new web process accepts traffic, its normal dispatch paths can revisit these intents under a new schema and code version. A pending or uncertain external job must remain withheld until read-only reconciliation.

**Concrete scenario:** An OCR intent is committed but has no Redis job because dispatch failed. `ops-status` reports pending work and exits 1; the queue depth is zero and the three regular expressions match. Activation migrates the database and opens the site. A subsequent replay or dispatch call enqueues the old intent under the new release without release-time reconciliation.

**Required change:** Treat the command's nonzero status as a gate; explicitly parse and require the safe outbox state (or a reviewed, enumerated withheld state), plus all relevant queue registries and task leases. Recheck after stopping ingress and writers, immediately before snapshot. Do not automatically clear or replay the historical preview/KAD job.

**Test gap:** `tests/test_deployment_units.py` checks for string fragments, and `tests/test_release_database.py` does not execute this preflight or model pending outbox. A state-machine/shell test should show an empty Redis queue with pending outbox is refused.

### High — maintenance is removed before activation has committed

**Evidence:** `scripts/ops/activate_vps_release.sh:100-105`, `:45-68`; `scripts/ops/release_database.py:140-148`.

The script removes `/run/sro-robot-maintenance` at line 102 and then starts the health timer at line 103. If that `systemctl start` fails, the `ERR` trap runs rollback with `switched=1`, but `release_database.py restore` requires the maintenance marker. It therefore refuses to restore the database, and rollback exits with the new pointer and database left in place, application services stopped, and ingress no longer protected by the marker. Nginx may return 502; more critically, the rollback outcome is neither the old release nor a guarded maintenance state.

**Concrete scenario:** The candidate schema migrates successfully, web/worker/health start, but the health timer unit cannot start. The trap stops web and workers. The restore helper rejects the missing marker before touching PostgreSQL. This is a deterministic control-flow failure, independent of the migration contents.

**Required change:** Keep ingress in maintenance until every operation that can trigger rollback has succeeded; make the rollback handler re-establish the marker before any stop/restore action. Only declare success after the final read-back and timer state are checked.

**Test gap:** No test executes the activation script with failure injection after line 102. The database unit tests only verify that a missing marker is rejected.

### Medium — rollback does not restore the preview service state

**Evidence:** `scripts/ops/activate_vps_release.sh:49-65`, `:89-101`; `deploy/cloud/systemd/sro-robot-preview.service:19`; `scripts/ops/check_vps_health.sh:4-12`; `app/main.py:299-313`.

Activation always stops preview and, on the success path, starts it. The rollback path stops it but restarts only web and OCR. If preview had been active before activation, a failure at snapshot, migration or health leaves it inactive while `ready` succeeds and maintenance is removed. Neither `/readyz` nor the periodic health check covers the preview queue/service. Future preview requests can therefore accumulate unnoticed. This is separate from the current production state, where preview is intentionally off and a historical queued job must remain withheld.

**Required change:** Capture the pre-release service state and restore exactly that state on rollback. Gate any first-time preview enablement on explicit reconciliation of the historical queued job; avoid enabling it merely because deployment succeeds. Include preview coverage in the release read-back if enabled.

**Test gap:** No rollback test starts with preview active or with an intentionally disabled preview and checks both outcomes.

### Medium — Nginx remains at candidate configuration after release failure

**Evidence:** `scripts/ops/deploy_vps.sh:74-85`.

The publisher copies the current Nginx vhost to `nginx-before-<commit>.conf`, overwrites and reloads the live vhost, and only then calls activation. The `nginx -t` failure branch restores the file, but an activation failure leaves the candidate configuration live. Because the existing site is already public, this is an incomplete rollback of ingress configuration, potentially including future proxy or access-policy differences from the old app.

**Required change:** Stage and validate the candidate vhost, then either activate it only at the committed release boundary or restore the saved vhost and reload Nginx on any downstream activation failure. Verify the live vhost hash and response after rollback.

**Test gap:** No test injects an activation failure after a successful Nginx reload and checks restored file content and active Nginx configuration.

## Release gate: historical preview job

The last recorded production state in `memory-bank/.now.md` has one old `sro_preview` job in `QUEUED`, one linked task in `writing`, and no preview worker. `activate_vps_release.sh:81` correctly requires `sro_preview=0`, so the current publisher should stop before maintenance. The job's outcome must be reconciled read-only and preserved; emptying or starting the queue simply to pass this gate is not authorized. The script's later unconditional preview start (`:100`) increases the consequence of an incorrect queue intervention.

## Database restore and retention notes

`release_database.py` hashes public-table rows and sequences, produces a custom-format dump, restores it into an isolated network-disabled container, and compares signatures before marking the checkpoint verified (`:55-137`). This is a useful safety gate. Its tests cover rejection paths and cleanup but do not exercise a successful real PostgreSQL snapshot/restore, ownership/ACL preservation, production role behavior, interrupted `pg_restore`, or the shell rollback that invokes it. `restore()` drops and recreates the application database (`:165-169`); a failed restore intentionally leaves maintenance in place, but an in-place partially restored database then needs operator recovery from retained artifacts. The normal daily backup separately captures Redis and documents, whereas this release rollback intentionally restores PostgreSQL only. Verify that no queue/file writer can run between checkpoint and a possible rollback, especially after workers start at activation line 100.

The snapshot directory is created once with `exist_ok=False` (`:119-120`). An interrupted attempt leaves a partial directory, and the publisher also refuses an already-created release tree (`deploy_vps.sh:48-49`). The recovery procedure must preserve that evidence and define a safe new release attempt; it must not remove the partial checkpoint or existing release blindly.

## Verification and blast radius

| Boundary | Direct caller / consumer | Reach |
| --- | --- | --- |
| `activate_vps_release.sh` | `deploy_vps.sh:84` | All users of the live site; web, OCR and preview units |
| `release_database.py snapshot/restore` | `activate_vps_release.sh:52,91` | All application PostgreSQL tables and sequences |
| Nginx template | `deploy_vps.sh:77-82` | Every public HTTP(S) request |
| Preview service | `activate_vps_release.sh:89,100` | Every queued preview task if enabled |

Static checks in `tests/test_deployment_units.py` cover presence/order of selected strings, not failure behavior. Five `tests/test_release_database.py` cases cover tamper rejection, missing root/maintenance, active writer rejection, source-change refusal and proof-container cleanup; none run the complete release or successful PostgreSQL restore. Full release qualification should use disposable PostgreSQL/Redis and service test doubles, retain failed raw streams, and compare source/target state before any real activation.

**Confidence:** high for the stated control-flow and preflight findings; medium for operational impact because the live VPS was not inspected during this review. No claim of exhaustive codebase coverage or production readiness.

## Follow-up disposition (release candidate)

The root reviewer re-read the combined release path after these findings. The
initial findings above describe the earlier draft, not remaining confirmed defects.

- Both preflight and the quiesced post-shutdown check require successful
  `ops-status` and empty queues. Nonzero status is no longer suppressed.
- Maintenance remains until all activation/timer operations pass; rollback first
  recreates it and refuses to reopen traffic after a database restore failure.
- Prior preview runtime and enabled states are captured and restored. The new
  health script requires preview; fallible enable/timer setup precedes activation.
- A failed activation restores the saved Nginx configuration only after successful
  old-release recovery. Failed database recovery keeps the maintenance-aware
  candidate proxy and closed ingress.
- The historical preview request was inspected read-only (queued, no start/end),
  backed up, then its queue list atomically moved to a non-RQ held key. The job
  object and private recovery manifest remain intact; no external action ran.

Qualification: six tests execute the real shell activation control flow with
mocked services/database commands, including migration, checkpoint, timer and
restore failures. This is not an actual systemd/database rollback drill. Separately
a real isolated PostgreSQL synthetic checkpoint/restore proved exact rows and
sequences after an intervening change, and the full repository Docker verifier
passed 10,000-row backup/restore and 88 PostgreSQL boundary/concurrency tests.
On VPS, the daily document archive was actually extracted and matched live hashes;
Redis RDB was actually loaded in a network-disabled container with the historical
queue intact. Production PostgreSQL restore proof remains an activation gate.

The canonical post-patch validation plan is retained under
`post-patch-validation/CONSOLIDATION-RELEASE-001_2026-10-16/`. Its declared evidence
level is **source**, so its complete assessment does not claim execution of a
real failed deployment. An initial incomplete harness used a newly introduced SSH
control as if it were unchanged; its logs are preserved privately and the corrected
control checks the existing dedicated identity invariant. All findings/gaps must
be read together with the operational evidence, not as exhaustive assurance.

Release is conditional on the exact-tree verifier, real checkpoint proof and
HTTPS commit/readiness read-back. No additional unresolved source defect was
confirmed in this focused review. Interrupted restore remains an explicit manual
recovery condition with ingress closed and artifacts preserved.
