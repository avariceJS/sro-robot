# Branch consolidation inventory (2026-10-16)

## Completed consolidation outcome

The integrated release was committed as `2827cd232ff6cc4287a6b880c2d310e21756e073`,
fast-forwarded to GitHub main, tagged `sro-v1.0.0` and successfully deployed with
the checked-in script. Public HTTPS `/version` matched main exactly; all readiness
checks passed. `develop` was created strictly from that main in the canonical
checkout. Subsequent checkpoint documentation is not a production code change.
After a temporary owner-requested pause, cleanup was completed on resumption.
Only `main` and `develop` remain as local and GitHub branches; only the canonical
Mac worktree remains. Seven historical local branches, four GitHub branches and
three extra worktrees were removed after archive and ancestry/content checks.
The legacy VPS Git metadata and source were moved to private recovery storage;
active runtime/configuration and all rollback releases remain in place.

The final source-parity check hashed all 1,725 tracked main files on the deployed
release using Git blob hashing: zero mismatches. The active infrastructure Compose
file also matches main. Production readiness and all application/infra units
passed again after cleanup. The inventory below describes original refs.

## Scope and original inventory

This inventory records the original local and remote branch tips before integration, their ancestry, and patch-equivalence review. It did not inspect `.env`, browser profiles, private storage, real documents, or secret contents. The original tips are retained under `refs/archive/consolidation-20261016/`; the private ref manifest is at `/Users/root1/.local/share/sro-robot-recovery/20261016/original-refs.json`.

Integration merged the cloud branch into the single-auth branch, then promoted
the verified merge to main by fast-forward. Original main was
`c17562c87b2fe9a8569ed7e4ea394a9d80991968`; the pre-release VPS ran
`cdb80f974934a0a8d81dbded6b27acc22b886c8f`. Neither was the full latest product.

## Original branch and remote tips

| Ref | Original full tip | Relation to main |
| --- | --- | --- |
| `main`, `origin/main` | `c17562c87b2fe9a8569ed7e4ea394a9d80991968` | Baseline; no `develop` ref was present. |
| `stage-1/foundation`, `origin/stage-1/foundation` | `777a531a8b910fe96cede65bb98806315f88e2b0` | Main ancestor plus 146 commits; fully contained in cloud. |
| `demo/safe-office-demo` | `e1bc8ba57d838ec3aa23783b5cc9d830e0397fd2` | Main plus 176 commits; contained in nightly and cloud. |
| `origin/demo/safe-office-demo` | `8db1e78791cce531689a142cb6b3420835f2ac35` | One commit beyond local demo; that checkpoint is also contained in cloud. |
| `codex/nightly-100pdf-20261014` | `03cfe2b76072f513ebc3640b085d5c21799e95b5` | Main plus 269 commits; contains demo, and is contained in cloud. Its configured upstream was `origin/demo/safe-office-demo` (ahead 92), not a nightly remote. |
| `codex/cloud-office-20261014`, `origin/codex/cloud-office-20261014` | `2a1ad6d29fccb7cbcfc6035c8f9ea94fc609d984` | Main plus 312 commits; contains stage 1, demo including its remote checkpoint, and nightly. Best integration base. |
| `codex/sro-single-auth`, `origin/codex/sro-single-auth` | `3743db4ed637a71e289d30afa5433d9429dde2e3` | Main plus 298 commits; diverges from cloud at `cdb80f974934a0a8d81dbded6b27acc22b886c8f`. After that base, cloud has 22 commits and auth has 8. |
| `codex/safe-access-log` (local only) | `41b337fc8b7ddd0962eb6960d8bb85520405189b` | Older divergent history; common base with safe-owner is `b7a51df95c9f9020b6dfcb7b66e5875bb7db2e31`. |
| `codex/safe-owner-integration` (local only) | `e72de212a2204aaed71fe013b9d62aacf3795690` | Ancestor of safe-access; 15 commits precede safe-access's final checkpoint. |

At inventory time, root/demo was clean and one commit behind `origin/demo`; cloud and single-auth were clean at their upstream tips; nightly was clean and ahead 92 of its misconfigured demo upstream. The subsequent integrated tree was committed and released; there is no remaining integration worktree. Cloud is the newest primary line: it carries the product/KAD helper, nightly batch work, and cloud release/TLS changes. Its history records ingress and DNS renewal blockers, so branch ancestry alone does not qualify a release.

## Auth work requiring preservation

All eight commits unique to the original auth tip relative to cloud were patch-unique. They add internal debtor/assignment drafts (`a8ab22e`), the SRO-backed employee-login candidate (`77efe06`), two schema migrations (`d6a3e18b0421`, `ea912b30c761`), tests and review material, and evidence checkpoints (`2a2eeec`, `b9a6ae7`, `c20e85b`, `139b702`, `234bf1b`, `3743db4`). These features and evidence were retained in the reviewed, tested and deployed integration. The original tip remains in private archive refs.

The `client.py` changes have separated base hunks: cloud tightens the prepare-only POST allowlist, while auth adds per-session browser state, lifecycle cleanup, and session validation. The combined behavior was reviewed and its guard/session tests executed. The current state is recorded in `.now.md`.

## Legacy local-only branches and dispositions

Safe-access and safe-owner share base `b7a51df95c9f9020b6dfcb7b66e5875bb7db2e31`; safe-owner is an ancestor of safe-access. Against cloud, 264 of safe-access's 267 commits and 249 of safe-owner's 252 commits have patch-equivalent counterparts. The same three patch-unique commits occur on both:

- `2a30cd8d5e5990401fd78e08fbd2ee60fd2b9dc3` (`Mark document recognition failures on tasks`) stores raw exception text. Current `app/workers/jobs.py` persists only generic error type metadata, and `tests/test_worker_error_safety.py` verifies sensitive exception text is neither stored nor logged. Do not reapply the legacy patch.
- `5fc992028f4f2e6f0e3b69d5826ad9629fdbcc44` combines a direct `create_kad_task` path with an unleased-worker guard. The direct constructor is superseded by authenticated, idempotent browser-PDF intake in `app/api/routes_kad_import.py` and `app/services/kad_import.py`. Duplicate-worker fencing is present in `app/services/tasks.py` and `app/workers/jobs.py`, with unit and concurrency coverage. Do not cherry-pick the old bundle.
- `bf245a2d3077898874cfe0398157ae841f482ebb` modernizes documentation and the agent handoff. Current `AGENTS.md` and the memory-bank index, handoff, and newer operational notes supersede it; current runner documentation also records the stricter local-only write boundary. No cherry-pick is needed.

The later safe-access checkpoint also contains the append-only audit migration and reviewed rollback evidence; the migration and retained report are present in the cloud tree. Both active branches were removed after this review; archive refs and verified private bundles retain their original history.

## Unreferenced history qualification

A connectivity check also found old stash objects, commits and annotated tags
without active refs. They were retained under private local archive refs; no
object pruning or history rewriting was performed.

- Owner WIP stash `23851f29f6092eb5000586e035cf21bb5690076a` changes 18 paths.
  Current main retains assignment-card inspection safety, previous-surname/address
  parsing fixes, task polling and atomic queued-worker claims. Its substantial
  direct server-side MKAD search/fetch workflow is intentionally retired in favor
  of the ordinary Firefox helper plus local PDF import. Birth-date extraction is
  outside the current reviewed card contract. No missing requested behavior was
  identified that warrants replaying the obsolete bundle.
- Evidence stash `54065b4ce4148cb510bf48ce32d64b5576bbd44a` has third parent
  `55e4a5b6a1adcef4b6b7a6da41c2bdaa83b0fe0c`. All 75 untracked evidence blobs
  match same-path blobs in main exactly (0 missing, 0 different). Aggregate
  SHA-256 over sorted path/object-ID/relationship records:
  `a7821e35b5fe4976c9bb8afd7cfaba5dad0c7122bf676343f0876ae688a1e12`.
- Unreferenced audit guard `57e988d997f055de9230a469ec3a00c32e1cad22` is not
  an ancestor or exact patch-ID match. Its behavior is implemented and extended
  by main's `e4944216fae7e8b135bb5f0d8b8eaa98971b406d`: UPDATE/DELETE/TRUNCATE
  denial, exact schema drift checks, role/restore drills and later event-contract
  guards. The revised audit review from `d75fc502...` is retained in current docs.
- `02d916e...` changes only historical preflight notes in `.now.md`;
  `93c7a15c...` (target of the unreferenced annotated tags) is already an ancestor
  of main. No historical code replay is needed.

## Recovery points and cleanup evidence

Private Mac root: `/Users/root1/.local/share/sro-robot-recovery/20261016` (0700).
It contains the original/final ref manifests, verified 0600 Git bundles, a verified
pack retaining 443 otherwise unreferenced objects, and operational evidence hashes.
Original 13 refs plus final deleted tips and the unreferenced commits/tags remain
under `refs/archive/consolidation-20261016/`. These are recovery refs, not branches;
none was pushed.

Before deleting the three extra worktrees, ten private runtime/artifact paths
were moved to `worktree-runtime/`. Its private manifest verifies 7,951 entries
including file hashes, modes and symlink-target hashes. It retains the nightly
`.env`/venv, private browser/output/storage directories, Hypothesis state and
unique nightly validation output. Generated bytecode caches were disposable.
The canonical `.env`, venv and runtime data remain in their original folder.

GitHub deletions were atomic and conditional on the exact archived remote tips.
The nightly branch's stale demo upstream was removed before normal branch deletion;
only the two superseded divergent safe branches needed explicit named deletion
after content review and recovery verification. No reset/clean/force-push occurred.

VPS private archive: `/var/backups/sro-robot/legacy-checkout-20261016` (0700).
It retains the original Git directory (connectivity verified), all 1,618 tracked
source files with fingerprints, old bytecode caches and the inactive/disabled
legacy OCR unit. Exactly 1,617 legacy source files were removed from the active
root; `deploy/docker-compose.dev.yml` remains as live infrastructure configuration.
Current and previous release imports were verified independent of the removed
root source. No active development checkout or Git branches remain on VPS.

Protected VPS paths remain: `.env`, `.venv` (old rollback runtime), `current`,
`releases`, `venvs`, `browsers`, `storage` and the active Compose configuration.
The release PostgreSQL restore-proven checkpoint remains under
`/var/backups/sro-robot/release-2827cd232ff6cc4287a6b880c2d310e21756e073/`.
Daily backup ran successfully again after cleanup.
`daily-20261016T094652Z` was copied to the Mac recovery destination and its manifest
verified. The installed hourly backup-pull agent uses a standalone script outside
the deleted worktrees and matches the released script.

## Functions qualified on the published release

Real authorized owner login, linked external-session reuse, logout/access denial,
Secure/HttpOnly/SameSite cookie, two-file upload (text PDF plus scan), server OCR
to review, internal person card and editable/resumable assignment with two
revisions all passed through HTTPS. The guarded SRO blank person form was filled,
read back and closed without Save; RQ confirmed successful completion and no
external record URL. Only one real account was available; multiple-user isolation
is qualified synthetically, not with two real employees.

The synthetic surname was not automatically extracted as expected and required
normal employee correction. This release does not claim general entity extraction
accuracy, automatic legal-entity/IP cards, or external record persistence. The
historical queued preview request remains preserved under a non-RQ held key;
its old writing task has not been retried or treated as externally successful.

## Verification and release operation

The release exact-tree gate passed 630 Python tests (88 initially skipped, then
all 88 executed against disposable PostgreSQL), 134 Node tests, runtime-role policy,
fresh/additive migration, failed migration, restore, recovery and concurrency
checks. The synthetic 10,000-task/20-staff restore retained row/sequence/document
identity; it is not a production throughput SLA. Six mocked-service tests exercise
actual activation shell failure paths; an isolated real PostgreSQL checkpoint and
rollback drill separately verified exact rows/sequences. No destructive rollback
was deliberately exercised against live production.

Main/deployed commit is the sole production source. Develop carries documentation
checkpoints beyond main and is the active canonical working branch. No feature
development was started after consolidation. Follow `docs/RELEASE_WORKFLOW.md`.
GitHub Actions remains blocked by account billing/spending limits; local scripted
release is tested and requires no manual file transfer. The production environment
accepts only main and has an environment-scoped deploy key. Branch-protection API
returned 403; server-enforced branch protection is not claimed.
