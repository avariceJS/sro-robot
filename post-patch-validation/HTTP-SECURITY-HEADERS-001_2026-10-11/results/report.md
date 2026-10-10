# Post-Patch Validation

Validation is incomplete. No supported patch failure was established.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: incomplete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

- `behavior-static-cache`: base: execution_error: check behavior-static-cache cwd does not exist on base: {checkout}
- `behavior-static-cache`: patched: execution_error: check behavior-static-cache cwd does not exist on patched: {checkout}
- `control-asgi`: base: execution_error: check control-asgi cwd does not exist on base: {checkout}
- `control-asgi`: patched: execution_error: check control-asgi cwd does not exist on patched: {checkout}
- `exploit-csp`: base: execution_error: check exploit-csp cwd does not exist on base: {checkout}
- `exploit-csp`: patched: execution_error: check exploit-csp cwd does not exist on patched: {checkout}
- `exploit-dynamic-cache`: base: execution_error: check exploit-dynamic-cache cwd does not exist on base: {checkout}
- `exploit-dynamic-cache`: patched: execution_error: check exploit-dynamic-cache cwd does not exist on patched: {checkout}
- `regression-security-tests`: base: execution_error: check regression-security-tests cwd does not exist on base: {checkout}
- `regression-security-tests`: patched: execution_error: check regression-security-tests cwd does not exist on patched: {checkout}
- `security-frame-override`: base: execution_error: check security-frame-override cwd does not exist on base: {checkout}
- `security-frame-override`: patched: execution_error: check security-frame-override cwd does not exist on patched: {checkout}
- `suite-full-verify`: patched: execution_error: check suite-full-verify cwd does not exist on patched: {checkout}
- `variant-cross-origin-rejection`: base: execution_error: check variant-cross-origin-rejection cwd does not exist on base: {checkout}
- `variant-cross-origin-rejection`: patched: execution_error: check variant-cross-origin-rejection cwd does not exist on patched: {checkout}

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
| `behavior-static-cache` | behavior | incomplete | incomplete | not compared |
| `control-asgi` | control | incomplete | incomplete | not compared |
| `exploit-csp` | exploit | incomplete | incomplete | not compared |
| `exploit-dynamic-cache` | exploit | incomplete | incomplete | not compared |
| `regression-security-tests` | regression | incomplete | incomplete | not compared |
| `security-frame-override` | security | incomplete | incomplete | not compared |
| `suite-full-verify` | suite | not run | incomplete | not compared |
| `variant-cross-origin-rejection` | variant | incomplete | incomplete | not compared |

## Next steps

Return findings to the patch author and investigate validation gaps.
After revising the patch or checks, pin the new inputs and save a fresh run.

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
