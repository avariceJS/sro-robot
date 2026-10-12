# Post-Patch Validation

All supplied checks passed. Human review is still required.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: complete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

All required checks produced usable evidence.

## Evidence

Finding: AUDIT-EVENT-REQUIRED-FIELDS-001: The PostgreSQL audit-event allowlist permits reviewed event kinds with empty payloads, omitting required evidence such as the approval values digest and worker attempt metadata.

Base commit: `619260af2f77ca9397f1985002382997867f18d6`

Patch SHA-256: `324f0de28dbbc9b3db4de3e6c916ef8ce512a44a92ae18f7b0a3f10a8138bd08`

Submodules: none

Forwarded environment: none

The table records whether each run matched its expected result. For exploits and
variants, a matched baseline means the safety assertion failed as expected.
Observations require the controls and comparisons described in the findings and gaps.

| Check | Kind | Base expectation | Patched expectation | Output comparison |
|---|---|---|---|---|
| `audit-payload-regression` | regression | matched | matched | not compared |
| `extra-event-field` | security | matched | matched | not compared |
| `missing-approved-digest` | exploit | matched | matched | not compared |
| `missing-required-fields` | variant | matched | matched | not compared |
| `unit-suite` | suite | not run | matched | not compared |
| `valid-approved-control` | control | matched | matched | not compared |
| `valid-event-behavior` | behavior | matched | matched | same |

## Next steps

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
