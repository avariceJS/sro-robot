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
- Owner authorization (2026-11-07): the owner, acting as a company developer,
  states that permission was obtained for bulk acquisition of published KAD acts
  for internal research and OCR/extraction improvement. This is an owner
  assertion; no written authorization document has been supplied or verified.
  It supersedes the previous research-only single-document acquisition hold.
- Use a sanctioned internal export, API or the existing ordinary KAD integration;
  prefer direct original PDFs when that channel works. Establish channel limits
  before sustained acquisition. Do not describe a self-imposed exploratory rate
  as an operator-agreed quota. Never bypass CAPTCHA, authentication, throttling,
  access blocks or other technical controls. Pause/cool down and resume durable
  queues after transient failures; persistent restrictions disable that adapter,
  not independent local research. Historical production acquisition jobs stay
  withheld; this authorization concerns isolated local corpus research.
- Start with 100–200 diverse acts, then expand in measured batches to hundreds
  and thousands as source, disk and processing budgets permit. There is no final
  200-document corpus ceiling. Keep case groups and duplicate derivatives in one
  split; previously tuned cases cannot become independent control. PDF text is
  a preliminary/weak reference until independently checked. Synthetic camera
  distortions and genuine photographs are distinct categories. Never treat OCR,
  parser or agent outputs as verified field/participant truth.
- SROArbitr uses WebForms: use reviewed IDs from platform code, reacquire locators
  after postbacks and preserve provenance/candidate-selection reasons. A case
  number or the first/latest visible document alone is not an act selection proof.

## Engineering and verification

Current owner strategy: develop, improve OCR/extraction and measure performance
locally on the Mac. Use the existing Ubuntu CI for functional checks. VPS use,
final Ubuntu deployment and Ubuntu performance qualification are a separate final
stage requiring owner authorization. Owner authorization (2026-11-13) permits a
separate expert-review deployment to Aeza service 666936, including private
document/capture transfer and durable expert corrections. This exception does not
authorize deployment of the main SRO Office application, production database
changes, SROArbitr writes or historical production acquisition jobs. Protect the
reviewer with HTTPS and authentication; keep private state outside source and
retrieve feedback over authenticated transport. Mac-specific acceleration must
retain a correct portable fallback.

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

## CLEAN-AS-YOU-GO

Keep only current code, used components, necessary tests/scripts/dependencies and
active documentation. After every completed experiment, architecture change or
technology rejection, clean up automatically within the authorized scope:

- Remove rejected implementations, experiment-only scripts/tests/fixtures/config,
  obsolete Markdown, unused functions, superseded code and dangling references.
- Remove models, private temporary environments, caches and measurement output
  downloaded/created solely for the ended experiment. Never clear shared caches
  or dependencies belonging to other projects. Preserve original `samples`, user
  uploads, databases, secrets and required configuration.
- In mixed-purpose files, retain the useful supported part and remove only the
  obsolete part. Preserve required Tesseract/native tools when actually used.
- Keep integrity guards, migrations, recovery tools and valid tests, including
  infrequently used ones. Cleanup must not weaken the working product.
- Do not create archive/backup/legacy folders for rejected work: Git is history.
- Run checks appropriate to the retained behavior, selectively commit and push
  the clean result to `develop`, and read back the published commit. Work is not
  complete while unneeded implementation artifacts remain or changes are local.
