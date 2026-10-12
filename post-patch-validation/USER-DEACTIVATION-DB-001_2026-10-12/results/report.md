# Post-Patch Validation

Validation is incomplete. No supported patch failure was established.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: incomplete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

- `login-normalization-behavior`: The baseline check failed; a new failure cannot be attributed to the patch.

## Evidence

Finding: USER-DEACTIVATION-DB-001: Direct SQL account deactivation could leave a live browser session usable after later account reactivation; enforce revocation at the PostgreSQL boundary.

Base commit: `276a743cebe041e2703b63a0ba1604c8de77a55b`

Patch SHA-256: `8ea76f5806d373bdfbb64704a9bc0fae8f288f03741c96d97207ae5480555cfd`

Submodules: none

Forwarded environment: none

The table records whether each run matched its expected result. For exploits and
variants, a matched baseline means the safety assertion failed as expected.
Observations require the controls and comparisons described in the findings and gaps.

| Check | Kind | Base expectation | Patched expectation | Output comparison |
|---|---|---|---|---|
| `active-account-security-control` | security | matched | matched | not compared |
| `authentication-unit-suite` | suite | not run | matched | not compared |
| `direct-sql-deactivation` | exploit | matched | matched | not compared |
| `login-normalization-behavior` | behavior | did not match | did not match | same |
| `reactivation-session-replay` | variant | matched | matched | not compared |
| `supported-deactivation-behavior` | regression | matched | matched | not compared |
| `unrelated-update-control` | control | matched | matched | not compared |

## Next steps

Return findings to the patch author and investigate validation gaps.
After revising the patch or checks, pin the new inputs and save a fresh run.

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
