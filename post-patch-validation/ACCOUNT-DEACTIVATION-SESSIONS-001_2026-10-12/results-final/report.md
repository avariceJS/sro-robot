# Post-Patch Validation

Validation is incomplete. No supported patch failure was established.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: incomplete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

- `active-session-control`: The baseline check failed; a new failure cannot be attributed to the patch.
- `active-session-control`: The patched harness control failed.
- `inactive-account-session-issuance`: base: marker_missing; the safety assertion was not reached.
- `inactive-account-session-issuance`: patched: marker_missing; the safety assertion was not reached.
- `login-normalization-behavior`: The baseline check failed; a new failure cannot be attributed to the patch.
- `logout-revocation-boundary`: The baseline check failed; a new failure cannot be attributed to the patch.
- `privacy-safe-failed-login`: The baseline check failed; a new failure cannot be attributed to the patch.
- `reactivation-session-replay`: base: marker_missing; the safety assertion was not reached.
- `reactivation-session-replay`: patched: marker_missing; the safety assertion was not reached.
- `unit-suite`: The suite also fails on baseline; attribution to the patch is unresolved.

## Evidence

Finding: ACCOUNT-DEACTIVATION-SESSIONS-001: Account deactivation rejected a session only while the user stayed inactive; reactivation could restore an unrevoked browser session, and login issuance could race with deactivation.

Base commit: `b5128938659d778b17615a4142631557a3162c25`

Patch SHA-256: `cfd41306844bb5d387bb0de9d03a9af836e36f2a52ae21fd8dd01585901b12ee`

Submodules: none

Forwarded environment: none

The table records whether each run matched its expected result. For exploits and
variants, a matched baseline means the safety assertion failed as expected.
Observations require the controls and comparisons described in the findings and gaps.

| Check | Kind | Base expectation | Patched expectation | Output comparison |
|---|---|---|---|---|
| `active-session-control` | control | did not match | did not match | not compared |
| `inactive-account-session-issuance` | variant | incomplete | incomplete | not compared |
| `login-normalization-behavior` | behavior | did not match | did not match | same |
| `logout-revocation-boundary` | security | did not match | did not match | not compared |
| `privacy-safe-failed-login` | regression | did not match | did not match | not compared |
| `reactivation-session-replay` | exploit | incomplete | incomplete | not compared |
| `unit-suite` | suite | did not match | did not match | not compared |

## Next steps

Return findings to the patch author and investigate validation gaps.
After revising the patch or checks, pin the new inputs and save a fresh run.

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
