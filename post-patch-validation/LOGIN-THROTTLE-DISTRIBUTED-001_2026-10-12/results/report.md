# Post-Patch Validation

Validation is incomplete. No supported patch failure was established.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: incomplete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

- `unit-suite`: The suite also fails on baseline; attribution to the patch is unresolved.

## Evidence

Finding: LOGIN-THROTTLE-DISTRIBUTED-001: The production login attempt limiter was process-local, so each restarted or parallel web worker admitted a fresh set of credential checks and the bounded local identity cache could not provide shared admission state.

Base commit: `68692d07c1b16845eb6ae7bf60de57511496c88e`

Patch SHA-256: `ba77e1d86dfd24f04a55c237b34c17cb4783fa17d6d74121c96660f5d145580f`

Submodules: none

Forwarded environment: none

The table records whether each run matched its expected result. For exploits and
variants, a matched baseline means the safety assertion failed as expected.
Observations require the controls and comparisons described in the findings and gaps.

| Check | Kind | Base expectation | Patched expectation | Output comparison |
|---|---|---|---|---|
| `adjacent-login-reservation` | security | matched | matched | not compared |
| `control-local-admission` | control | matched | matched | not compared |
| `distributed-process-bypass` | exploit | matched | matched | not compared |
| `identity-bucket-cap` | variant | matched | matched | not compared |
| `local-limiter-behavior` | behavior | matched | matched | same |
| `safe-login-audit` | regression | matched | matched | not compared |
| `unit-suite` | suite | did not match | did not match | not compared |

## Next steps

Return findings to the patch author and investigate validation gaps.
After revising the patch or checks, pin the new inputs and save a fresh run.

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
