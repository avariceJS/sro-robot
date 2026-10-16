# Branch consolidation inventory (2026-10-16)

## Scope and outcome

This inventory records the original local and remote branch tips before integration, their ancestry, and patch-equivalence review. It did not inspect `.env`, browser profiles, private storage, real documents, or secret contents. The original tips are retained under `refs/archive/consolidation-20261016/`; the private ref manifest is at `/Users/root1/.local/share/sro-robot-recovery/20261016/original-refs.json`.

The integration in progress is an uncommitted merge of `codex/cloud-office-20261014` into `codex/sro-single-auth`. The auth branch still points at its original tip `3743db4ed637a71e289d30afa5433d9429dde2e3`; its worktree contains staged integration changes. Release, branch cleanup, and deployment remain pending. No deployment result is asserted here.

The original `main` and `origin/main` tip was `c17562c87b2fe9a8569ed7e4ea394a9d80991968`. The last observed VPS code revision was `cdb80f974934a0a8d81dbded6b27acc22b886c8f`; it is the common ancestor of the cloud and single-auth tips, not evidence of the VPS's current release state.

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

At inventory time, root/demo was clean and one commit behind `origin/demo`; cloud and single-auth were clean at their upstream tips; nightly was clean and ahead 92 of its misconfigured demo upstream. The integration worktree now has staged, uncommitted changes. Cloud is the newest primary line: it carries the product/KAD helper, nightly batch work, and cloud release/TLS changes. Its history records ingress and DNS renewal blockers, so branch ancestry alone does not qualify a release.

## Auth work requiring preservation

All eight commits unique to the original auth tip relative to cloud were patch-unique. They add internal debtor/assignment drafts (`a8ab22e`), the SRO-backed employee-login candidate (`77efe06`), two schema migrations (`d6a3e18b0421`, `ea912b30c761`), tests and review material, and evidence checkpoints (`2a2eeec`, `b9a6ae7`, `c20e85b`, `139b702`, `234bf1b`, `3743db4`). The feature and evidence are included in the auth-side integration work and require review before release; do not drop the original auth tip while the integration is uncommitted.

The `client.py` changes have separated base hunks: cloud tightens the prepare-only POST allowlist, while auth adds per-session browser state, lifecycle cleanup, and session validation. A content conflict is unlikely; review the combined external-session and mutation-guard behavior together. `memory-bank/.now.md` is the other shared changed path and needs a factual reconciliation.

## Legacy local-only branches and dispositions

Safe-access and safe-owner share base `b7a51df95c9f9020b6dfcb7b66e5875bb7db2e31`; safe-owner is an ancestor of safe-access. Against cloud, 264 of safe-access's 267 commits and 249 of safe-owner's 252 commits have patch-equivalent counterparts. The same three patch-unique commits occur on both:

- `2a30cd8d5e5990401fd78e08fbd2ee60fd2b9dc3` (`Mark document recognition failures on tasks`) stores raw exception text. Current `app/workers/jobs.py` persists only generic error type metadata, and `tests/test_worker_error_safety.py` verifies sensitive exception text is neither stored nor logged. Do not reapply the legacy patch.
- `5fc992028f4f2e6f0e3b69d5826ad9629fdbcc44` combines a direct `create_kad_task` path with an unleased-worker guard. The direct constructor is superseded by authenticated, idempotent browser-PDF intake in `app/api/routes_kad_import.py` and `app/services/kad_import.py`. Duplicate-worker fencing is present in `app/services/tasks.py` and `app/workers/jobs.py`, with unit and concurrency coverage. Do not cherry-pick the old bundle.
- `bf245a2d3077898874cfe0398157ae841f482ebb` modernizes documentation and the agent handoff. Current `AGENTS.md` and the memory-bank index, handoff, and newer operational notes supersede it; current runner documentation also records the stricter local-only write boundary. No cherry-pick is needed.

The later safe-access checkpoint also contains the append-only audit migration and reviewed rollback evidence; the migration and retained report are present in the cloud tree. Once the cloud/auth integration and these retained files are reviewed, safe-owner is redundant and safe-access can be removed from active branches. Keep the archive refs until cleanup is explicitly complete.

## Archive and pending work

Original refs are preserved under `refs/archive/consolidation-20261016/` for main, stage-1, demo, nightly, cloud, auth, both safe branches, and the captured origin refs. The ref list is recorded in the private manifest path above; this document does not reproduce its contents.

Still pending: review the staged cloud-to-auth integration, reconcile the current-state documentation, validate the integrated candidate at its exact tree, complete the release gate, deploy only after that gate, then remove superseded active branches/worktrees while retaining the archive refs and private manifest. No service action or deployment is recorded by this inventory.
