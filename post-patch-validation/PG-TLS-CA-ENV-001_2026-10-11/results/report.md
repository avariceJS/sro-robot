# Post-Patch Validation

All supplied checks passed. Human review is still required.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: complete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

All required checks produced usable evidence.

## Evidence

Finding: PG-TLS-CA-ENV-001: An ambient PGSSLROOTCERT can replace libpq’s implicit TLS trust root while production settings accept sslmode=verify-full.

Base commit: `180fd97c108762ec8666e61fe597d307ce9d6fe1`

Patch SHA-256: `4e01842c63c5e07d6e4912203e43e4979b71ed19645bfe886e0b1ef426e18ded`

Submodules: none

Forwarded environment: none

The table records whether each run matched its expected result. For exploits and
variants, a matched baseline means the safety assertion failed as expected.
Observations require the controls and comparisons described in the findings and gaps.

| Check | Kind | Base expectation | Patched expectation | Output comparison |
|---|---|---|---|---|
| `behavior-valid-tls` | behavior | matched | matched | same |
| `control-valid-production-config` | control | matched | matched | not compared |
| `exploit-ambient-ca-root` | exploit | matched | matched | not compared |
| `regression-settings` | regression | matched | matched | not compared |
| `security-debug-compatible` | security | matched | matched | not compared |
| `suite-settings-patched` | suite | not run | matched | not compared |
| `variant-migration-url` | variant | matched | matched | not compared |
| `variant-runtime-url` | variant | matched | matched | not compared |

## Next steps

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
