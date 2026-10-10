# Python dependency vulnerability snapshot

- Date: 2026-10-11
- Input: `requirements.txt` (51 pinned Python distributions)
- Tool: `pip-audit 2.10.1`, installed in a temporary isolated virtual environment; project dependencies and project `.venv` were not modified.
- Command: `pip-audit -r requirements.txt --format json`
- Result: exit 0; no known vulnerabilities reported; zero advisories across 51 packages.
- Machine-readable output: [`result.json`](result.json).

This is a point-in-time advisory-database result for the Python requirements file only. It does not cover operating-system packages, Tesseract/Poppler/Chromium, Docker images, application code, dependency provenance, package hashes, or future advisories. It does not qualify the environment as secure or production-ready. Repeat the audit during release qualification and after dependency changes.
