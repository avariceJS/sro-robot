# Post-Patch Validation

Validation is incomplete. No supported patch failure was established.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: incomplete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

- `exploit-worker-opens-outside-root-path`: patched: marker_missing; the safety assertion was not reached.
- `variant-symlink-source-is-withheld`: patched: marker_missing; the safety assertion was not reached.
- `variant-tampered-source-is-withheld`: patched: marker_missing; the safety assertion was not reached.

## Evidence

Finding: OCR-STORED-SOURCE-001: The OCR worker parsed a database-referenced path without rechecking that it remained within private document storage and matched its recorded digest.

Base commit: `fb6a43ddd27fcb8dba0865ca77d9ed70e4890378`

Patch SHA-256: `a33fa7d65515a92ec9705ae4416b9b768da1d824928dc246883d5bbe2ad09a7a`

Submodules: none

Forwarded environment: SRO_ROBOT_PYTHON

The table records whether each run matched its expected result. For exploits and
variants, a matched baseline means the safety assertion failed as expected.
Observations require the controls and comparisons described in the findings and gaps.

| Check | Kind | Base expectation | Patched expectation | Output comparison |
|---|---|---|---|---|
| `behavior-valid-source-still-parses` | behavior | matched | matched | same |
| `control-private-storage-source` | control | matched | matched | not compared |
| `exploit-worker-opens-outside-root-path` | exploit | matched | incomplete | not compared |
| `regression-claim-guards-remain-intact` | regression | matched | matched | not compared |
| `security-private-source-readback` | security | matched | matched | not compared |
| `suite-full-local-verifier` | suite | not run | matched | not compared |
| `variant-symlink-source-is-withheld` | variant | matched | incomplete | not compared |
| `variant-tampered-source-is-withheld` | variant | matched | incomplete | not compared |

## Next steps

Return findings to the patch author and investigate validation gaps.
After revising the patch or checks, pin the new inputs and save a fresh run.

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
