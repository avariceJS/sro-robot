# Post-Patch Validation

All supplied checks passed. Human review is still required.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: complete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

All required checks produced usable evidence.

## Evidence

Finding: PG-POOL-SEARCH-PATH-001: A pooled PostgreSQL connection can retain a role-specific search_path changed during a request and resolve a later request's unqualified ORM tables through a shadow schema.

Base commit: `a27cffabcf9d758d7d991f69956cc74c1c38c18d`

Patch SHA-256: `ebfa0ec08ba67863d6bcd69028b843a3f12ac024152f97d06ae1627c5be2bd7a`

Submodules: none

Forwarded environment: none

The table records whether each run matched its expected result. For exploits and
variants, a matched baseline means the safety assertion failed as expected.
Observations require the controls and comparisons described in the findings and gaps.

| Check | Kind | Base expectation | Patched expectation | Output comparison |
|---|---|---|---|---|
| `behavior-default-path` | behavior | matched | matched | same |
| `control-pooled-path` | control | matched | matched | not compared |
| `exploit-runtime-pool-reuse` | exploit | matched | matched | not compared |
| `regression-database-cli` | regression | matched | matched | not compared |
| `security-config-redaction` | security | matched | matched | not compared |
| `suite-connection-boundaries` | suite | not run | matched | not compared |
| `variant-migration-pool-reuse` | variant | matched | matched | not compared |

## Next steps

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
