# Post-Patch Validation

All supplied checks passed. Human review is still required.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: complete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

All required checks produced usable evidence.

## Evidence

Finding: DOC-FILE-CREATE-SECURITY-001: Document writes used write_bytes followed by chmod, allowing broad initial permissions and truncating/following pre-existing paths; partial writes were not safely cleaned up.

Base commit: `4ffc478bd9a4d0ab087286a563f0d84141d47b67`

Patch SHA-256: `f9e73c87f5de8ed79abe706143a34b4a533a6baabcd7df08bf194955fb65c8aa`

Submodules: none

Forwarded environment: none

The table records whether each run matched its expected result. For exploits and
variants, a matched baseline means the safety assertion failed as expected.
Observations require the controls and comparisons described in the findings and gaps.

| Check | Kind | Base expectation | Patched expectation | Output comparison |
|---|---|---|---|---|
| `behavior-content-and-mode` | behavior | matched | matched | same |
| `control-basic-write` | control | matched | matched | not compared |
| `exploit-initial-permissions` | exploit | matched | matched | not compared |
| `regression-auth-tests` | regression | matched | matched | not compared |
| `security-final-permissions` | security | matched | matched | not compared |
| `suite-storage-regressions` | suite | not run | matched | not compared |
| `variant-existing-path` | variant | matched | matched | not compared |
| `variant-symlink-destination` | variant | matched | matched | not compared |

## Next steps

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
