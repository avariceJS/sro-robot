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

## Workflow architecture

- Model intake as document/task work with scenario-defined fields; do not make a physical-person card the universal domain object.
- OCR is stateless and can use a measured worker pool. SRO WebForms has page/session state; serialize per SRO account/session until isolation is proven.
- Preserve source, candidate act list, selection rationale, extracted-field provenance, operator edits/approvals, attempt identity, errors and verified result.
- Fail closed on unsupported act types, ambiguous participant identity, CAPTCHA/access challenge, invalid or mismatched case identifiers, and unknown external outcomes.
- Keep dry-run, saved and verified results distinct.

## Operator approval

- Approval binds to an exact field payload. Implemented as SHA-256 over canonical JSON in the `approved` audit event; field edits invalidate it; worker startup rechecks it. Older approvals without a digest require re-approval.
- Approval does not prove identity, eliminate concurrent task races, or authorize any real write by an agent.

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
