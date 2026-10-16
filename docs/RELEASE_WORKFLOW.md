# Release workflow

## Branch and checkout policy

Maintain only two shared branches:

- `main` is the production release line. When no release is in flight, its tip must match the commit reported by the production `/version` endpoint.
- `develop` is the sole integration branch. It is never deployed automatically and is not a production identity.

Use one repository folder: `/Users/root1/Desktop/sro-robot`. Keep the local environment, credentials, browser profiles, documents, and runtime data private and outside Git. Do not develop or release from another clone or worktree. Other branches are temporary review branches; the local `refs/archive/consolidation-20261016/` refs and the private original-ref manifest are recovery records, not shared branches and must not be pushed. The manifest is at `/Users/root1/.local/share/sro-robot-recovery/20261016/original-refs.json`; do not copy its contents into Git or release artifacts.

## Promotion and deployment

A release begins only after the owner explicitly authorizes it. Before that authorization, do not promote `develop`, create a release tag, or deploy.

1. Finish the change on `develop`. Review the exact diff, migration effects, security implications, and recovery behavior. Run the full exact-tree gate, `scripts/dev/verify.sh`, from the canonical checkout. Resolve failures before promotion. Keep real documents, OCR content, credentials, and service data out of test fixtures and release notes.
2. Confirm the release commit is the verified `develop` tip. Fast-forward `main` to that commit; never create a merge commit on `main`:

   ```bash
   git switch main
   git pull --ff-only origin main
   git merge --ff-only develop
   git push origin main
   ```

   If the fast-forward is not possible, stop and reconcile the branch history. Do not force-push or reset `main`.
3. Fetch the published tip and create an annotated version tag on exactly that commit. The tag format is `sro-vMAJOR.MINOR.PATCH`:

   ```bash
   git fetch origin main
   git tag -a sro-v1.2.3 -m "SRO Robot 1.2.3" "$(git rev-parse origin/main)"
   git push origin sro-v1.2.3
   git switch --detach sro-v1.2.3
   ```

   Substitute the owner-approved version. Do not tag `develop`, an unpublished commit, or a commit that differs from `origin/main`.
4. From the clean checkout at the exact tag, run:

   ```bash
   scripts/ops/deploy_vps.sh sro-v1.2.3
   ```

   The script rejects a dirty worktree, a lightweight tag, a tag whose commit is not `HEAD`, or a commit that does not equal the fetched `origin/main` tip. It verifies the dedicated SSH identity and reruns `scripts/dev/verify.sh` before it sends the exact source archive. Do not bypass these checks. The production GitHub workflow is also manual (`workflow_dispatch`), requires the workflow ref to be `main`, and requires the explicit `deploy` confirmation; there is no `develop` auto-deploy path.
5. Read back the deployed release through the configured office origin's `/version` endpoint and compare its 40-character `commit` value with the tagged commit and `origin/main`. Check `/readyz` as well. `app/main.py` returns the release marker from `.release-commit`; a development tree without that valid marker reports `development`. Record the comparison and readiness outcome in the normal private release record. A timeout, mismatch, or unavailable endpoint means release verification is incomplete; do not infer success from the deploy command alone.

`main` is the production identity when the read-back matches. If activation rolls back or read-back does not match, stop further releases and reconcile the Git production tip with the observed `/version` through an owner-authorized forward-only release. Deployment rollback changes the VPS pointer and database, not Git refs.

## Database checkpoint and rollback boundary

`deploy_vps.sh` delegates activation to `scripts/ops/activate_vps_release.sh`. Activation first checks the current release's schema and requires empty OCR, SRO, and preview queues, no started/deferred/scheduled work requiring reconciliation, and only the expected registered workers. It then enables ingress maintenance and stops the web, OCR, preview, and health writers before the final backup and migration.

The root-only `scripts/ops/release_database.py snapshot` command requires the maintenance marker and stopped application writers. It creates a private, commit-specific checkpoint under `/var/backups/sro-robot`, hashes the PostgreSQL table rows and sequence state before and after `pg_dump`, restores the dump into a temporary network-disabled PostgreSQL container, compares table and sequence signatures, and only then writes `verified.json`. The database checkpoint is separate from document and Redis backups; it does not replace either store.

If candidate activation fails after switching releases, the activation script stops candidate services, restores the verified database checkpoint, switches the release pointer back, and checks readiness. If database restore or old-release readiness fails, ingress remains in maintenance. Never manually delete that marker, replay uncertain jobs, downgrade a schema, or replace document/Redis data as an improvised rollback. Follow the recorded failure state and reconcile it before reopening traffic.

## Release record

For each authorized release, record the owner authorization, verified `develop` commit, `main` fast-forward and published tip, annotated tag, deployment command/result, `/version` commit, `/readyz` result, checkpoint location and verification status, and any rollback outcome. Keep database manifests, operational logs, credentials, personal data, and original-ref manifest contents in private storage. Do not claim production parity until the read-back commit equals the intended release commit.

## GitHub service constraints observed during consolidation

GitHub Actions refused the earlier jobs before any step started because account
payments failed or the spending limit was exhausted (provider annotation). This
cannot be repaired by changing application code. The production environment is
restricted to `main`, with an environment-scoped deploy key; the workflow still
requires an explicit manual confirmation. Do not claim it ran until the billing
restriction is cleared and a new run succeeds.

The same checked-in `deploy_vps.sh` is the supported local execution path while
Actions is unavailable. It performs verification, exact archive transfer,
independent environment creation, checkpoint/migration and activation itself;
no manual file copying is required. GitHub branch-protection access returned403
on the current private repository plan; do not claim server-enforced branch
protection. The exact-main/tag checks and environment branch policy remain active.
