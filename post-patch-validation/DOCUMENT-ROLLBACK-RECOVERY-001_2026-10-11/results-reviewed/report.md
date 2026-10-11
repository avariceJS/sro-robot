# Post-Patch Validation

All supplied checks passed. Human review is still required.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: complete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

All required checks produced usable evidence.

## Evidence

Finding: DOCUMENT-ROLLBACK-RECOVERY-001: Database rollback after content-addressed file creation leaves an orphan that blocks a same-content retry; recovery must validate ownership, mode, inode and exact digest without overwriting or following symlinks.

Base commit: `b2d2521decc825779a12f2522b8072712aa6b076`

Patch SHA-256: `3b9552a914b07db74326b8f46eb397903a9ac2d76513fc8c791064acd02797a6`

Submodules: none

Forwarded environment: none

The table records whether each run matched its expected result. For exploits and
variants, a matched baseline means the safety assertion failed as expected.
Observations require the controls and comparisons described in the findings and gaps.

| Check | Kind | Base expectation | Patched expectation | Output comparison |
|---|---|---|---|---|
| `behavior-first-upload-output` | behavior | matched | matched | same |
| `control-fresh-admission` | control | matched | matched | not compared |
| `exploit-rollback-orphan-retry` | exploit | matched | matched | not compared |
| `regression-storage-permissions` | regression | matched | matched | not compared |
| `security-symlink-preservation` | security | matched | matched | not compared |
| `suite-local-unittest-discovery` | suite | not run | matched | not compared |
| `variant-flush-failure-retry` | variant | matched | matched | not compared |

## Next steps

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
