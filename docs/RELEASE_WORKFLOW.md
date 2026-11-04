# Release workflow

## Branch and checkout policy

Maintain only two shared branches:

- `main` is the production release line. When no release is in flight, its tip must match the commit reported by the production `/version` endpoint.
- `develop` is the sole integration branch. It is never deployed automatically and is not a production identity.

Use one repository folder: `/Users/root1/Desktop/sro-robot`. Keep the local environment, credentials, browser profiles, documents, and runtime data private and outside Git. Do not develop or release from another clone or worktree. Keep only these two local and remote branch heads. The local `refs/archive/consolidation-20261016/` refs and the private original-ref manifest are recovery records, not shared branches and must not be pushed. The manifest is at `/Users/root1/.local/share/sro-robot-recovery/20261016/original-refs.json`; do not copy its contents into Git or release artifacts.

## Current operator access (2026-11-04)

The verified AEZA service is `657389`, `root@82.148.241.37`
(IPv6 `2a14:a084:17be::2`). The operator Mac alias `ssh sro-office-vps`
uses the existing dedicated `~/.ssh/sro-office-codex` identity and strict
host-key verification. No password is needed for ordinary deployment.
The current server ED25519 fingerprint is
`SHA256:waebQvkyPJUAjcQ30bK3FnHViZ0vckSoN4YdoizBF1w`.
The checked-in host pin and the develop workflow assertion use this identity;
the GitHub production host variable is `root@82.148.241.37`.
These workflow source changes require normal promotion before a future CI
release; no hosted CI deployment was run for the initial installation.

The server has a dedicated read-only GitHub deploy key and pinned GitHub
host keys. After verification and normal main/tag publication, the existing
pull helper supports a release from the operator Mac:

```bash
ssh sro-office-vps 'bash /root/deploy_vps_pull.sh NEW_RELEASE_TAG FULL_RELEASE_COMMIT'
```

Substitute a new annotated release tag and its full main commit. The helper
refuses an existing release directory; do not rerun the current installed tag.
Its reviewed SHA-256 is
`5c8544836b60120e8c8ae6e24225b148ac2c6a3c05751a8f7bae5c88c1626617`.
Retain the verification profile, exact-ref, database checkpoint, activation and
HTTPS readback requirements below. This access setup does not authorize an
unrequested release or automatic deployment.

The owner authorized a fresh empty database for the initial `sro-v1.3.0`
installation. Initial schema/runtime guards and an isolated PostgreSQL
checkpoint restore passed. Operator evidence and the one-time reviewed bootstrap
are private under `/root/sro-install-evidence`; future updates use the normal
release helpers, not that bootstrap. Daily backups stay private on the VPS.

Cloudflare apex A/AAAA are DNS-only and point to this server. Certificate
renewal now uses Certbot HTTP-01 webroot at `/var/www/html`, with a scoped
Nginx reload hook and active timer. Renewal dry-run including the hook passed.
No Cloudflare API token is required for this webroot setup. Preserve the
HTTP challenge location when updating Nginx.

During activation the Mac VPN path through `utun7` returned a certificate for
`de3.m-ui.ru`. Verified HTTPS over Wi-Fi with `curl --interface en0` returned
exact release identity, healthy readiness and HTTP 200 login. Do not disable
certificate checking to work around this path; VPN routing changes require
owner agreement. This observation does not establish a global routing outage.

## Promotion and deployment

A release begins only after the owner explicitly authorizes it. Before that authorization, do not promote `develop`, create a release tag, or deploy.

1. Finish the change on `develop` and choose a verification profile by risk.
   For the **fast lane**, use the criteria and checks in “Fast lane for routine
   releases” below. For a **full lane** change, run `scripts/dev/verify.sh`
   from the canonical checkout and export the exact-tree evidence:

   ```bash
   python3 scripts/ops/release_gate_evidence.py --commit "$(git rev-parse develop)"
   ```

   The evidence command requires `HEAD` and `origin/develop` to equal the
   candidate. It runs the 100,000-task exact verifier, streams its output, and
   writes a mode-0600 JSON record under the local temporary directory. Preserve
   the emitted `source_commit`, `source_tree`, and
   `verification_log_sha256` values with the release record. A failed,
   interrupted, or changed checkout emits no pass record. Keep real documents,
   OCR content, credentials, and service data out of tests and release notes.
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
   ```

   Substitute the owner-approved version. Do not tag `develop`, an unpublished commit, or a commit that differs from `origin/main`.
4. From the clean checkout at the exact tag, run:

   ```bash
   scripts/ops/deploy_vps.sh sro-v1.2.3
   ```

   The script rejects a dirty worktree, a lightweight tag, a tag whose commit is not `HEAD`, or a commit that does not equal the fetched `origin/main` tip. It verifies the dedicated SSH identity and reruns `scripts/dev/verify.sh` before it sends the exact source archive. Do not bypass these checks. The production GitHub workflow is also manual (`workflow_dispatch`), requires the workflow ref to be `main`, and requires the explicit `deploy` confirmation; there is no `develop` auto-deploy path.
5. Read back the deployed release through the configured office origin's `/version` endpoint and compare its 40-character `commit` value with the tagged commit and `origin/main`. Check `/readyz` as well. `app/main.py` returns the release marker from `.release-commit`; a development tree without that valid marker reports `development`. Record the comparison and readiness outcome in the normal private release record. A timeout, mismatch, or unavailable endpoint means release verification is incomplete; do not infer success from the deploy command alone.

6. After successful parity/readiness verification, return to the normal working branch with `git switch develop`. Do not start further development as part of the release mission.

`main` is the production identity when the read-back matches. If activation rolls back or read-back does not match, stop further releases and reconcile the Git production tip with the observed `/version` through an owner-authorized forward-only release. Deployment rollback changes the VPS pointer and database, not Git refs.

### Fast lane for routine releases

For every release, review the exact diff and run tests that directly cover the
changed behavior, plus syntax checks for changed code and `git diff --check`.
This is the fast lane: use it for UI, OCR extraction rules, authentication and
SRO boundary changes when focused unit/integration coverage exists. For OCR or
identity decisions, that means the focused synthetic fixtures and relevant
workflow tests; never use real office documents or records as fixtures. Record
the exact commands, results, full candidate commit, and test scope. Do not claim
the full 100,000-task gate passed when it was not run.

Run the full-volume/recovery lane when a change affects database schema or
migrations, task admission/lifecycle persistence, queues/retries, concurrency
or idempotency guarantees, runtime roles, backup/restore, VPS provisioning or
activation, dependencies that alter the runtime, a cross-cutting refactor, or
when there is no focused test for the affected behavior. This lane remains
`scripts/dev/verify.sh` plus `release_gate_evidence.py` on the exact candidate.
If scope or impact is uncertain, use the full lane. The 100,000-task run is a
scale/recovery gate for these changes, not a routine prerequisite for each
small product update.

The ordinary `scripts/ops/deploy_vps.sh` deliberately reruns the full verifier;
it is therefore a full-lane command. For a fast-lane release through the
configured AEZA console, use the VNC GitHub pull transport below after running
the focused checks. The pull helper does not run the 100,000-task suite, but it
still validates that the fetched annotated tag, fetched `main`, and supplied
full commit are identical. Provisioning retains its production database
checkpoint, migration/activation guards, service readiness checks, and final
HTTPS read-back. Never remove or bypass those deployment safeguards to meet a
time target.

When the VPS already has the previously reviewed `/root/deploy_vps_pull.sh`,
first compare its SHA-256 with the last approved helper hash recorded in
`memory-bank/.now.md`. If it matches, the fast deploy operation itself is one
VPS command after `main` and the annotated tag are published:

```bash
bash /root/deploy_vps_pull.sh sro-vMAJOR.MINOR.PATCH FULL_40_CHARACTER_COMMIT
```

The helper fetches and validates the exact refs before deployment. If the
helper hash differs, do not run the unknown copy: bootstrap the helper from the
exact release commit using the steps in “VNC-only GitHub pull transport”, review
its hash, and then deploy. The actual duration still depends on dependency
changes, database size, and migrations; the safety checkpoint is retained.

### Fast VPS deployment through the open Safari console

Use this route when the VPS is reachable through the owner's AEZA VNC console
and local SSH or GitHub Actions is unavailable. For a fast-lane release, run
the focused checks above; for a full-lane release, export the full exact-tree
gate record. Neither profile skips explicit release authorization, promotion
to `main`, the annotated tag, database checkpoint, or production read-back.
Never deploy `develop` directly.

1. On `develop`, finish and review the change. Complete the selected profile
   above and confirm the checks apply to the exact commit being released.
   After owner authorization, fast-forward and push `main`, publish the
   annotated release tag, and record the tag plus full 40-character commit.
2. Check out the exact tag in the canonical local checkout. For a full-lane
   release with SSH available, the checked-in deployment command is:

   ```bash
   scripts/ops/deploy_vps.sh sro-vMAJOR.MINOR.PATCH
   ```

   It reruns the full verifier and sends the exact tagged archive. For a
   fast-lane release, use the VPS pull helper below; `deploy_vps.sh` will
   rerun the full suite. Keep the Safari tab on the VPS console visible. Put one
   command at a time in Safari's address/search field as clipboard scratch;
   copy it, paste it into the VPS terminal, and press Enter there. Inspect the
   complete output before preparing the next command. Do not execute commands
   in Safari's address field. This preserves command boundaries and makes
   errors visible before continuing.
3. Compare `/root/deploy_vps_pull.sh` on the VPS with the approved helper hash
   in `memory-bank/.now.md`. If it matches, run the command below directly. If
   it differs, bootstrap the helper from the exact release commit using the
   commands in “VNC-only GitHub pull transport”, review its SHA-256, then run
   the same command. Substitute the actual values:

   ```bash
   bash /root/deploy_vps_pull.sh sro-vMAJOR.MINOR.PATCH FULL_40_CHARACTER_COMMIT
   ```

   The helper fetches `main` and the annotated tag from GitHub and aborts unless
   both resolve to the supplied commit. It creates the immutable release and
   invokes the shared provisioning/activation helper, which checkpoints the
   database and performs migration, service activation, and readiness checks.
   Do not hand-copy application files or run provisioning steps manually.
4. From a trusted local shell, read back `https://sro-office.ru/version` and
   `https://sro-office.ru/readyz`. Require the returned commit to equal the
   release tag commit and `origin/main`, and require all readiness checks to
   pass. Record the helper output, checkpoint result, commit comparison, and
   readiness result. A completed shell command alone is not proof of a live
   release.

If any command fails, the output is incomplete, or the endpoints time out or
disagree, stop and reconcile the observed state before retrying. Never repeat a
deployment blindly: the pull helper intentionally refuses to replace an
existing release directory.

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
no manual file copying is required. GitHub returned 403 for the branch-protection endpoint; server enforcement
was not confirmed. Do not claim server-enforced branch protection. The exact-main/tag checks and environment branch policy remain active.

### VNC-only GitHub pull transport

When the canonical checkout cannot reach the VPS over SSH, use the checked-in
VPS pull helper from VNC as a transport alternative. This does not replace the
local exact gate or release authorization. First run the local gate export
above and promote only its exact candidate. Keep the private JSON result with
the release record. It is operator evidence, not a cryptographic deployment
credential. The VPS pull helper independently requires the fetched annotated
tag, fetched `main`, and supplied full commit to agree. GitHub Actions billing
status does not change these checks.

The root VPS identity `/root/.ssh/sro-robot-deploy` must be a read-only GitHub
deploy key and GitHub's host key must already be pinned in
`/root/.ssh/known_hosts`. Never disable host-key checking. Fetch uses a private
temporary bare repository, fetches only the named release tag and `main`, and
creates a source archive only after verifying the exact commit. The shared
`provision_vps_release.sh` helper then performs the same environment setup,
systemd/Nginx changes, backup timer start and activation as the SSH transport.
Do not bypass the helper or manually recreate its operations.

Bootstrap helpers through the existing read-only deploy key, not raw HTTPS
(the repository is private). In a root shell, create a private temporary bare
repository, fetch the pinned ref, and extract the helper at the exact commit:

```bash
umask 077
work=$(mktemp -d /root/sro-helper.XXXXXX)
git init --bare "$work/repo.git"
GIT_SSH_COMMAND='ssh -i /root/.ssh/sro-robot-deploy -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/root/.ssh/known_hosts' \
  git --git-dir="$work/repo.git" fetch --no-tags git@github.com:avariceJS/sro-robot.git \
  '+refs/heads/develop:refs/remotes/origin/develop'
test "$(git --git-dir="$work/repo.git" rev-parse refs/remotes/origin/develop)" = <full-develop-commit>
git --git-dir="$work/repo.git" show <full-develop-commit>:scripts/ops/stage_vps_candidate.sh > /root/stage_vps_candidate.sh
sha256sum /root/stage_vps_candidate.sh
```

Compare that hash to the locally reviewed helper hash before running it. For
the final deploy helper, fetch both `refs/heads/main` and the exact published
tag into the temporary bare repo, then check and extract:

```bash
GIT_SSH_COMMAND='ssh -i /root/.ssh/sro-robot-deploy -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/root/.ssh/known_hosts' \
  git --git-dir="$work/repo.git" fetch --no-tags git@github.com:avariceJS/sro-robot.git \
  '+refs/heads/main:refs/remotes/origin/main' '+refs/tags/<release-tag>:refs/tags/<release-tag>'
test "$(git --git-dir="$work/repo.git" cat-file -t refs/tags/<release-tag>)" = tag
test "$(git --git-dir="$work/repo.git" rev-parse 'refs/tags/<release-tag>^{commit}')" = <full-release-commit>
test "$(git --git-dir="$work/repo.git" rev-parse refs/remotes/origin/main)" = <full-release-commit>
git --git-dir="$work/repo.git" show <full-release-commit>:scripts/ops/deploy_vps_pull.sh > /root/deploy_vps_pull.sh
sha256sum /root/deploy_vps_pull.sh
```

Compare fetched commits and helper SHA-256 with the locally reviewed values
before execution. Do not put a PAT/password in the command or disable
host-key checking. The helper fetches and validates `main` and the tag again
before creating the release directory; it never deploys `develop`.
The existing `deploy_vps.sh` path still reruns the exact local verifier and
uses the shared provisioning helper.

Before promotion, a pinned develop candidate may be staged for isolated
rehearsal only:

```bash
printf '<locally-reviewed-sha256>  /root/stage_vps_candidate.sh\n' | sha256sum --check
bash /root/stage_vps_candidate.sh <full-develop-commit>
```

This creates a root-only source tar and hash manifest under
`/var/backups/sro-robot/candidate-staging`; it does not create a release, venv,
service configuration, or activation. Use it only as input to the isolated
clone rehearsal. Never activate a develop archive. For final deployment,
bootstrap `deploy_vps_pull.sh` the same way from the exact published tag commit,
verify it against the local SHA-256, then pass the tag and full commit to it.

## Retained infrastructure and recovery archives

The VPS has no development checkout. `current` selects one immutable release;
previous release directories and the old global venv remain available for rollback.
The separate `/opt/sro-robot/deploy/docker-compose.dev.yml` and private root `.env`
are live infrastructure configuration, even though the Compose filename is
historical. Do not remove them during source cleanup. Changes to infrastructure
images/volumes require their own reviewed migration plan rather than silently
replacing this file in an application release.

Historical Git tips, owner WIP/evidence and private worktree runtime files are
retained under the recovery locations recorded in the consolidation inventory.
These archives must never be pushed to GitHub. Inspect and restore only the
required artifact in a protected recovery directory; do not recreate historical
branches as permanent development branches. A venv moved out of its original
path is a recovery artifact, not an environment to execute in place.
