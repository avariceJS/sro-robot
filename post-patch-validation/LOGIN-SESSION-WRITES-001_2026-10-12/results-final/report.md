# Post-Patch Validation

All supplied checks passed. Human review is still required.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: complete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

All required checks produced usable evidence.

## Evidence

Finding: LOGIN-SESSION-WRITES-001: The runtime database role can bypass the authentication service by inserting a live login_session for an inactive account, rewriting session identity/expiry, or clearing revoked_at and replaying an old bearer token.

Base commit: `7e4b279d8b1755847aa445dad43715f2f4fd13f2`

Patch SHA-256: `3b2f8b06de4db866120be6487355b2686abcce2b2e68706a7a0e7e4de89516e8`

Submodules: none

Forwarded environment: none

The table records whether each run matched its expected result. For exploits and
variants, a matched baseline means the safety assertion failed as expected.
Observations require the controls and comparisons described in the findings and gaps.

| Check | Kind | Base expectation | Patched expectation | Output comparison |
|---|---|---|---|---|
| `active-session-behavior` | behavior | matched | matched | same |
| `active-session-control` | control | matched | matched | not compared |
| `direct-inactive-insert` | exploit | matched | matched | not compared |
| `direct-revocation-reset` | variant | matched | matched | not compared |
| `login-schema-unit-regression` | regression | matched | matched | not compared |
| `patched-full-unit-suite` | suite | not run | matched | not compared |
| `unknown-user-reference` | security | matched | matched | not compared |

## Next steps

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
