# Hash-locked Python dependency advisory snapshot

- Date: 2026-10-12
- Input: `requirements.txt`, SHA-256 `d43c6568c0cfc14b7ac188656d0d62b373f9b4417e1fd7a87b22e3cfcc13eac5`
- Tool: `pip-audit 2.10.1`, installed in a temporary Python virtual environment from `requirements-audit.txt` with `pip install --require-hashes`; auditor lock SHA-256 `6d9abeb2581d466967766259722f028a2b0efd51e06acbf8fc9dbedfc0e83be2`
- Command: `python scripts/dev/audit_python_dependencies.py`
- Result: exit 0; 54 host-applicable distributions audited; zero known advisories.
- Machine-readable result: [`result-hashlocked.json`](result-hashlocked.json), SHA-256 `f3a4ca2b81f1bc0803d6dad7dcd068c5d6ce7e60783fdc8804de44ba026992c4`.

The helper installs the scanner only into a disposable venv, verifies the scanner dependency artifacts against hashes, emits the scanner's JSON result, removes scratch data on success and retains diagnostic scratch data on setup failure or findings. `pip-audit` uses the Python Packaging Advisory Database exposed through PyPI's JSON API ([official PyPI project](https://pypi.org/project/pip-audit/)).

This is a point-in-time advisory snapshot for Python requirements only. It does not cover application code, OS packages, Tesseract/Poppler/Chromium, container images, package provenance, deployment configuration or future advisories. It does not establish production readiness; repeat during release qualification and after dependency changes.
