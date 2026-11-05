# SRO Office

SRO Office is a local document-processing and staff-review service for a legal
office. It accepts photos/HEIC, images, TIFF and PDF batches, extracts fields and
sources, and maintains internal debtor cards and assignment drafts. KAD documents
arrive through the ordinary-browser helper. SROArbitr actions are separately
guarded; an internal save or a filled external form is not an external saved record.

## Current product

- Staff authentication and revocable sessions; shared worklists and attributable edits.
- Resumable batch intake, per-file progress, durable OCR admission and retries.
- PDF text-layer extraction, local Tesseract Russian OCR, role-aware person/entity
  parsing, formal identifier checks and editable review suggestions.
- Original documents beside review fields, source highlights and PDF/TIFF previews.
- Internal natural-person, organization and sole-proprietor cards; explicit
  employee review, version conflicts, revision history and linked assignment drafts.
- Employee-curated case packages and explicit membership; no silent merge by case/INN.
- KAD helper distribution/import with selected-document provenance.
- Guarded SRO preparation, readback and operation-state controls. Recognition-only
  mode blocks external actions; assignment form automation is not qualified.

OCR confidence is heuristic, not a correctness probability. Missing extraction is
not proven source absence. Staff must resolve uncertain identity, role, date and
address/type; never invent digits, an apartment or an owner link. There is no local
neural-model dependency in the supported extraction path.

## Run locally

Python 3.13, Node, Docker, Tesseract with Russian traineddata and Poppler are required.
Install pinned Python dependencies into the project environment:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-bootstrap.txt
.venv/bin/python -m pip install --require-hashes -r requirements.txt
.venv/bin/python -m pip install --require-hashes -r requirements-test.txt
.venv/bin/python -m playwright install chromium
npm ci
npm start
```

`npm start` uses `scripts/dev/start-local.sh`: private `.local-run/local.env`,
loopback PostgreSQL/Redis, migrations, workers, dispatcher and web. It does not use
the root `.env` or an external database. Check existing local data/queues before
starting. Stop with Ctrl+C; database volumes and uploaded documents remain.
Use `SRO_LOCAL_PORT=8001 npm start` if the web port is occupied. Do not remove an
existing volume to fix a startup problem.

For manually managed environments, use `.env.example` and
[memory-bank/40-operations.md](memory-bank/40-operations.md). Keep `.env` private
(mode 0600), and never give the web/worker process migration-role credentials.
For an already configured, safely migrated manual environment, start the web
process with `bash scripts/dev/run-web.sh`; workers/dispatcher are separate
processes described in the operations guide.

Employee login is verified by SROArbitr; local tests use fake authentication and
disposable databases. Never use the real registry as a test fixture.

## Development and checks

```sh
scripts/dev/verify.sh
npm run build:css
```

The verifier runs Python and Node product checks, compilation, shell checks and
isolated PostgreSQL role/migration/restore/recovery checks. Docker must be available;
it creates its own disposable containers and must not target office databases.
Database tests refuse arbitrary targets. Use the current targeted tests during a
small change, and the full verifier for cross-cutting or persistence changes.
CSS source is `app/web/styles/app.css`; commit its generated static CSS when changed.

Work only in `develop`. Each completed stage is verified, selectively committed,
pushed and checked on GitHub. `main` is verified release source; deployment is a
separate manual operation. Keep only `main`/`develop`; extra branches need explicit
owner instruction. See [AGENTS.md](AGENTS.md) and
[release workflow](docs/RELEASE_WORKFLOW.md).

## Code and documentation

| Path | Current purpose |
| --- | --- |
| `app/` | FastAPI/UI, PostgreSQL state, Redis/RQ, intake, review and internal workflows |
| `sro_robot/core/` | Local OCR, parsing, validation and source geometry |
| `sro_robot/platform/`, `scenarios/` | Guarded SRO adapters and operation contracts |
| `browser-extension/kad-helper/` | Ordinary-browser KAD selection and PDF transfer |
| `migrations/` | Forward schema history; preserve existing-install upgrade paths |
| `scripts/dev/`, `tests/` | Development, current regression and isolated recovery checks |
| `scripts/ops/`, `deploy/` | Deployment, backup, restore and operational tools |
| `memory-bank/00-start-here.md` | Compact current architecture/operations index |

Staff workflows: [workspace](docs/WORKSPACE_UI.md), [batch intake](docs/BULK_PROCESSING.md),
[debtor entities](docs/DEBTOR_ENTITIES.md), [assignments](docs/APPOINTMENTS.md),
[case packages](docs/CASE_PACKAGES.md), [source highlights](docs/FIELD_SOURCE_HIGHLIGHTS.md).
KAD administration/publication instructions are under `docs/` and the extension README.

Original `samples`, `storage`, browser state, credentials, databases, environments
and generated output stay outside Git. Do not upload document content to cloud
recognition services. Historical implementation and verification records are in Git.
