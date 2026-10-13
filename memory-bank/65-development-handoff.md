# Continue development on another computer

## Checkout

The checkpoint branch is `demo/safe-office-demo` in `avariceJS/sro-robot`.

For a new checkout:

```bash
git clone --branch demo/safe-office-demo https://github.com/avariceJS/sro-robot.git
cd sro-robot
```

For an existing clean checkout:

```bash
git fetch origin
git switch demo/safe-office-demo
git pull --ff-only origin demo/safe-office-demo
```

Start with `AGENTS.md`, `memory-bank/.now.md` and `memory-bank/00-start-here.md`.
Preserve any changes already present on the other computer; never reset/clean them.

## Recreate the local environment

Git contains application code, pinned dependency files, migrations, helper source,
synthetic tests and compact engineering evidence. It does not transfer `.env`,
`.venv`, browser profiles/cookies, confidential samples, uploaded documents,
PostgreSQL/Redis volumes, running workers or the temporary Firefox installation.

Follow the root README to install Python 3.13 dependencies, local Tesseract with
Russian data, Poppler, Playwright Chromium and Docker. Create a private `.env`
from `.env.example` with local secrets and database/Redis settings. Do not commit
it. SRO credentials are optional for development that does not open SRO.

For a new empty local database, start the loopback-only Compose services, apply
`python -m app.cli init-db` in the project virtual environment, and create a local
application user with `python -m app.cli create-user`. The schema head is
`b62d941a7f30`; both migrations following `f91a2d6b83c4` are included. Use the CLI
help/README for arguments. Existing databases require backup and explicit schema
reconciliation, not an automatic reset or stamp.

Run `bash scripts/dev/run-web.sh`, then check `python -m app.cli ops-status`.
On a fresh/reconciled queue, run one OCR worker and one preview worker in separate
terminals using the project virtual environment:

```bash
.venv/bin/python scripts/dev/run_worker.py ocr
.venv/bin/python scripts/dev/run_worker.py sro_preview
```

Do not start a generic `sro` worker or replay historical failed/external jobs.
Restart workers with the current code after schema updates; cached old processes
can fail their per-job schema guard even when current code and DB agree.

## KAD and SRO workflow

Install ordinary Firefox 128+ and load
`browser-extension/kad-helper/manifest.json` through
`about:debugging#/runtime/this-firefox` → Load Temporary Add-on. The helper README
and authenticated `/kad-helper` page describe its PDF-transfer permissions and
local service access. Enable them on this computer; grants do not come from Git.
Temporary add-ons may need loading again after Firefox restarts.

Sign into `http://localhost:8000/`, enter the case number and optional act date,
and use Find and recognize. Keep the originating service tab open through receipt.
Helper 0.2.6 uses ordinary browser navigation and captures the actual PDF response
without manual download/upload, then performs local extraction. A unique candidate
and matching parsed case/date are required; known access checks stop explicitly.
This is an ordinary-browser executor, not a qualified headless server service.

Review and approve the extracted fields before preparing SRO. The only allowed
real SRO sequence is Add a new blank card → fill approved supported fields →
inspect/read back → Close without saving. Save/Send, existing-record changes and
nested address actions are prohibited. One owner-authorized two-page act completed
PDF intake, 11-field review and this unsaved form preview; broader document,
concurrency and recovery coverage still needs qualification.

The checkpoint preserves the independent experimental Fedresurs API/backend and
its schema work. It is not the requested primary workflow or an automatic fallback.
The older MKAD/WebDriver adapter and its historical acquisition jobs remain retired.

## Evidence and next work

The latest KAD change passed 100 Node tests and 23 Python import/distribution tests;
commands and limits are in the helper README and `.now.md`. This checkpoint's
additional backend/migration work received static import/schema/diff inspection.
The complete exact-tree verifier has not been rerun for this handoff; run it in a
disposable environment before treating the checkpoint as a qualified release.

Continue with wider act/layout coverage, employee review, local processing and
operational reliability. Keep personal documents and OCR local, preserve source
and selection evidence, and keep production readiness claims bounded by evidence.
