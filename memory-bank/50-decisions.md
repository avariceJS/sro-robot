# Durable engineering decisions

This file records accepted constraints. Reopen a decision only when new evidence materially changes its assumptions or the owner changes the business requirement.

## Data and external actions

- Keep OCR/extraction local because documents contain personal data. A future ML extractor must run locally or use a provider explicitly approved by the owner; never send source documents or OCR text to a generic cloud model.
- Human review is required for uncertain legal data. Do not invent unreadable fields.
- Real SRO mutations are prohibited for agents, including `--live`, Save, Send, deletion and update. Tests use isolated fakes. This prohibition is stronger than the current code's explicit live flags.
- The current build fails closed for all live writes. `live=False` is local validation only: no SRO browser, registry search, form fill, Add, Save, or Close action is permitted. Its audit event is `local_validation_completed` with `saved: false`; this state must never be presented as a saved/verified registry record. Form-preparation tests require a local fixture or fake.
- `SroClient` state-changing controls are disabled by default. Do not enable them in this deployment or use a real registry for tests. Historical dry-run completions without the new local-validation marker remain unverified and are not retry authorization.
- Do not automatically resubmit an SRO write after a durable pre-Save intent marker. Keep the attempt `write_result_unknown` until read-only reconciliation and semantic verification prove an outcome. Empty search does not establish failure.
- Successful Save must not be inferred from click success, current URL or disappearance of a form. Reopen the record and compare identity plus approved fields.
- Reserve tenant/registry + debtor identity + create operation across tasks. A task-level duplicate check is not sufficient. Local reservations cannot guarantee no race with independent humans/clients.

### Debtor identity and creation reservations

- High-impact identity decision (2026-10-10): automatic creation requires a valid natural-person INN with explicit debtor-role attribution and operator approval bound to that payload. FIO, date of birth, and case number are review signals only; they are not automatic reservation keys. Missing, invalid, conflicting, or ambiguously attributed INN blocks creation.
- Before write enforcement is enabled, confirm whether one debtor has one reusable card per SRO registry realm, and determine which accounts share that realm. The business cardinality and registry scope are currently unknown, so live creation remains disabled.
- When implemented, reservation keys must be a versioned HMAC-SHA-256 token using a dedicated secret and domain separator, with atomic uniqueness on `(target_scope, identity_scheme, identity_token)`. Do not store or log raw identifiers in reservation records. Key rotation must not permit duplicate reservations.
- Keep one shared unresolved registry scope until account-to-registry boundaries are verified. Unresolved scope, existing submission, duplicate candidate, or identity ambiguity blocks creation; human review may reconcile an existing record but cannot silently authorize a second creation.
- Owner-provided examples may already exist in SRO. They may still be used for local OCR, extraction, review, and no-SRO validation. Never bypass a duplicate finding to create or submit a real record; existing-record status does not weaken the external-action prohibition.

### Judicial-act selection from MKAD

- Never select the first or latest visible act as an implicit match. A case may contain petitions, scheduling orders, appointment acts, final decisions, corrections and later procedural events. Publication order and generic `определение` labels do not establish operation relevance.
- First establish an explicit target operation. Build a complete bounded candidate inventory with document/event ID, title/type, act and publication dates separately, source URL, case linkage and pagination completeness. Limit downloads to five candidates per operation and ten per submitted package; exceeding either budget or incomplete inventory requires review rather than selecting a prefix.
- Metadata may narrow candidates but cannot establish that an act supports the requested operation. Local content checks must verify operation-specific dispositive text and party linkage, preserve source snippets/pages for material fields, and detect conflicts, corrections and replacements.
- Automatic selection is permitted only when the inventory is complete, case/subject match, one approved operation rule is satisfied, all required fields have source evidence, OCR uncertainty is resolved, and no competing or superseding candidate remains. Without a confirmed act-priority rule, multiple plausible acts require operator review.
- A package may contain separate operations supported by separate acts. Record each operation-to-act decision independently, including rejected candidate reasons and operator decisions. MKAD unavailable/empty page means source unavailable, not no matching document.
- Current product rules for initial card basis, manager appointment/replacement, corrections and act priority are not yet confirmed. Until they are, case-number intake must not automatically treat a latest act as selected; real SRO mutations remain prohibited.

## Workflow architecture

- Model intake as document/task work with scenario-defined fields; do not make a physical-person card the universal domain object.
- Staff-reported process confirms that a debtor card and an assignment are distinct business entities and actions: create/find the debtor card first, then separately create an assignment linked to that card. Do not infer assignment creation from card creation or assume one assignment per debtor/case.
- An internal SRO Robot assignment draft is a resumable local work item, not an SROArbitr draft or successful external action. Keep internal draft, readiness/approval, external attempt, unknown result, externally saved and semantically verified states distinct. Preserve the link to the existing debtor card and assignment-specific fields/provenance. These are target architecture constraints; no assignment model or lifecycle is implemented yet.
- OCR is stateless and can use a measured worker pool. SRO WebForms has page/session state; serialize per SRO account/session until isolation is proven.
- Preserve source, candidate act list, selection rationale, extracted-field provenance, operator edits/approvals, attempt identity, errors and verified result.
- Fail closed on unsupported act types, ambiguous participant identity, CAPTCHA/access challenge, invalid or mismatched case identifiers, and unknown external outcomes.
- Keep dry-run, saved and verified results distinct.

## Operator approval

- Approval binds to an exact field payload. Implemented as SHA-256 over canonical JSON in the `approved` audit event; field edits invalidate it; worker startup rechecks it. Older approvals without a digest require re-approval.
- Approval does not prove identity, eliminate concurrent task races, or authorize any real write by an agent.

## OCR worker ownership and recovery

- PostgreSQL is the source of truth; RQ delivery is at-least-once. OCR execution may be reclaimed after an expired lease because parsing an immutable uploaded file has no external write side effect. Lease expiry for any future external operation must remain an unknown outcome and never authorize retry by itself.
- Persist OCR claim owner token, attempt number, fencing generation and lease expiry in a dedicated task-lease row. Acquire by locking the task row and updating lease/task state in one transaction. Result and failure writes must verify the current token, attempt, generation, task state and unexpired database-clock lease in the same transaction as their state change.
- Bound OCR attempts to three; exhaustion becomes an actionable `failed` task for human review. Use a 900-second lease against the current 600-second RQ timeout. Do not add automatic lease renewal until measured OCR duration requires it.
- Apply this protocol only to immutable local upload/batch recognition. KAD acquisition remains excluded until candidate selection is valid; SRO execution is excluded and must never be automatically replayed.
- Existing `recognizing` tasks without a lease are legacy unknowns and are not auto-claimed. They require read-only reconciliation. Before enabling lease-aware workers, apply the additive migration, stop all pre-lease OCR workers, deploy lease-aware workers, and reconcile legacy `recognizing` tasks. Mixed-version OCR workers are unsafe because the old handler does not enforce lease fencing. Keep the feature disabled by default in an unadopted environment.
- A reconciler may enqueue new local OCR tasks lacking an active lease and expired eligible OCR leases. Duplicate RQ deliveries are safe only because database claim and fencing are authoritative. Redis/job enqueue success is not the durable task state.

## Browser automation

- SRO fields are found through inspected IDs, not labels.
- Prefer direct page URLs over duplicate menu entries.
- Refresh element references after WebForms postbacks.
- Never click Add merely to inspect a form: it may create a draft.
- Do not bypass external access controls.

## Product and deployment constraints

- The target is 20+ staff and large document batches, but concurrency and throughput must be measured before enabling more workers.
- Preserve a simple supportable stack where evidence supports it. Avoid visual redesign while core workflow correctness, durability and recovery remain incomplete.
- Do not call the system production-ready until operational, security, recovery, load and end-to-end evidence exists.
