# Post-Patch Validation

All supplied checks passed. Human review is still required.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: complete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

All required checks produced usable evidence.

## Evidence

Finding: MIGRATION-TARGET-DEBUG-001: Debug mode skipped runtime/migration database target validation, so init-db could run against a different database than the application runtime.

Base commit: `5df5f7faf6808f77bb77f907c9376166a9d6f703`

Patch SHA-256: `300b1b5c6f03feb673468f20bebbd2fe987a66405086bfd26fa5063ec77b390c`

Submodules: none

Forwarded environment: none

The table records whether each run matched its expected result. For exploits and
variants, a matched baseline means the safety assertion failed as expected.
Observations require the controls and comparisons described in the findings and gaps.

| Check | Kind | Base expectation | Patched expectation | Output comparison |
|---|---|---|---|---|
| `behavior-same-target-parity` | behavior | matched | matched | same |
| `control-same-target` | control | matched | matched | not compared |
| `exploit-debug-mismatch` | exploit | matched | matched | not compared |
| `regression-auth-tests` | regression | matched | matched | not compared |
| `security-production-config` | security | matched | matched | not compared |
| `suite-settings-tests` | suite | not run | matched | not compared |
| `variant-runtime-fallback` | variant | matched | matched | not compared |

## Next steps

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
