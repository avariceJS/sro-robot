# Post-Patch Validation

Validation is incomplete. No supported patch failure was established.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: incomplete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

- `root-swap-after-leaf-check`: The safety assertion did not fail on baseline; reproduction is missing.

## Evidence

Finding: STORAGE-ROOT-TOCTOU-001: Document reference validation checked the private storage root by pathname, then reopened a child by pathname; replacing the root with a symlink between checks could make the service accept/read a matching file outside the configured root.

Base commit: `8b613cc5640009adfd36e57b651c77f58b63ebc7`

Patch SHA-256: `7c42820b6ae84fb4de5259c5f3f6e26605d8ddac1e71a86a09bd298a99ec88f9`

Submodules: none

Forwarded environment: none

The table records whether each run matched its expected result. For exploits and
variants, a matched baseline means the safety assertion failed as expected.
Observations require the controls and comparisons described in the findings and gaps.

| Check | Kind | Base expectation | Patched expectation | Output comparison |
|---|---|---|---|---|
| `adjacent-storage-security` | security | matched | matched | not compared |
| `control-private-file` | control | matched | matched | not compared |
| `full-local-verifier` | suite | not run | matched | not compared |
| `private-file-behavior` | behavior | matched | matched | same |
| `root-swap-after-leaf-check` | variant | did not match | matched | not compared |
| `root-symlink-race` | exploit | matched | matched | not compared |
| `storage-regression` | regression | matched | matched | not compared |

## Next steps

Return findings to the patch author and investigate validation gaps.
After revising the patch or checks, pin the new inputs and save a fresh run.

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
