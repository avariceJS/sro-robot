# Post-Patch Validation

Validation is incomplete. No supported patch failure was established.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: incomplete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

- `behavior-valid-document-reuse`: The baseline check failed; a new failure cannot be attributed to the patch.
- `control-private-storage-readback`: The baseline check failed; a new failure cannot be attributed to the patch.
- `control-private-storage-readback`: The patched harness control failed.
- `exploit-outside-root-reference`: base: marker_missing; the safety assertion was not reached.
- `exploit-outside-root-reference`: patched: marker_missing; the safety assertion was not reached.
- `security-corrupted-bytes-rejected`: The baseline check failed; a new failure cannot be attributed to the patch.
- `variant-symlink-reference`: base: marker_missing; the safety assertion was not reached.
- `variant-symlink-reference`: patched: marker_missing; the safety assertion was not reached.

## Evidence

Finding: DUPLICATE-DOCUMENT-REFERENCE-001: Re-uploading an existing document hash trusted its database path without validating private-root confinement, file type, owner, mode, or current content digest.

Base commit: `fddf6326ca1f463e2aeef97b4c4d02fe39714642`

Patch SHA-256: `89f6e924db7f14658ddd033b37d32039f6441235728cbd3a3c7e73f1457240f6`

Submodules: none

Forwarded environment: SRO_ROBOT_PYTHON

The table records whether each run matched its expected result. For exploits and
variants, a matched baseline means the safety assertion failed as expected.
Observations require the controls and comparisons described in the findings and gaps.

| Check | Kind | Base expectation | Patched expectation | Output comparison |
|---|---|---|---|---|
| `behavior-valid-document-reuse` | behavior | did not match | did not match | same |
| `control-private-storage-readback` | control | did not match | did not match | not compared |
| `exploit-outside-root-reference` | exploit | incomplete | incomplete | not compared |
| `regression-storage-security-tests` | regression | matched | matched | not compared |
| `security-corrupted-bytes-rejected` | security | did not match | did not match | not compared |
| `suite-full-local-verifier` | suite | not run | matched | not compared |
| `variant-symlink-reference` | variant | incomplete | incomplete | not compared |

## Next steps

Return findings to the patch author and investigate validation gaps.
After revising the patch or checks, pin the new inputs and save a fresh run.

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
