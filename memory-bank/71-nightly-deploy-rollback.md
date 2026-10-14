# Local nightly changes: morning review, server update and rollback

## Scope and evidence boundary

The nightly branch is local and unpushed. No VPS files, services, secrets, database or queues were accessed. This runbook is prepared from repository configuration and local synthetic tests; it is not evidence that the server has matching versions, ports, service users, display support or file ownership.

The upload UI/outbox change has no database migration. The KAD helper manifest is version `0.2.11`; reloading its unpacked source is required for the owned-tab-close behavior. No live KAD lookup, SRO action or external record was performed.

## Morning review from the development computer

```bash
cd /Users/root1/Desktop/sro-robot-nightly
git status --short --branch
git log --oneline --decorate -8
git diff --check
git diff 8db1e78791cce531689a142cb6b3420835f2ac35...HEAD
```

The original checkout at `/Users/root1/Desktop/sro-robot` was left untouched. Review the feature commits and tests on `codex/nightly-100pdf-20261014`; nothing was pushed or merged automatically.

## Server preflight before any update

Run these as read-only checks through the existing approved access path. Do not copy `.env`, browser profiles, real PDFs, OCR output, or database contents to a workstation or report.

1. Record the server's current Git commit/branch, app and worker process command lines, listening port, Compose/systemd unit names, service account, and versions of Python, Firefox, the KAD helper, Tesseract and Poppler. Compare the runtime `ops-status` and `/readyz` outputs without printing configuration values.
2. Confirm which user owns the app directory, upload directory, PostgreSQL/Redis volumes and Firefox profile. Preserve existing private file ownership and modes. Confirm the server has an interactive graphical session for the operator account before using the Firefox launcher. Do not use headless browser automation for the KAD workflow.
3. Read-only reconcile OCR, SRO-preview and outbox queues. Do not retry historical, failed, or unknown-outcome SRO jobs. Confirm the live schema is still `c3a8e1d04b57`; this change does not require a migration.
4. Take and verify a database backup using the site's normal backup procedure. Record hashes for the backup and code archive in private operational records, never in Git.
5. Review the exact remote/local diff against the server revision. The night left the branch unpushed, so deliver the reviewed source through the owner's normal release process.

## Apply after review and authorization

1. Prepare a versioned application directory or a clean worktree for the reviewed commit. Do not overwrite the currently running tree. Keep `.env`, uploads, SQLite/DB files, browser profiles and logs outside Git.
2. Install the repository's pinned dependencies and run the focused checks in a disposable environment. No migration is needed for the upload batching, replay-intent, helper-tab-loss or Firefox launcher changes.
3. Stop the web process and local OCR workers gracefully through their actual service manager. Verify that no worker from the prior code remains before starting workers from the new version.
4. Start the web app and OCR worker using the existing site units and private service account. Check `/healthz`, `/readyz`, `ops-status`, upload limits and queue health before exposing the service to staff.
5. For server-side KAD work, establish an interactive session as the dedicated private operator account, then run:

   ```bash
   SRO_LOCAL_APP_URL=http://127.0.0.1:8000 scripts/ops/launch-kad-firefox.sh
   ```

   The launcher refuses root, requires X11/Wayland and a loopback app on port 8000, creates/uses a mode-0700 Firefox profile owned by that user, and opens the service and add-on debugging pages. In Firefox 128+, load `browser-extension/kad-helper/manifest.json` through `about:debugging#/runtime/this-firefox` and grant the helper's required permissions. The helper is a temporary add-on and must be loaded again after Firefox exits. Keep the ordinary browser visible to the operator and stop on any access challenge.
6. Run a synthetic local intake smoke test and read the resulting task/source mapping back before staff use. A test KAD scenario does not qualify a real server-side search.

Do not change firewall, remote-desktop, systemd, profile policy, service-user, database or Redis settings as part of this source update. If an interactive session or approved access is unavailable, keep using the existing employee-browser workflow and leave the server-side browser path unverified.

## Rollback

1. Stop the new web process and all new-version OCR workers. Reconcile the queue and outbox read-only; do not retry an uncertain external SRO operation.
2. Start the previously recorded application version in its preserved directory and restart only its matching OCR workers. Keep PostgreSQL/Redis data volumes and the upload directory intact; do not run `docker compose down -v`, reset the DB, or remove user files.
3. Reload the previously recorded KAD helper version in the operator's browser. Leave the new private Firefox profile untouched until its owner decides whether to retain it.
4. Check `/readyz`, `ops-status`, queue/outbox counts and a synthetic local upload. If a migration is ever added in a later release, do not infer rollback safety from this no-migration change; use the migration-specific recovery procedure.

## Unverified until the owner reviews the server

- Actual VPS repository revision, operating system, service manager, process user, ports, firewall and private-directory ownership.
- Firefox installation/version and availability of a supported interactive desktop session for the operator account.
- Current server add-on version/permissions, live KAD search behavior and real document formats.
- Server-specific OCR throughput, memory ceiling, employee concurrency and restore/recovery behavior.
