# Bootstrap installer audit — PIP-BOOTSTRAP-001

Date: 2026-10-11

## Finding and change

The reproducible Python 3.13 development environment had 56 SHA-256 locked application distributions, but the `pip` installer was inherited from `venv`/the Python distribution and was not pinned. A fresh baseline environment used pip 25.2. `pip-audit 2.10.1` reported 12 advisory records for pip alone (six distinct advisory IDs); no other installed distribution was affected.

Added `requirements-bootstrap.txt`, pinning `pip==26.2.1` with SHA-256 hashes for every PyPI release artifact. README and CI now upgrade the fresh venv's installer from this manifest before installing `requirements.txt`. A separate `requirements-audit.txt` pins `pip-audit==2.10.1` and its 26 transitive tool packages with hashes for every PyPI artifact. CI installs this scanner lock and audits the installed environment before running the full verifier. Contract tests verify all lock manifests and install/audit order.

## Baseline-versus-patch evidence

- Baseline audit: 57 installed distributions (the 56 application pins plus pip 25.2), 12 vulnerability records, all attached to pip.
- Patched audit: 57 installed distributions (the same 56 application pins plus pip 26.2.1), zero known vulnerability records.
- The new environment was built from a fresh Python 3.13 venv. Both manifests installed successfully with `--require-hashes`; `pip check` reported no broken requirements.
- `tests.test_ci_contract` and `tests.test_settings` passed (35 tests). Full `scripts/dev/verify.sh` passed at `SRO_DRILL_TASKS=100000`, including all 28 PostgreSQL integration tests in the disposable drill, 100,000 task/event identities, 100,001 restored files, migration/lease/role/queue/RQ recovery, and 1,000 unique claims in both thread and process diagnostics.
- No shared database/Redis mutation and no real SRO action occurred.

The audit is a point-in-time check against pip-audit's advisory service. It does not validate compromised package publishers, OS/browser assets, pip's vendored-code reachability, or future advisories. The bootstrap is downloaded via the configured package index and is hash-verified against the manifest.

## Differential review

The changes add separate bootstrap and advisory-tool locks, install and scan them through the CI workflow and local instructions, and expand the CI contract test. Runtime application code and the 56 application pins are unchanged. The updated installer is pip 26.2.1; the audit-enabled pip-audit result for all 78 installed distributions was clean. Hosted workflow execution remains unobserved. No additional source or runtime behavior regression was found.

The advisory [CVE-2026-13346](https://github.com/advisories/ghsa-qwm4-qh6w-59xr) is one of the pip 25.2 findings and marks pip versions before 26.2.0 affected.
