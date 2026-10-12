# Hash-locked Python dependency advisory snapshot

- Date: 2026-10-12
- Inputs: `requirements.txt` SHA-256 `d43c6568c0cfc14b7ac188656d0d62b373f9b4417e1fd7a87b22e3cfcc13eac5`; `requirements-test.txt` SHA-256 `2710c6a443d8440f429072b69e5223a4359031b78ab64869f7eb57b2a5b28eea`
- Tool: `pip-audit 2.10.1`, installed in a temporary Python virtual environment from `requirements-audit.txt` using `pip install --require-hashes`; auditor lock SHA-256 `6d9abeb2581d466967766259722f028a2b0efd51e06acbf8fc9dbedfc0e83be2`
- Command: `python scripts/dev/audit_python_dependencies.py`
- Result: exit 0; 54 host-applicable runtime lock entries and 2 test-only entries audited; zero known advisories.
- Summary: `pip-audit 2.10.1 lock_entries=56 vulnerabilities=0 requirements_sha256=d43c6568c0cfc14b7ac188656d0d62b373f9b4417e1fd7a87b22e3cfcc13eac5 test_requirements_sha256=2710c6a443d8440f429072b69e5223a4359031b78ab64869f7eb57b2a5b28eea auditor_lock_sha256=6d9abeb2581d466967766259722f028a2b0efd51e06acbf8fc9dbedfc0e83be2`
- Machine-readable result: [`result-hashlocked.json`](result-hashlocked.json), SHA-256 `a1d4d83b08a87d5a44337811bcb72bc2787ccde817328cbd0cd7d6ab147b1282`.

The helper audits runtime and test-only manifests separately in an isolated scanner environment. It removes scratch data on success and retains diagnostic data on setup failure or findings. This is a point-in-time Python advisory check; it does not cover application code, OS packages, browser binaries, deployment configuration or future advisories.
