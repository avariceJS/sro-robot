# Hash-locked Python dependency advisory snapshot

- Date: 2026-10-11
- Input: `requirements.txt` (56 exact Python distribution pins with SHA-256 release hashes)
- Tool: `pip-audit 2.10.1`, installed in a dedicated temporary Python 3.13 virtual environment; no project environment was modified.
- Command: `pip-audit -r requirements.txt --format json`
- Platform resolution: 54 applicable distributions on the local macOS arm64/Python 3.13 host; the other exact pins are platform/environment-specific. All 56 CPython 3.13 Linux x86-64 wheel selections were separately validated against the lock hashes.
- Result: exit 0; zero known advisories across the 54 host-resolved distributions.
- Machine-readable output: [`result-hashlocked.json`](result-hashlocked.json).

This is a point-in-time advisory-database result for the Python requirements file only. It does not cover operating-system packages, Tesseract/Poppler/Chromium, Docker images, application code, dependency provenance, or future advisories. It does not qualify the environment as secure or production-ready. Repeat the audit during release qualification and after dependency changes.
