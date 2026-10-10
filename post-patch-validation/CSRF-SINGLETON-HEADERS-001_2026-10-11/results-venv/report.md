# Post-Patch Validation

Validation is incomplete. No supported patch failure was established.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: incomplete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

- `comma_host_security`: The baseline check failed; a new failure cannot be attributed to the patch.
- `host_duplicate_variant`: base: marker_missing; the safety assertion was not reached.
- `host_duplicate_variant`: patched: marker_missing; the safety assertion was not reached.
- `origin_duplicate_exploit`: base: marker_missing; the safety assertion was not reached.
- `origin_duplicate_exploit`: patched: marker_missing; the safety assertion was not reached.
- `referer_fallback_behavior`: The baseline check failed; a new failure cannot be attributed to the patch.
- `same_origin_control`: The baseline check failed; a new failure cannot be attributed to the patch.
- `same_origin_control`: The patched harness control failed.
- `security_suite`: The suite also fails on baseline; attribution to the patch is unresolved.
- `targeted_regression`: The baseline check failed; a new failure cannot be attributed to the patch.

## Evidence

Finding: CSRF-SINGLETON-HEADERS-001: Same-origin mutation guard accepted duplicate Host, Origin, or Referer fields based on whichever duplicate was retained by dictionary construction.

Base commit: `694c64c83c5476bb8803181716483d259f6638ec`

Patch SHA-256: `227bbe28d2d845364863c9c056b0aa906605eca4c7806abc47e3e5e952a0d4c5`

Submodules: none

Forwarded environment: none

The table records whether each run matched its expected result. For exploits and
variants, a matched baseline means the safety assertion failed as expected.
Observations require the controls and comparisons described in the findings and gaps.

| Check | Kind | Base expectation | Patched expectation | Output comparison |
|---|---|---|---|---|
| `comma_host_security` | security | did not match | did not match | not compared |
| `host_duplicate_variant` | variant | incomplete | incomplete | not compared |
| `origin_duplicate_exploit` | exploit | incomplete | incomplete | not compared |
| `referer_fallback_behavior` | behavior | did not match | did not match | same |
| `same_origin_control` | control | did not match | did not match | not compared |
| `security_suite` | suite | did not match | did not match | not compared |
| `targeted_regression` | regression | did not match | did not match | not compared |

## Next steps

Return findings to the patch author and investigate validation gaps.
After revising the patch or checks, pin the new inputs and save a fresh run.

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
