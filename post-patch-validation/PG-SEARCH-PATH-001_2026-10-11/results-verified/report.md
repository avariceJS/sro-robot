# Post-Patch Validation

All supplied checks passed. Human review is still required.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: complete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

All required checks produced usable evidence.

## Evidence

Finding: PG-SEARCH-PATH-001: Runtime and migration database roles can resolve unqualified tables through different PostgreSQL search_path values, making preflight, migrations, and ORM use different schemas.

Base commit: `04a0092f8ccfcbde77003a40eb7828533bbb72ba`

Patch SHA-256: `aef9a59ef4f764b1a9ff0cf1ddd762e93dfc96be1b0b1e68b251a04f0daa883d`

Submodules: none

Forwarded environment: none

The table records whether each run matched its expected result. For exploits and
variants, a matched baseline means the safety assertion failed as expected.
Observations require the controls and comparisons described in the findings and gaps.

| Check | Kind | Base expectation | Patched expectation | Output comparison |
|---|---|---|---|---|
| `behavior-default-public-path` | behavior | matched | matched | same |
| `control-role-smoke` | control | matched | matched | not compared |
| `exploit-runtime-shadow-schema` | exploit | matched | matched | not compared |
| `regression-database-cli` | regression | matched | matched | not compared |
| `security-secret-repr` | security | matched | matched | not compared |
| `suite-dependency-boundaries` | suite | not run | matched | not compared |
| `variant-migration-shadow-schema` | variant | matched | matched | not compared |

## Next steps

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
