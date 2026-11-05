# Repository agent instructions

## Product and evidence

SRO Office processes court documents locally for staff review, internal debtor
cards and assignments, KAD browser imports and guarded SROArbitr preparation.
Keep implemented behavior separate from targets and unqualified external actions.
Read `memory-bank/.now.md`, then `memory-bank/00-start-here.md` and only relevant
current documentation. Check Git, runtime and existing work before changing code.
Preserve user edits. Code, configuration and current requirements outrank old tests
or reports. Do not add cosmetic work unrelated to the requested outcome.

## Git and publication

- Use `/Users/root1/Desktop/sro-robot` as the sole checkout.
- Keep exactly two local and shared branches: `main` and `develop`.
- Develop in `develop`. Additional branches, clones or worktrees require a
  separate explicit owner instruction.
- Each completed stage requires appropriate verification, a selective meaningful
  commit, push to GitHub and read-back of the published commit. Do not leave
  completed work only locally. Never broadly stage unrelated edits.
- Promote verified source to `main` only when authorized. An authorized branch
  consolidation can put `main` ahead of the deployed release; it is not deployment.
- Never force-push or rewrite history to reduce size. Private `refs/archive/*`
  preserve historical recovery commits; they are not active branches and stay local.
- Deployment remains a separate owner-authorized operation under
  `docs/RELEASE_WORKFLOW.md`; never auto-deploy `develop` or bypass tag/checkpoint gates.

## Data and external actions

- Never create, change, submit, delete or save a real SROArbitr record while
  developing/testing. Do not click Save/Send or bypass the client's write guard.
- The separately authorized blank-person prepare-only path is Add, approved-field
  fill, inspection, Close without saving. It does not authorize existing-record
  edits, nested address actions, assignment Add, or an uncertain retry.
- Reconcile an unknown external outcome read-only before any retry. Filling or
  closing a form is not persistence or verified completion.
- All document OCR/extraction stays on our infrastructure. No documents, OCR,
  identifiers or credentials in cloud AI, issues, logs, Git or real-data fixtures.
- Preserve `samples`, user uploads, databases, secrets and required configuration.
  Never print/read `.env` contents; use synthetic values in tests. Keep browser
  profiles private. Do not stop unrelated services or clear shared caches.
- Do not bypass CAPTCHA/access challenges. The active KAD path is the ordinary
  browser helper plus local PDF import. Historical acquisition jobs remain withheld.
- SROArbitr uses WebForms: use reviewed IDs from platform code, reacquire locators
  after postbacks and preserve provenance/candidate-selection reasons. A case
  number or the first/latest visible document alone is not an act selection proof.

## Engineering and verification

Use durable state, idempotency, bounded concurrency, audit events and recovery.
Test current behavior and material failure boundaries; old passing tests alone do
not establish a requirement. Preserve independent review for integrity, migration,
concurrency, identity, external-effect and publication boundaries. Use Luna High
for routine execution and Sol High for those decisions when delegating.

Keep technical documentation in English and owner communication in Russian.
Update the compact `.now.md` after meaningful work; do not append project history.
Keep temporary output ignored and bounded. After successful verification retain
commands and compact evidence; do not commit streams, snapshots or completed-stage
reports. History belongs in Git, not new archive/legacy/backup directories.
Do not request routine engineering approval within the owner's authorized scope.
