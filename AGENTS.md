# Repository agent instructions

## Project objective

Develop SRO Robot from its experimental MVP into a reliable, supportable document-automation platform for a legal office with 20+ staff and high-volume daily batches. Treat existing code as evidence and a starting point, not as a production design constraint. Keep implemented facts separate from target requirements and unverified assumptions.

## Start here

1. Read `memory-bank/.now.md` for the current state.
2. Read `memory-bank/00-start-here.md` for the compact index, then open only task-relevant documents.
3. Check `git status`, branch, HEAD, and runtime before acting. Preserve all existing uncommitted work; never use `git reset --hard`, `git clean`, or broad staging.
4. Work autonomously on bounded engineering changes. After each meaningful change, verify it, update `.now.md`, and make a selective commit if the change is logically complete.

## Safety and data

- **Never create, update, delete, submit, or save a real SROArbitr record.** This prohibition includes CLI `--live`, UI Save/Send controls, and any action with an unproven write side effect. Do not click Add if it may create a draft. Use isolated fakes/mocks for write testing.
- Read-only navigation is allowed only after checking that the next action cannot mutate external state. Test form filling only when the page is proven not to autosave or create drafts. Never use the real registry as a test fixture.
- Do not retry the previously failed SRO job until its outcome is reconciled with read-only evidence. Do not convert an unknown external result into success or retryable failure.
- Documents contain personal data. OCR and extraction must remain local. Never send document content, OCR text, or real identifiers to cloud AI, external issue trackers, or test fixtures.
- Keep passwords in `.env`, browser profiles out of Git, and synthetic values in tests. Do not print or inspect `.env` contents.
- Do not bypass CAPTCHA, access challenges, or other KAD/MKAD controls. Stop with an explicit failure state.

## External platform rules

- SROArbitr is ASP.NET WebForms. Use element identifiers documented in `memory-bank/30-integrations.md`, not labels. After postbacks, wait for page settling and reacquire locators.
- Account/network reachability is IP-dependent. Verify actual source network before diagnosing a connection refusal as a code bug.
- Preserve source provenance, candidate documents and selection reasons. Case number alone does not establish that the first/latest act is correct.

## Engineering and evidence

- Prefer deterministic behavior, durable state, idempotency, concurrency controls, structured logs, audit events, recovery and read-back verification.
- Add unit, integration, end-to-end, concurrency and failure-recovery tests at the relevant boundaries. Use test doubles for external writes. Do not claim exhaustive coverage or production readiness based on unit tests alone.
- Do not add cosmetic UI work. Functional UI changes are allowed when needed for correct operations or recovery.
- Keep internal technical documentation in English. Communicate with the owner only in Russian.
- Use the current model-routing instructions from the active owner request: Luna High for execution; Sol High only for decisions with high data-integrity, safety, concurrency, identity, migration or external-write risk. Delegate predictable mechanical work when the environment supports it.
- Never ask for routine engineering approval. Ask only when business semantics, missing access, material spend, or an explicitly prohibited/irreversible real action makes progress impossible.
