# Post-Patch Validation

Validation is incomplete. No supported patch failure was established.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: incomplete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

- `behavior-static-cache`: The baseline check failed; a new failure cannot be attributed to the patch.
- `control-asgi`: The baseline check failed; a new failure cannot be attributed to the patch.
- `control-asgi`: The patched harness control failed.
- `exploit-csp`: base: marker_missing; the safety assertion was not reached.
- `exploit-csp`: patched: marker_missing; the safety assertion was not reached.
- `exploit-dynamic-cache`: base: marker_missing; the safety assertion was not reached.
- `exploit-dynamic-cache`: patched: marker_missing; the safety assertion was not reached.
- `regression-security-tests`: The baseline check failed; a new failure cannot be attributed to the patch.
- `security-frame-override`: The baseline check failed; a new failure cannot be attributed to the patch.
- `suite-full-verify`: The suite also fails on baseline; attribution to the patch is unresolved.
- `variant-cross-origin-rejection`: base: marker_missing; the safety assertion was not reached.
- `variant-cross-origin-rejection`: patched: marker_missing; the safety assertion was not reached.

## Evidence

Finding: HTTP-SECURITY-HEADERS-001: Dynamic responses containing legal-office task data lacked explicit private no-store caching, and browser responses lacked a restrictive CSP; add response cache policy and script/source restrictions while preserving static asset caching and current HTMX behavior.

Base commit: `4b170d489ef0618a3a40c6f3486ee055dff1aed0`

Patch SHA-256: `a09da0c4d5ce7e7371b267397545a06e75ff1607e7d526846a04e8c1a4749d9f`

Submodules: none

Forwarded environment: none

The table records whether each run matched its expected result. For exploits and
variants, a matched baseline means the safety assertion failed as expected.
Observations require the controls and comparisons described in the findings and gaps.

| Check | Kind | Base expectation | Patched expectation | Output comparison |
|---|---|---|---|---|
| `behavior-static-cache` | behavior | did not match | did not match | same |
| `control-asgi` | control | did not match | did not match | not compared |
| `exploit-csp` | exploit | incomplete | incomplete | not compared |
| `exploit-dynamic-cache` | exploit | incomplete | incomplete | not compared |
| `regression-security-tests` | regression | did not match | did not match | not compared |
| `security-frame-override` | security | did not match | did not match | not compared |
| `suite-full-verify` | suite | did not match | did not match | not compared |
| `variant-cross-origin-rejection` | variant | incomplete | incomplete | not compared |

## Next steps

Return findings to the patch author and investigate validation gaps.
After revising the patch or checks, pin the new inputs and save a fresh run.

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
