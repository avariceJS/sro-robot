# Post-Patch Validation

All supplied checks passed. Human review is still required.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: complete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

All required checks produced usable evidence.

## Evidence

Finding: PG-URL-TARGET-001: Production URL validation can be bypassed by libpq hostaddr, service-file, URL query identity overrides, or ambient target environment values.

Base commit: `6f60d17a10278f90d2d7afbf31e4e7e87451083a`

Patch SHA-256: `f7a167208c95bc5eae701c5c17b57bcb824be41139b1b4a25162f21067bcb45c`

Submodules: none

Forwarded environment: none

The table records whether each run matched its expected result. For exploits and
variants, a matched baseline means the safety assertion failed as expected.
Observations require the controls and comparisons described in the findings and gaps.

| Check | Kind | Base expectation | Patched expectation | Output comparison |
|---|---|---|---|---|
| `behavior-valid-remote-tls` | behavior | matched | matched | same |
| `control-valid-configuration` | control | matched | matched | not compared |
| `exploit-localhost-hostaddr-query` | exploit | matched | matched | not compared |
| `regression-settings-suite` | regression | matched | matched | not compared |
| `security-debug-libpq-environment` | security | matched | matched | not compared |
| `suite-settings-tests-with-patch` | suite | not run | matched | not compared |
| `variant-database-query-override` | variant | matched | matched | not compared |
| `variant-environment-hostaddr` | variant | matched | matched | not compared |
| `variant-service-file-parameters` | variant | matched | matched | not compared |
| `variant-user-query-override` | variant | matched | matched | not compared |

## Next steps

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
