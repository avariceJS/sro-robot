# Post-Patch Validation

All supplied checks passed. Human review is still required.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: complete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

All required checks produced usable evidence.

## Evidence

Finding: UVICORN-ACCESS-LOG-001: The documented Uvicorn launch path enables its default access logger, which records request paths and query strings containing internal IDs or user-controlled identifiers

Base commit: `5babe9abc9156876544ccde7a9584819954f2dc3`

Patch SHA-256: `48061525210a92afc1d2b34c1e2a3869f44f5f97d09563580d1728e2f07f04eb`

Submodules: none

Forwarded environment: none

The table records whether each run matched its expected result. For exploits and
variants, a matched baseline means the safety assertion failed as expected.
Observations require the controls and comparisons described in the findings and gaps.

| Check | Kind | Base expectation | Patched expectation | Output comparison |
|---|---|---|---|---|
| `adjacent-response-privacy` | security | matched | matched | not compared |
| `behavior-response-equivalence` | behavior | matched | matched | same |
| `harness-control` | control | matched | matched | not compared |
| `internal-id-access-log` | exploit | matched | matched | not compared |
| `logging-regression` | regression | matched | matched | not compared |
| `patched-http-integration-suite` | suite | not run | matched | not compared |
| `unmatched-path-variant` | variant | matched | matched | not compared |

## Next steps

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
