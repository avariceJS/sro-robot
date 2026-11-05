# Current system

`app` depends on `sro_robot`; the core has no database/browser dependency. FastAPI
serves Jinja/HTMX and static JS. PostgreSQL stores documents, intake operations,
tasks, cards, assignments, sessions, provenance and append-only revision/audit
records. Redis/RQ provides OCR and guarded preview execution; a PostgreSQL outbox
survives dispatch failure. Alembic history remains required for existing installs.

Current flow: upload or explicit KAD import → durable source/task → local reader
→ parser fields and review suggestions → employee correction/approval → internal
card and explicit assignment link. Internal persistence is separate from every
external SRO operation. Manual cards support person, legal entity and entrepreneur;
assignments can remain incomplete and resumed later. Case packages have explicit
membership, not inferred grouping from a case number.

All active staff share ordinary work visibility. Attributable events, approval
snapshots, row locks, versions and lease fencing protect mutations; do not replace
these with per-task visibility rules or assume they prove business identity.
Worker claims and duplicate deliveries are separate from staff edit conflicts.
Task transitions and managed database constraints/triggers are checked by readiness
and schema diagnostics; a database owner can still disable them.

Authentication stores only a hash of the local session token. SRO session state
is strictly origin-validated and encrypted per employee/login session; logout
revokes local reuse. Do not log credentials, field values or browser storage.
Readiness tests database/schema and queued-work coverage; health only proves the
web process responds. Neither proves office extraction accuracy or external saves.

External adapters preserve owned-form identity, exact source/candidate provenance,
request admission, bounded waits and unknown-result holds. Synthetic safety tests
do not authorize real SRO actions. See `30-integrations.md` and current source.
