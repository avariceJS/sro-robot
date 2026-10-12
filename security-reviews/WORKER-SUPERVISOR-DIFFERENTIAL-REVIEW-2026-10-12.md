# Worker Supervisor Differential Security Review

## Executive Summary

| Severity | Count |
| --- | ---: |
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 0 |

**Overall risk:** Low
**Recommendation:** Approve
**Reviewed code:** `scripts/dev/supervise_worker.py` and `tests/test_worker_supervisor.py`
**Commit context:** `819c704..782123d`, with the restart-cleanup regression added to the working tree after that range.

The change makes worker shutdown and restart account for the complete isolated POSIX process group after its leader exits. The baseline returned as soon as the leader exited, even if a child process remained and could overlap the replacement worker. Review found no process-group escape, command-argument disclosure, or unrelated-group targeting in the normal lifecycle. The first review pass identified that restart cleanup lacked a direct orphan regression; a new synthetic test now verifies the replacement process sees the old grandchild stopped.

## What Changed

| File | Change | Risk | Direct callers/references |
| --- | --- | --- | ---: |
| `scripts/dev/supervise_worker.py` | Probe the isolated process group, send TERM, wait for group exit, escalate to KILL, and clean descendants before clean return or restart. | Medium | 14 textual references across implementation and tests |
| `tests/test_worker_supervisor.py` | Add clean-exit and failed-restart orphan regressions. | Low | Six supervisor tests total |

The runtime entry `run_supervised()` is called by the script's `main()` entry point. `_stop_process_group()` is reached from shutdown, normal child exit, and final cleanup; `_signal_process_group()` is used for initial TERM and KILL escalation. `start_new_session=True` gives each launched worker a process group whose ID is the child's PID.

## Findings

No unresolved security findings.

### Verified safety behavior

Before this change, `_signal_process_group()` stopped signaling once `process.poll()` observed that the leader had exited, and `_stop_process_group()` returned immediately in the same condition. A worker that launched a same-group child and then exited could therefore leave that child running while the wrapper reported clean exit or started another worker.

The updated cleanup targets only the process group identified by the `Popen` PID. It sends `SIGTERM`, waits up to the configured grace interval while the group exists, and sends `SIGKILL` if members remain. A clean-exit regression uses a grandchild that ignores `SIGTERM` and checks it no longer runs. A separate failure/restart regression requires the replacement worker to observe the previous grandchild stopped before it reports readiness.

The implementation does not signal a process group after `killpg(pgid, 0)` reports it absent. A process group that still has descendants retains its group ID, so those descendants remain addressable after the leader exits. The two probes and subsequent signal are separate POSIX operations; as with process-group signaling generally, the operating system does not provide an atomic “signal this previously observed group instance” primitive. The isolated `start_new_session` group and short cleanup path constrain that residual identifier-reuse race.

## Test Coverage Analysis

All six worker-supervisor tests passed:

- repeated nonzero exits restart within the configured budget, then a clean exit stops the wrapper;
- restart-budget exhaustion returns nonzero without another start;
- clean leader exit reaps a same-group grandchild that ignores TERM;
- a failed worker's descendant is stopped before its replacement starts;
- TERM reaches a child and permits graceful exit;
- TERM escalates when the leader ignores it.

The full verifier passed 441 discovered tests (62 PostgreSQL-only cases skipped during discovery) and then all 62 disposable PostgreSQL cases. It also passed queue/process recovery, restore/adoption, 10,000 synthetic task/event pairs, 10,001 file checks, and both 1,000-task thread/process claims with zero unclaimed tasks. Full-log SHA-256: `6ae7048ea88bae565d39fa416e6c07b8f7f26d288079a3bb1beb1e68040c20af`.

The tests exercise POSIX subprocess groups on the development host. They do not establish the behavior of a production service manager, host reboot recovery, a supervisor killed with `SIGKILL`, or a child that deliberately escapes its session with `setsid()`.

## Historical Context

`git blame` attributes the pre-change supervisor to `591adc74` (2026-10-11). The early return on leader exit implemented direct-child cleanup only. The new cleanup extends that lifecycle contract; it does not remove authentication, authorization, command validation, or an external-call guard. No security fix or validation was reverted.

## Recommendations

### Before production

- Keep the external service manager as the host-level supervisor and set its own restart-rate and kill-mode policy; this local wrapper is not deployment evidence.
- Verify the production service manager terminates the full worker cgroup/process tree and records restart exhaustion in its incident channel.

### Technical boundary

- A child that deliberately creates a new session leaves this wrapper's process group. Do not run commands that daemonize or detach under this wrapper; use service-manager/cgroup containment for deployments that require stronger process-tree control.

## Analysis Methodology

**Strategy:** Surgical (large repository; one production file and one focused test file).
**Scope:** Baseline and changed implementations, all changed lines, `git blame`, direct call/reference count, supervisor tests, and the full repository verifier.
**Adversarial scenario:** A worker leader exits while a same-group grandchild ignores TERM and keeps running; review verified TERM/KILL cleanup before clean return and before restart.
**Coverage:** All changed runtime paths have focused regression coverage; the direct restart-specific orphan case was added after the first review pass and passed.
**Confidence:** High for the local POSIX wrapper behavior; low for untested deployment-specific service-manager behavior.
