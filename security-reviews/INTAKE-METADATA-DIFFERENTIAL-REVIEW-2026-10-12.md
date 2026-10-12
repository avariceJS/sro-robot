# Intake Metadata Differential Review — 2026-10-12

## Executive summary

| Severity | Confirmed findings |
|---|---:|
| Critical / High | 0 |
| Medium | 0 |
| Low | 0 open |

**Reviewed change:** `db48e13..9b339ea` (`Bind intake retries to filename metadata`).
**Follow-up hardening:** `e6e4162` (`Require explicit intake metadata binding`).
**Recommendation:** Accept the change after the follow-up hardening, which is included in the current branch.
**Confidence:** High for the intake identity, metadata hashing, migration, and caller paths reviewed; this is not a full application security audit.

The change binds same-user idempotent upload retries to both ordered content digests and exact ordered filenames. The HTTP route passes the metadata digest before any document, task, or outbox writes. A nullable database field preserves retry compatibility for operations committed before the metadata contract existed. No exploitable regression was found. Review identified one maintainability weakness in the new service API: its optional default allowed future callers to silently omit metadata. Follow-up `e6e4162` removes the default and makes every caller choose a digest or explicit `None`.

## What changed

The reviewed commit adds migration `b7e1c40d62a9`, an ORM field, a versioned length-framed UTF-8 filename-manifest digest, replay conflict checking, upload-route wiring, deterministic digest tests, HTTP conflict assertions, and PostgreSQL compatibility tests. It updates migration/head fixtures and operational documentation.

The follow-up changes `begin_intake_operation` so `source_metadata_sha256` is required as a keyword argument. The upload route supplies the computed digest; the release-gated KAD route and explicit legacy test setup pass `None`.

## Findings and disposition

### Low — implicit metadata omission was possible for future callers (resolved)

**Location:** `app/services/tasks.py:126-133` in follow-up `e6e4162`.
**Risk:** Low; no external exploit path in the reviewed commit.
**Blast radius:** Two application call sites (upload and disabled KAD) plus test callers.
**Test coverage:** HTTP route wiring, deterministic manifest contracts, PostgreSQL conflict/legacy behavior, and full verifier.

In `9b339ea`, `source_metadata_sha256` defaulted to `None`. The current upload route correctly passed a digest, so an authenticated user could not bypass filename binding through the HTTP path. A future internal upload caller could omit the argument and create a new operation with no filename binding, weakening the invariant silently. Commit `e6e4162` makes the choice mandatory at the Python call boundary and updates callers to state their intent explicitly.

## Adversarial review

**Attacker model:** An authenticated employee may submit files and reuse a form idempotency key. Employees have equal case access under the accepted policy.

**Relevant attack sequence:** Submit a file with a key, then replay that key with unchanged bytes and a renamed or reordered file. Before the metadata binding, the content-only manifest could return the original task while retaining the earlier filename provenance. The reviewed change computes a separate ordered metadata digest and compares it under the locked operation row before downstream writes; changed metadata conflicts. The route regression verifies that a conflict causes no storage, task, outbox, or dispatch call.

Filename metadata is length-framed and version-domain-separated, so ambiguous concatenations do not collide at the encoding layer. The digest stores no raw filenames in the operation record. Existing null-digest operations deliberately retain content-only retry behavior so a client can recover a previously committed request with an unknown HTTP outcome.

## Coverage and blast radius

- The changed service function has two production call sites: upload and currently disabled KAD intake. Both are explicitly bound after `e6e4162`.
- The public HTTP path validates input and computes both manifests before `begin_intake_operation`; conflict handling precedes document storage, task creation and outbox intent creation.
- Tests cover exact repeat, rename, order, ambiguous concatenation, empty values, invalid types, HTTP call wiring, no-write conflict handling, PostgreSQL changed-name conflict, and null-metadata compatibility.
- The full disposable verifier passed 460 discovered tests and all 70 PostgreSQL cases, including migration, runtime-role, restore/adoption, queue recovery, and 1,000-task thread/process claim drills. Log SHA-256: `86f1164532137dd8c45663158a932919a414abfb2e702687116ef015a677ade5`.

## Historical context

`git blame` shows the prior content-manifest and row-locking behavior predates this change. The reviewed diff adds a separate metadata comparison and does not remove the existing content digest, uniqueness constraint, row lock, or transaction ordering. The earlier behavior could not detect filename-only changes; the new code closes that gap. No security-removal or reintroduction pattern was found in the reviewed diff.

## Limitations

This review covers the intake manifest change and its direct callers, not the full application, dependency supply chain, deployed role grants, real shared database, or production-like workload. All verification used disposable local services. No shared database or SRO record was accessed or changed. Stage 1 remains M1 partial.
