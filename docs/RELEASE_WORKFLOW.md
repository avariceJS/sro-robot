# Source and release workflow

## Two branches

Use the sole `/Users/root1/Desktop/sro-robot` checkout and exactly two local/shared
branches: `develop` for work, `main` for verified release source. Additional
branches/worktrees require separate owner instruction. Preserve unique useful
changes before deleting a branch; never force-push or rewrite history.

Each completed development stage is tested, selectively committed, pushed to
`origin/develop` and verified through a fresh GitHub ref read-back. Main promotion
requires owner authorization; an explicitly authorized source consolidation may
synchronize main/develop without deploying. Production identity is the deployed
release marker returned by HTTPS `/version`, not an assumption about main's tip.
Private archive refs are recovery history, not shared branches; never push them.

## Verification and promotion

For focused behavior changes run relevant product checks, syntax and diff checks.
For schema, queues, persistence, concurrency, roles, dependencies, recovery,
provisioning or cross-cutting changes run `scripts/dev/verify.sh` and the exact-tree
release evidence gate. Tests use synthetic data/disposable services, never office
records. Do not claim a gate passed if it was not run.

For an authorized full release after publishing the verified develop candidate:

```sh
python3 scripts/ops/release_gate_evidence.py --commit "$(git rev-parse develop)"
git switch main
git pull --ff-only origin main
git merge --ff-only develop
git push origin main
```

Stop and reconcile if fast-forward is impossible. Verify origin/main on GitHub,
then create an annotated owner-approved `sro-vMAJOR.MINOR.PATCH` tag exactly there.
Do not tag an unpublished/dirty/different tree. `main` promotion alone is not a
production deployment and does not authorize creating external SRO records.

## Manual deployment

`.github/workflows/deploy-production.yml` is manual-only: main ref, exact annotated
release tag and explicit deploy confirmation. Push workflows only verify/audit.
Do not change this boundary as part of routine cleanup.

The standard controlled transport is:

```sh
scripts/ops/deploy_vps.sh sro-vMAJOR.MINOR.PATCH
```

It validates clean/exact source, SSH identity, main/tag parity and verification,
then uses the provisioning/activation helpers with a database checkpoint. Keep all
those checks. Use only the configured intended host, pinned SSH key and valid HTTPS;
network reachability or an open SSH port alone does not establish server identity.

When the installed VPS pull helper is the reviewed current copy, an operator can
use the pinned SSH alias instead of copying a new source archive:

```sh
ssh sro-office-vps 'bash /root/deploy_vps_pull.sh RELEASE_TAG FULL_40_CHARACTER_COMMIT'
```

First compare the installed helper with the approved helper from the exact source.
If it differs, inspect/bootstrap through the reviewed workflow; do not run an
unknown copy or repeat an existing release directory. The pull helper checks the
fetched annotated tag/main/commit, provisions a fresh immutable release and keeps
checkpoint/migration/activation safeguards. Consult scripts' help for parameters;
credentials remain private. Never expose them in a console transcript.

After activation compare HTTPS `/version` commit with the exact release tag and
published main, and check `/readyz`. A timeout/mismatch leaves deployment unverified.
If activation rolled back, reconcile source versus observed runtime through an
authorized forward release; do not force-reset Git or retry an unknown operation.
Record compact release/checkpoint evidence privately and return to develop.

Backups, restore, schema-role boundaries, stored-document validation and service
manager units in `scripts/ops` and `deploy/cloud` remain required for operations.
Do not change server files or office databases simply to clean the repository.

The supported Nginx template preserves the HTTP-01 ACME webroot location.
Retain `reload_nginx_after_certbot.sh` as the bounded renewal reload hook.
No Cloudflare credential/DNS delegation component is required by this template.
