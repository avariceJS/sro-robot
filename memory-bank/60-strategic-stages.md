# Strategic engineering stages

## Purpose and working model

This is the durable product-development map from the current MVP to a qualified legal-office platform for 20+ staff and large daily batches. It defines capabilities and evidence gates, not sprint tasks or delivery dates. Keep current facts and the selected next horizon in `.now.md`; put stable architecture decisions in `50-decisions.md`.

Stages are capability areas, not a strict waterfall. Start bounded prototypes in dependent stages when they produce useful evidence, but do not enable a downstream external effect before its prerequisites are proven. Each workflow is a vertical slice through the applicable stages.

## Shared maturity scale

- **M0 — Unproven:** absent, assumed, or no reproducible evidence.
- **M1 — Prototype:** a narrow path exists; unit or manual evidence only; known gaps remain.
- **M2 — Bounded:** a supported scenario passes repeatable local integration/failure tests with explicit limits and review states.
- **M3 — Operational:** supervised use has recovery, access control, audit, runbooks and service-level evidence; users can diagnose and resume safely.
- **M4 — Qualified at scale:** production-like security, load, recovery and end-to-end evidence meets owner-approved workload and quality targets; rollout and change control are established.

Maturity is evidence-based and capability-specific. A stage is not complete because it contains code, has high test coverage, or has one successful demo. Report limitations and evidence date. Production qualification must state the supported document/operation set, throughput, concurrency and external-system assumptions.

## Stage map

| # | Global stage | Core capability | Current assessment | Dependency |
| --- | --- | --- | --- | --- |
| 1 | Platform Foundation and Control Plane | Governed data, identity, workflow contracts and safe change | **M1 partial** — FastAPI/PostgreSQL/Redis/RQ, approval digest, guarded Alembic baseline, synthetic populated-schema adoption/restore drill including duplicate-blocked unique-index migration rollback/retry, 100,000-task/document/event metadata and 100,001-file restore verification at exact `0600` with corruption/missing-file/unsafe-mode/special-mode/symlink rejection, and a long-lived feature-disabled worker process that reuses its pre-migration PostgreSQL backend connection across an additive migration retry; bounded readiness diagnostics, secret/CSRF/session controls, CSP and dynamic-response no-store headers, read-only queue diagnostics and schema comparison for index uniqueness/RLS/public extensions/routines, PostgreSQL-tested local OCR leases/reconciliation (disabled pending adoption), and a runtime/migration URL split required with distinct roles in production and tested with disposable roles; shared DB remains unversioned, task role/ownership policy is unresolved, the rolling rehearsal does not use a prior release binary, and production rollout/recovery/load evidence is absent | Enables every stage |
| 2 | Evidence Intake and Acquisition | Accept source material and obtain the right source documents with provenance | **M1 partial** — upload path and MKAD case-number path exist; MKAD returned an empty page in a live read-only attempt, and current pending adapter's first/latest shortcut is not a valid selection policy | 1 for durable intake; can prototype with fixtures in parallel |
| 3 | Legal Document Understanding and Identity | Determine act, operation, participant role and supported facts | **M1 partial** — local OCR and heuristic person-field extraction work on limited samples; role binding, act classification, source spans and calibrated quality are absent | 2 provides evidence; synthetic evaluation may run in parallel |
| 4 | Casework Orchestration and Human Review | Coordinate cases, operations, batches, workers and accountable staff decisions | **M1 partial** — task/review/approval flow exists; case packages, atomic claims, idempotent intake, collaboration controls and durable retry/recovery do not | 1–3 define durable input and identity contracts |
| 5 | SRO Execution and Reconciliation | Apply an approved operation once, establish outcome and prove the resulting record | **M1 simulation-only** — person-card scenario and selectors exist; live mutations are blocked; durable attempt/identity reservations, unknown-outcome reconciliation and read-back are absent | 1, 3 and 4; live actions additionally require verified business rules and explicit owner authorization |
| 6 | Production Reliability, Security and Scale | Operate, protect and recover the service under real office conditions | **M0–M1 partial** — local services and tests exist, including a synthetic disposable PostgreSQL restore drill; no production deployment, RBAC/security qualification, metrics/alerts, production backup-restore objectives, crash-recovery proof or 20+ user load evidence | Cross-cutting from Stage 1; qualification depends on complete vertical slices |
| 7 | Domain Coverage and Controlled Productization | Add supported legal entities and operations without rebuilding the platform | **M0–M1 foundation only** — scenario/profile seams exist, but verified delivery is limited to a physical-person debtor-card path | Shared contracts from 1–5; new scenarios can be prototyped early, with release gated by 6 |

## Stage responsibilities and maturity gates

### 1. Platform Foundation and Control Plane

Own service boundaries and contracts, PostgreSQL schema evolution, authenticated identity and authorization, task lifecycle and concurrency, secrets/configuration, local data protection, CI/reproducible environments, structured diagnostics and safe version changes. Keep `app -> sro_robot`; core extraction must remain independent of browser/network I/O.

- **M2:** versioned migrations are adopted safely by existing and fresh databases; lifecycle transitions and idempotency boundaries are explicit; authenticated actions enforce role and ownership; logs correlate a task without exposing document content; a synthetic vertical slice works after process restart.
- **M3:** access review, secret rotation, schema rollout/rollback policy, service readiness, worker supervision and incident procedures are exercised; database and queue failure behavior is bounded and diagnosable.
- **M4:** upgrades, restore and rolling process changes are rehearsed against production-like data volume with no unaccounted tasks or identity changes.

### 2. Evidence Intake and Acquisition

Own upload, batch admission, file validation, duplicate intake keys, immutable originals/checksums, source and retrieval provenance, MKAD navigation, complete candidate inventory, pagination and document selection handoff. A case number identifies a proceeding; it does not choose an act. Empty/challenged MKAD responses mean source unavailable, not no matching act. Do not bypass challenges or access controls.

- **M2:** individual PDF/image and case-number flows have replayable fixtures, explicit acquisition states, bounded size/time/page limits, and preserve original bytes plus source metadata; incomplete candidate inventories cannot be auto-selected.
- **M3:** batch ingestion is restartable and idempotent, queue backpressure is visible, source outages do not lose work, and operators can distinguish unavailable, incomplete and empty results.
- **M4:** measured large-batch throughput, candidate coverage and recovery meet agreed targets across representative site/document variations without unsafe browser parallelism.

### 3. Legal Document Understanding and Identity

Own local text-layer extraction/OCR, document and operation classification, participant/role binding, entity normalization, field provenance, validation, confidence calibration and the ambiguity boundary. Bind identity fields to one evidenced party; checksums and whole-document matches alone are not attribution. Never send personal documents or OCR text to generic cloud AI.

- **M2:** a declared document/operation set has a synthetic or safely redacted evaluation corpus, field-level source locations, measured precision/recall/error categories, deterministic validators and explicit `needs_review` outcomes; unsupported cases fail closed.
- **M3:** quality is monitored by document class/layout, regressions are reproducible, model/rule versions are traceable, and operators can correct facts without losing provenance or invalidating stale approvals silently.
- **M4:** owner-approved quality/error targets are met across real-world variation, with safe fallback behavior and controlled rule/model releases.

### 4. Casework Orchestration and Human Review

Own case/package/operation relationships, debtor-card-to-assignment links, queues, worker ownership and leases, retries, task uniqueness, staff assignment, review/edit/approval, audit events, bulk progress, cancellation and recovery. Model an assignment as a separate resumable business work item that may remain an internal draft while information is pending; do not make its lifecycle depend on holding a worker or browser session. Treat a batch as related durable work, not one long request. Separate operator wait time from browser/session resource ownership.

- **M2:** each accepted item has a durable state, correlation ID and deterministic duplicate/retry behavior; concurrent workers cannot claim one operation twice; approval is bound to the exact payload and actor; failures remain actionable; a local assignment draft can be resumed after a process restart without implying an external record.
- **M3:** multi-operator review, stale-job recovery, pause/cancel, escalation and audit history are exercised; operators can resume without guessing whether work was lost or repeated.
- **M4:** measured 20+ staff and large-batch workload meets agreed queue-age/latency targets with bounded database/worker contention and auditable workload outcomes.

### 5. SRO Execution and Reconciliation

Own SRO session isolation, WebForms adapters, versioned scenario-to-field mapping, read-only preflight, duplicate reconciliation, identity reservation, approved-payload binding, durable pre-save intent, unknown outcomes, semantic read-back and verified completion. Debtor-card creation and assignment creation are separate external operations; success of one must not imply success of the other. An internal assignment draft is never evidence of an external SRO draft. A click or URL change is not success. Never automatically repeat a possibly submitted write.

- **M2:** the full prepare/write/reconcile state machine passes against isolated fakes, including crashes before and after the submission boundary; real SRO access remains read-only or disabled.
- **M3:** an explicitly authorized non-production environment verifies selectors, session behavior, read-back semantics and recovery; operators can reconcile unknown outcomes without creating another record.
- **M4:** only after verified registry identity/cardinality, business rules, security review, reconciliation controls and separate explicit owner authorization: narrowly enabled production operations with a kill switch and ongoing read-back evidence. The current owner prohibition on real SRO changes remains in force for development agents.

### 6. Production Reliability, Security and Scale

Own threat modeling, authentication/authorization hardening, CSRF/session controls, document storage protection and retention, deployment, capacity, rate limits, queue isolation/backpressure, metrics, alerting, health/readiness, audit retention, backup/restore, disaster recovery and operator runbooks. This stage continuously constrains all others; it is not a final cleanup sprint.

- **M2:** structured safe logs, service/worker health, dependency/config controls and synthetic failure injection expose actionable diagnoses; basic backup and restore are repeatable locally.
- **M3:** security review, least privilege, key/secret lifecycle, retention/deletion, alerting, on-call/incident procedures and tested recovery objectives exist for the actual deployment.
- **M4:** load, soak, security and disaster-recovery evidence meets approved office workload and recovery objectives; residual risks and unsupported modes are accepted explicitly.

### 7. Domain Coverage and Controlled Productization

Own business-rule discovery and independently versioned workflows for natural persons, organizations, sole proprietors, appointments/assignments and later operations. Staff also identify the SRO areas Registers (SRO, legal entities/IPs, individuals, assignments, accreditations) and Reports (payments, insurance, checks, documents) as potential future scope; this inventory is not yet a set of automation specifications. Keep debtor-card identity, assignment, proceeding, operation and source act distinct. Reuse shared platform capabilities, not a universal person-card schema. Legal process rules require accountable office-owner confirmation and cited source evidence.

- **M2:** each added entity/operation has a written rule contract, realistic synthetic examples, role/identity checks, scenario-specific review fields and fake-platform execution tests. Assignment rules distinguish reported optional/pending fields from technically required fields and keep unresolved semantics out of hard validation.
- **M3:** the office validates each workflow against representative cases; exceptions, corrections/replacements and rule changes are auditable and supported by operator guidance.
- **M4:** a controlled rollout demonstrates stable daily use for the declared scenario catalog; unsupported/new scenarios are isolated instead of silently routed to the nearest existing workflow.

## Dependencies and parallel work

1. Stage 1 is the platform dependency for durable multi-user processing and every external write. Complete its safe minimum before adding irreversible effects.
2. Stages 2 and 3 form an evidence-to-understanding path. Their fixtures, OCR evaluation and source inventory can advance while Stage 1 hardens; no case-number workflow may infer a semantic act selection from recency.
3. Stage 4 depends on stable task, identity, provenance and approval contracts. Start its orchestration design early, but qualify concurrency only with real locking/recovery evidence.
4. Stage 5 depends on Stage 1 controls, Stage 3 identity/operation admission and Stage 4 durable approval/recovery. It stays simulation-only until all gates and owner authorization are met.
5. Stage 6 applies continuously. Production qualification is a release gate across the whole workflow, not a separate checkbox.
6. Stage 7 may develop vertical slices after shared contracts are stable. Each scenario must pass its own Stages 2–6 gates before release; one successful physical-person path does not qualify another entity or operation.

## Current strategic focus

**Primary: Stage 1 — Platform Foundation and Control Plane.** The Alembic baseline and pinned Python environment are initial evidence, not migration adoption, secure multi-user controls, or production operations. Prioritize safe schema lifecycle, explicit task/worker ownership and authorization/diagnostics before enabling downstream effects. Keep future data/workflow contracts capable of representing a debtor card separately from an assignment and of persisting a resumable internal draft without any external SRO side effect; implementation remains in later bounded work.

**Parallel bounded tracks:** Stage 2 should replace the pending KAD first/latest shortcut with a candidate inventory and operator-selection boundary using fixtures; live MKAD acquisition remains unverified. Stage 3 should expand only synthetic extraction/role-binding evaluation. Keep Stage 5 local-only. Use Stage 6 security/recovery review as a continuous acceptance lens; defer load claims until contracts stabilize.
