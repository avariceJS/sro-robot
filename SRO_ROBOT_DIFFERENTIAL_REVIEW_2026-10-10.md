# SRO Robot Differential Security Review — 2026-10-10

## Executive summary

| Severity | New findings in reviewed route diff |
| --- | ---: |
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 0 |

**Overall risk for the changed authentication boundary:** Low for an authentication bypass; the new route uses the same authenticated-user dependency as the task router. **Recommendation:** Conditional. This review does not qualify authorization for multi-user use because all authenticated users can still access the shared task pool. That pre-existing high-impact policy gap remains an explicit Stage 1 blocker.

## Scope and baseline

- Baseline: committed `HEAD` `ab98187`.
- Worktree changes reviewed: `app/api/routes_tasks.py`, `app/services/tasks.py`, `app/workers/jobs.py`, `app/workers/queues.py`, and `tests/test_worker_recognition.py`, with focus on the new `/case-search` task-creation route and its authentication/trust boundary.
- Baseline context: task routes already required `current_user`; the baseline did not contain `/case-search` or KAD task creation. `git blame HEAD -- app/api/routes_tasks.py` shows existing task-route authentication predates this diff. No authentication guard was removed by the reviewed change.
- Blast radius: the router currently exposes 11 task routes. All 11 inject `current_user`, including the new `/case-search` route; the same shared-pool visibility limitation applies across the task list, task detail, document screenshot and mutation routes.

## Findings

### Existing High — Authenticated users are not authorized by task ownership or role

**Affected code:** `app/api/routes_tasks.py` task list and `_require_task` consumers (currently lines 38–39, 25–29, 98–100, 119–121, 136–140, 167–171, 214–216).

Any active account can enumerate the recent shared task pool, open another task by ID, and invoke its edit, approve, local-run, decision, screenshot and status routes. `current_user` authenticates a user but `_require_task` looks up only by task ID. `Task.assignee_id` is populated but is not used to authorize access. This behavior predates the KAD route and is not a bypass introduced by the reviewed diff; it prevents Stage 1 M2/M3/M4 authorization claims for a multi-staff environment.

**Attack scenario:** an ordinary authenticated employee requests `/tasks/<known-or-enumerated-id>` and then `/tasks/<id>/shot/0` or submits a mutation. The handler resolves the task without comparing its assignee or a role/tenant policy, so another employee's document-derived data or screenshot may be exposed or changed.

**Required resolution:** the owner must define personal versus team visibility, roles and permitted cross-user actions. Enforcing `assignee_id == current_user.id` now would guess business policy and could prevent required collaboration; the policy remains unresolved and no such rule was added.

## Reviewed change and tests

The new `/case-search` route requires `Depends(current_user)`. The app also has same-origin checks for cookie-authenticated mutations; direct ASGI tests cover the middleware, but no end-to-end route test was present for this new endpoint. A regression test was added at `tests/test_route_auth_contract.py` that enumerates the task router and requires every route's dependency graph to include `current_user`. It passes for all 11 routes.

Worker/KAD logic was only inspected where relevant to the route's trust boundary; this is not a full review of KAD candidate selection, worker leases, acquisition semantics, or the rest of the owner-pending diff. The documented first/latest act selection risk remains outside this review and must not be interpreted as safe.

## Verification and limitations

- `tests.test_route_auth_contract`: 1 focused test passed, then the full verification passed with 103 unit tests and 9 disposable PostgreSQL tests.
- `git diff --check`: passed in the full verification run.
- Full local verification passed from a fresh Python 3.13 virtual environment. The new route-auth regression was included in this full run.
- No SROArbitr writes, real document contents, or real identifiers were used.
- This is a focused review of authentication coverage, not a complete security audit, penetration test, deployment review, or proof of task authorization.

## Recommendation

Keep this route-authentication contract test. Before claiming multi-user authorization maturity, define and implement owner-approved role/ownership policy, add cross-user allow/deny tests for every read and mutation path, and review worker actions against the same policy. Do not infer that a valid session grants access to every task as an accepted product rule.
