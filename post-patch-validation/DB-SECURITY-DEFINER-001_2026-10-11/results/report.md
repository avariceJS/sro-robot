# Post-Patch Validation

Validation is incomplete. No supported patch failure was established.

Declared evidence level: runtime (execution of the reported behavior and its safety assertions).

Assessment: incomplete. Human review is required.

## Findings

No supported failures in the supplied checks.

## Validation gaps

- `behavior-direct-dml`: base: execution_error: check behavior-direct-dml cwd does not exist on base: {checkout}
- `behavior-direct-dml`: patched: execution_error: check behavior-direct-dml cwd does not exist on patched: {checkout}
- `control-benign-runtime`: base: execution_error: check control-benign-runtime cwd does not exist on base: {checkout}
- `control-benign-runtime`: patched: execution_error: check control-benign-runtime cwd does not exist on patched: {checkout}
- `exploit-explicit-execute`: base: execution_error: check exploit-explicit-execute cwd does not exist on base: {checkout}
- `exploit-explicit-execute`: patched: execution_error: check exploit-explicit-execute cwd does not exist on patched: {checkout}
- `role-policy-unit-suite`: patched: execution_error: check role-policy-unit-suite cwd does not exist on patched: {checkout}
- `role-smoke-regression`: base: execution_error: check role-smoke-regression cwd does not exist on base: {checkout}
- `role-smoke-regression`: patched: execution_error: check role-smoke-regression cwd does not exist on patched: {checkout}
- `security-ungranted-definer`: base: execution_error: check security-ungranted-definer cwd does not exist on base: {checkout}
- `security-ungranted-definer`: patched: execution_error: check security-ungranted-definer cwd does not exist on patched: {checkout}
- `variant-public-execute`: base: execution_error: check variant-public-execute cwd does not exist on base: {checkout}
- `variant-public-execute`: patched: execution_error: check variant-public-execute cwd does not exist on patched: {checkout}

## Evidence

Finding: DB-SECURITY-DEFINER-001: Production runtime role guard must reject executable SECURITY DEFINER routines that can use owner privileges to mutate protected application data.

Base commit: `74aaa23b1e8b457303c15691b1f390a65c55a8f3`

Patch SHA-256: `e309760cf415be5536d29d75d89b8037d40c100df37b224b63ff3eda6ead8468`

Submodules: none

Forwarded environment: none

The table records whether each run matched its expected result. For exploits and
variants, a matched baseline means the safety assertion failed as expected.
Observations require the controls and comparisons described in the findings and gaps.

| Check | Kind | Base expectation | Patched expectation | Output comparison |
|---|---|---|---|---|
| `behavior-direct-dml` | behavior | incomplete | incomplete | not compared |
| `control-benign-runtime` | control | incomplete | incomplete | not compared |
| `exploit-explicit-execute` | exploit | incomplete | incomplete | not compared |
| `role-policy-unit-suite` | suite | not run | incomplete | not compared |
| `role-smoke-regression` | regression | incomplete | incomplete | not compared |
| `security-ungranted-definer` | security | incomplete | incomplete | not compared |
| `variant-public-execute` | variant | incomplete | incomplete | not compared |

## Next steps

Return findings to the patch author and investigate validation gaps.
After revising the patch or checks, pin the new inputs and save a fresh run.

Review the saved assertions and omitted paths before accepting the patch. A complete
assessment means the supplied checks produced usable evidence.
It does not establish that coverage is exhaustive.
