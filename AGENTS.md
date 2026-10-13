# Repository agent instructions

## Project objective

Develop SRO Robot from its experimental MVP into a reliable, supportable document-automation platform for a legal office with 20+ staff and high-volume daily batches. Treat existing code as evidence and a starting point, not as a production design constraint. Keep implemented facts separate from target requirements and unverified assumptions.

## Start here

1. Read `memory-bank/.now.md` for the current state.
2. Read `memory-bank/00-start-here.md` for the compact index, then open only task-relevant documents.
3. Check `git status`, branch, HEAD, and runtime before acting. Preserve all existing uncommitted work; never use `git reset --hard`, `git clean`, or broad staging.
4. Work autonomously on bounded engineering changes. After each meaningful change, verify it, update `.now.md`, and make a selective commit if the change is logically complete.

## Safety and data

- **Never create, update, delete, submit, or save a real SROArbitr record.** This prohibition includes CLI `--live` and UI Save/Send controls. The owner explicitly authorized one narrow exception for a new blank person card: Add → fill approved fields → inspect → Close without saving. Staff confirmed Add only opens the blank form and persistence occurs only after Save. Do not change an existing record or invoke uncertain side-effect controls.
- In the current build, `live=False` means local validation only and must not open the SRO site. The default SRO client blocks state-changing controls. Do not bypass its guard. The separate prepare-only preview path may use only the owner-authorized blank-card Add, field fill, inspection and Close sequence; Save, Send and nested address actions remain blocked.
- Read-only navigation is allowed only after checking that the next action cannot mutate external state. Do not use the real registry as a test fixture or treat a filled blank card as a persisted record.
- Do not retry the previously failed SRO job until its outcome is reconciled with read-only evidence. Do not convert an unknown external result into success or retryable failure.
- Documents contain personal data. OCR and extraction must remain local. Never send document content, OCR text, or real identifiers to cloud AI, external issue trackers, or test fixtures.
- Keep passwords in `.env`, browser profiles out of Git, and synthetic values in tests. Do not print or inspect `.env` contents.
- Do not bypass CAPTCHA, access challenges, or other external-platform controls. Stop with an explicit failure state. The owner-requested case path uses the ordinary Firefox KAD helper and local PDF import. The older MKAD/WebDriver adapter is retired; historical KAD acquisition jobs stay withheld. Preserve the separate experimental Fedresurs backend without substituting it for the requested KAD workflow.

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

## Test output and validation artifact retention

- Do not let temporary test output accumulate in Git. Keep raw stdout/stderr and intermediate files in temporary directories or ignored local storage while a check is running.
- After a successful, reviewed validation, retain only the reproducible inputs and compact evidence needed to understand and repeat it: the command/check source, pinned inputs, result summary, report, and relevant hashes. Remove raw streams and scratch data once their status and hashes are recorded.
- Preserve raw output when a check fails, is incomplete, or needs diagnosis. After resolving it, keep a concise note of the failure and its disposition before pruning superseded runs.
- Use `scripts/dev/prune-validation-logs.py` for completed post-patch-validation runs. It only prunes streams for a complete report with no findings/gaps, validates recorded stream hashes, and refreshes the artifact manifest. Review its dry-run output before applying it.
- Do not delete source fixtures, user files, or unique evidence as part of artifact cleanup. Do not stage an entire validation directory or use broad cleanup commands.
