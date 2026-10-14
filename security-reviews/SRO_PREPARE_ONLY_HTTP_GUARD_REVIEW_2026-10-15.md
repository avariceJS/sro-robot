# SRO Robot Differential Security Review — Prepare-Only HTTP Guard

## Executive summary

| Severity | Remaining findings |
|---|---:|
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 0 |

**Overall risk for the reviewed patch:** Low after the guard correction.
**Recommendation for this patch:** Conditional approval for the candidate branch after normal human review.
**Recommendation for promotion of the full branch to production:** Do not promote from this review alone.

The review found that the prepare-only SRO browser guard forwarded unsafe methods and
cross-origin POST requests. A patch now permits only safe methods plus the explicit same-origin
login, blank-card Add, and Close postbacks. Save and other unsafe operations are blocked. The
baseline and patch were compared with synthetic request objects; no real SRO session or record
was used.

## Scope and change summary

**Compared release context:** `origin/main` at `c17562c` to candidate commit `dcfda96` (before
the uncommitted guard patch).

The candidate is 295 commits and 505 changed files ahead of `origin/main`, with 89,593 insertions
and 1,526 deletions. This is a large change set. The strategy was surgical: prioritize HTTP
authentication and session controls, PostgreSQL privilege/migration boundaries, SRO external
mutation safeguards, release activation and rollback, Nginx/systemd configuration, and backup
handling. The review did not inspect every changed file or every changed function.

The security change validated in this report is limited to:

- `sro_robot/platform/client.py`
- `tests/test_sro_mutation_guard.py`

Pinned pre-patch commit: `dcfda96a470cb8ff51c6c5908551ba6b025b9998`.

## Finding addressed by the patch

### Prepare-only requests were not restricted to the authorized workflow

**Severity before correction:** Medium, with potentially high impact if an unsafe postback reached
an authenticated external platform.

**Location:** `sro_robot/platform/client.py:107-135`.

Before the patch, `_guard_prepare_only_request` continued every non-POST request and every
cross-origin request before evaluating the allowlist. Therefore a same-origin `DELETE`,
`PUT`, or `PATCH` was forwarded, and a cross-origin `POST` bypassed the SRO postback allowlist.
The login check also accepted any parameter containing `loginbutton`, even when
`__EVENTTARGET` named a different WebForms action.

**Attacker model and impact:** This guard runs in a browser context after authentication. A
compromised page, unexpected platform script, or alternate endpoint could cause an unsafe request
to be sent despite `prepare_only=True`. The validation proves the guard forwarded the synthetic
requests on the baseline; it does not claim that a particular real SRO endpoint accepts DELETE or
cross-origin mutations. No such endpoint was contacted.

**Patch:** The guard now allows only `GET`, `HEAD`, and `OPTIONS` without a postback check. Other
methods must be same-origin `POST`s matching the exact login, blank-card Add, or Close conditions.
Duplicate `__EVENTTARGET` values are rejected, and a login-button field cannot override a
different event target. Save and other events remain blocked.

## Validation evidence

The pinned post-patch validation is under
`post-patch-validation/PREPARE-ONLY-HTTP-GUARD-001_2026-10-15/`.

- Runtime PPV assessment: complete, no findings or gaps, human review still required.
- Same-origin DELETE exploit: baseline safety assertion failed as expected; patched assertion
  passed.
- Cross-origin POST variant: baseline forwarded it; patched guard blocked it.
- Conflicting login-button/event-target variant: baseline accepted it; patched guard blocked it.
- Allowed behavior comparison: ordinary GET, external asset GET, login, Add, and Close produced
  byte-identical output on baseline and patch.
- Existing Save denial passed on both revisions.
- `tests.test_sro_mutation_guard` passed on the patched revision. Unaffected SRO scenario/read-back
  regression tests passed on baseline and patch with synthetic local configuration.
- The full `scripts/dev/verify.sh` run completed successfully on the patched worktree; its final
  log line was `SRO Robot local verification passed`. The outer shell wrapper then raised an
  assertion during temporary-directory cleanup because macOS resolved `/tmp` as `/private/tmp`.
  This did not change the verifier's exit result. The captured log was hashed and the exact
  temporary directory was removed after inspection. Log SHA-256:
  `48c9f79781e4e4e240c0530307f0852b629b459e1286e228301b88d7def4e2bd`.

Two earlier PPV attempts were incomplete because the harness omitted a synthetic application
secret, then used SQLite with PostgreSQL-specific pool arguments. Their failures were validation
setup errors, not patch findings. The final plan used a synthetic PostgreSQL URL and made no
database connection. The failed-run outputs and this disposition were retained during review;
superseded raw streams were pruned only after the successful run.

## Other high-risk areas sampled

Read-only source review covered the login session lifecycle, login throttling and request limits,
production database role policy, migration role separation and advisory lock, application
readiness checks, systemd service privilege boundaries, release preflight/rollback behavior,
Nginx origin/proxy settings, backup permissions, and the SRO prepare-only flow. No additional
confirmed finding was established in the sampled paths. This is not a finding of exhaustive
coverage across the 505-file diff.

## Blast radius and test coverage

The guard is installed on every Playwright request from `SroClient` when `prepare_only=True`.
Consequently, its blast radius is all request methods and endpoints reached by the one-shot SRO
card preparation flow. The candidate contains direct tests in `tests/test_sro_mutation_guard.py`;
the focused suite and independent baseline-versus-patch harness both passed their stated checks.

## Production promotion limits

This report does not approve the full candidate branch for production promotion. The branch is
still hundreds of commits ahead of `origin/main`, and the successful release-tag deployment path
has not been exercised from an approved tag on `main`. The production publisher correctly rejects
candidate-only tags. Public HTTPS is also still unavailable because the CA cannot reach the
HTTP-01 challenge through the provider firewall. Those are release gates, not findings in this
patch.

## Methodology and confidence

**Strategy:** SURGICAL for a 505-file candidate diff.

**Analyzed deeply:** The prepare-only request guard and its direct synthetic test; sampled adjacent
authentication, data-role, deployment, and external-write paths.

**Techniques:** Baseline comparison, request-path adversarial modeling, tests/call-site review,
line-level source review, and runtime post-patch validation with an immutable base commit and
patch hash.

**Limitations:** No live SRO requests were made. The test harness validates decisions at the
Playwright route-guard boundary, not downstream SRO server behavior. Most of the 505 changed files
received no line-by-line review. No production release tag was tested.

**Confidence:** High for the stated prepare-only guard behavior under tested requests; low to
medium for overall security of the entire candidate diff.
