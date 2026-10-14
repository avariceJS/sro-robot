# Local nightly changes: morning review, server update and rollback

> Status update: the VPS rollout began on 2026-10-14. The preparation notes
> below are historical and are superseded by “Live VPS rollout” at the end of
> this file. Current live facts and blockers are tracked there and in `.now.md`.

## Office cloud preparation after the nightly handoff (2026-10-14)

The separate `codex/cloud-office-20261014` worktree starts from nightly checkpoint
`03cfe2b`. Public deployment is not active. The owner's previous VPS pilot was
`8db1e78` under `/opt/sro-robot`, with systemd user `srorobot`, loopback web port
8000, and Compose project `deploy` (volumes `deploy_pgdata` / `deploy_redisdata`).
These are prior-session facts, not a current remote inventory. The latest direct
SSH attempt timed out before authorization and the workstation's old tunnel is
not listening. The domain and current server/queue status are pending owner input.

Prepared, uninstalled artifacts:

- `deploy/cloud/nginx.conf.template`: one exact domain, valid TLS certificate
  required, canonical upstream Host, proxy headers overwritten at direct ingress,
  105 MiB request ceiling, no document-directory exposure or access log.
- `deploy/cloud/systemd/`: loopback web, bounded individual OCR worker instances,
  and an infra boot unit pinned to the existing Compose project. Docker owns
  container runtime restarts through `deploy/cloud/docker-compose.restart.yml`;
  the oneshot infra unit performs bring-up only. Do not run the legacy OCR unit
  alongside new instances without queue and process reconciliation.
- `scripts/ops/cloud-preflight.sh`: read-only host/process/permission inventory.
  It skips application CLI checks because loading application settings can create
  or chmod private directories. Run the printed application diagnostics separately
  as the service user after accounting for that filesystem behavior.
- `APP_PUBLIC_ORIGIN`: optional explicit canonical HTTPS origin. Public mode
  refuses `APP_DEBUG=1`, restricts Host names, and requires exact HTTPS provenance
  for public-host mutation requests. The helper ZIP is specialized at packaging
  time for that same configured site, without broad HTTPS permissions or source
  file mutation. Default local packaging is retained.

Before installation, obtain current VPS inventory, reconcile queues, preserve a
verified backup of PostgreSQL and the document store, and prepare a distinct
least-privilege runtime database role. The pilot's `sro` database owner must not
be reused as the production runtime role. Existing `APP_DEBUG=0` guards permit
loopback PostgreSQL/Redis on this single host; non-loopback connections retain
their verified TLS requirements. Keep migration credentials distinct. Do not
reset the database or switch to newly named Compose volumes. DNS, certificate
issuance/renewal, backup restoration, proxy behavior, packaged Firefox import,
staff onboarding and VPS capacity still require actual deployment evidence.

Staff initially use their own ordinary browser for KAD, connected to the HTTPS
office site; the server runs storage and local OCR. Browserless/server-side KAD
execution and parallel real SRO records are not implemented or authorized by this
change. Shared-browser account switching during an in-flight PDF import remains
an identity boundary requiring further work; use distinct private staff profiles.
All tasks currently share one office registry; tenant isolation and a web-based
administrator permission system are not introduced here.

Validation for this preparation is syntax and static review only. No new tests,
load run, remote role change, migration, certificate, firewall action, deployment,
or real KAD/SRO operation has been performed. Do not expose this candidate to
staff until its production configuration and end-to-end TLS workflow are verified.

## Scope and evidence boundary

The nightly branch is local and unpushed. No VPS files, services, secrets, database or queues were accessed. This runbook is prepared from repository configuration and local synthetic tests; it is not evidence that the server has matching versions, ports, service users, display support or file ownership.

None of the nightly changes require a database migration. Scanned PDFs without a useful text layer now use the locally measured grayscale/threshold OCR path with a legacy fallback; all results still enter operator review. Pending upload fingerprints and idempotency keys persist in origin-scoped browser local storage so an uncertain upload can be retried after its tab closes; the browser profile must remain private to its operator. The KAD helper manifest is version `0.2.11`; reloading its unpacked source is required for the owned-tab-close behavior. The upload client now verifies persistent retry-key storage before sending the first chunk and stops with a clear no-upload message if storage is unavailable. FIO extraction matches the debtor label without loosening the person-name token checks. A higher global OCR threshold candidate was measured and rejected because it regressed on a separate synthetic corpus; the committed default remains 215. No live KAD lookup, SRO action or external record was performed.

The local stack also processed 2,000 PDFs from 20 synthetic operators through 8 OCR workers, producing 2,000 distinct source items with no failures. This upload run used already authenticated sessions so the upload burst could be measured independently of login admission. A separate simultaneous fresh-login attempt showed the configured login guard (`2` concurrent attempts, `250 ms` wait) returning HTTP 503 for 12 of 20 requests. The guard was left unchanged; do not raise it without a separate security and capacity review. These local results do not qualify 20-user capacity on the VPS.

The upload UI keeps idempotency keys per request chunk so correcting a rejected chunk preserves keys for earlier accepted chunks. The recent-task list polls once for all visible tasks while any remain active and stops at terminal state; this uses the repository's bundled HTMX 2.0.4 and requires no new service or migration.

The Firefox launcher has six local subprocess tests with stubbed `id`, `getent`, health-check `curl`, profile `stat` and Firefox commands. They cover root, missing display, non-loopback URL, unavailable local health, foreign-owned profile and successful mode-0700 profile launch. They do not verify a live server desktop, installed Firefox or real profile ownership.

## Morning review from the development computer

```bash
cd /Users/root1/Desktop/sro-robot-nightly
git status --short --branch
git log --oneline --decorate -8
git diff --check
git diff 8db1e78791cce531689a142cb6b3420835f2ac35...HEAD
```

The review branch is `codex/nightly-100pdf-20261014`, based on `origin/demo/safe-office-demo` at `8db1e787`; it remains unpushed and unmerged. The current tip and full commit list are available from the commands above. The original checkout at `/Users/root1/Desktop/sro-robot` was left untouched. Review the feature commits and tests on the night branch before using the normal release process.

## Server preflight before any update

Run these as read-only checks through the existing approved access path. Do not copy `.env`, browser profiles, real PDFs, OCR output, or database contents to a workstation or report.

1. Record the server's current Git commit/branch, app and worker process command lines, listening port, Compose/systemd unit names, service account, and versions of Python, Firefox, the KAD helper, Tesseract and Poppler. Compare the runtime `ops-status` and `/readyz` outputs without printing configuration values.
2. Confirm which user owns the app directory, upload directory, PostgreSQL/Redis volumes and Firefox profile. Preserve existing private file ownership and modes. Confirm the server has an interactive graphical session for the operator account before using the Firefox launcher. Do not use headless browser automation for the KAD workflow.
3. Read-only reconcile OCR, SRO-preview and outbox queues. Do not retry historical, failed, or unknown-outcome SRO jobs. Confirm the live schema is still `c3a8e1d04b57`; this change does not require a migration.
4. Take and verify a database backup using the site's normal backup procedure. Record hashes for the backup and code archive in private operational records, never in Git.
5. Review the exact remote/local diff against the server revision. The night left the branch unpushed, so deliver the reviewed source through the owner's normal release process.

## Apply after review and authorization

1. Prepare a versioned application directory or a clean worktree for the reviewed commit. Do not overwrite the currently running tree. Keep `.env`, uploads, SQLite/DB files, browser profiles and logs outside Git.
2. Install the repository's pinned dependencies and run the focused checks in a disposable environment. No migration is needed for upload batching, retry-state storage validation, FIO label parsing, OCR preprocessing, replay reconciliation, helper-tab handling or the Firefox launcher changes. Keep all OCR fields in human review; synthetic OCR scores are not a reason to bypass review.
3. Stop the web process and local OCR workers gracefully through their actual service manager. Verify that no worker from the prior code remains before starting workers from the new version.
4. Start the web app and OCR worker using the existing site units and private service account. Check `/healthz`, `/readyz`, `ops-status`, upload limits and queue health before exposing the service to staff. Compare server OCR performance and scan accuracy with the local synthetic baseline in `memory-bank/70-nightly-plan-2026-10-14.md`; do not assume this Mac's eight-worker limit applies to the VPS.
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

## Live VPS rollout

- Host: Ubuntu 24.04 at `85.192.30.98`; the existing `deploy` PostgreSQL/Redis
  containers, named volumes, document directory and staff account were retained.
- Verified backup directory:
  `/var/backups/sro-robot/20261014T182632Z`. PostgreSQL, Redis and document
  artifacts passed checksums; a disposable restore/read-back drill passed and
  an encrypted second copy exists on the FileVault-protected development Mac.
- Candidate release `965fa88419d06325caf15d75fcdbcb4f36fda9be` is active through
  `/opt/sro-robot/current`. Its runtime DB role passed capability checks; the
  root-only migration environment is separate. Schema is unchanged at
  `c3a8e1d04b57`.
- `sro-robot-web` and one `sro-robot-ocr@1` instance run under systemd. Local
  `/readyz` and `/login` return 200. OCR queue depth is zero. Keep the single
  old `sro_preview` job withheld; there is no SRO or preview worker.
- The first guarded activation attempt restored the previous pointer after the
  root migration process rejected the service-owned document store during
  settings startup, before connecting to PostgreSQL. The schema and records
  remain unchanged. The migration unit now uses a private temporary upload path
  under `PrivateTmp` and has no writable access to persistent documents.
- The existing PostgreSQL and Redis containers use `unless-stopped` restart
  policies, applied without restarting either container. New release trees are
  root-owned/read-only; each private runtime `.env` is service-owned mode 0600.
- UFW denies inbound by default and permits TCP 22/80/443. Nginx is installed.
  Public DNS resolves to the VPS and HTTP works from the development Mac, but
  Let's Encrypt HTTP-01 still times out from its source addresses. The
  provider-level rule must permit all public IPv4/IPv6 sources to TCP 80/443
  before certificate issuance and the public HTTPS proxy can pass.
- A daily private backup job passed on the VPS at
  `/var/backups/sro-robot/daily-20261014T203403Z`. PostgreSQL archive catalog,
  Redis 7.4 RDB integrity, document tar and all SHA-256 values passed. Its
  manifest hash is in `.now.md`; directory/artifacts are root-only. The run used
  a temporary pinned-release override; the permanent backup service and timer
  still need to be installed/enabled. An encrypted FileVault local copy also
  passed artifact/hash checks. Earlier failed attempts published no partial
  backup. Automated off-host backup retention and
  external alert delivery are not configured because no destination/endpoint
  credentials are present.
- The deployment entry point is `scripts/ops/deploy_vps.sh`. It accepts only an
  annotated `sro-vX.Y.Z` tag already on `origin/main`, reruns the full verifier,
  streams the exact commit archive, checks database role/schema and idle OCR/SRO
  queues, creates a backup, and atomically switches the release pointer. A
  failed readiness check restores the previous pointer. Database downgrades
  are never run automatically; schema migrations must remain backward
  compatible with the previous app. Dependency changes fail closed pending a
  separately qualified environment build. Do not tag experimental branches.
- GitHub hosted Actions have failed before runner assignment on previous pushes;
  local full verification is the current release gate. The cloud candidate has
  not been merged to `main` or tagged as an official release.
