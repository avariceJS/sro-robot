# Manual debtor-card creation idempotency — differential review

## Executive summary

| Severity | Findings |
| --- | ---: |
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 0 |

**Overall risk:** Low after review and synthetic verification.
**Recommendation:** Approve for `develop`; this review does not authorize or
qualify a production migration.
**Reviewed commit:** `6247307ab1ce565385301bd8226b2efe2856b28c`
**Baseline:** `cb387290cc8c3c481dc15379bbe10c50f7580064`

## Scope and changes

The commit adds a per-form random action key and a durable receipt table. The
manual-card endpoint now atomically claims the key, creates and validates the
card, and records the card ID. Identical retries return the same internal card;
changed payloads and cross-user reuse return a conflict. The additive migration
is forward-only. Related changes update the runtime schema head, migration
smokes, PostgreSQL restore/adoption drills, and tests.

Primary code locations: [route](../app/api/routes_assignments.py#L178),
[replay service](../app/services/assignments.py#L629),
[receipt model](../app/db/models.py#L353),
[form key](../app/web/templates/debtor_card_new.html#L18), and
[migration](../migrations/versions/a7c4e91f6302_add_manual_card_create_idempotency.py#L17).

Risk focus was the new persistence/concurrency path and the form endpoint.
Documentation, test, and schema-revision fixture changes were also checked.
The only production caller of `create_manual_debtor_card_once` is the
authenticated `POST /debtors/new` route; the GET route issues its form key.

| Change group | Files | Risk | Verification |
| --- | ---: | --- | --- |
| Route, service, model, template, migration, schema head | 6 | Medium; state and key handling | Authenticated HTTP, PostgreSQL replay race, migration/restore |
| Tests | 5 | Low | Full deterministic and PostgreSQL suites |
| Schema-head / drill updates | 7 | Medium; deployment compatibility | Runtime role, head, adoption, restore and downgrade drills |
| Documentation / Memory Bank | 3 | Low | Diff and consistency review |

The changed service function has one production caller and four direct
integration-test call sites. No high-blast-radius public helper was changed.

## Baseline and invariant analysis

At baseline commit `83fd7799`, `POST /debtors/new` created a new card and then
updated it for every accepted POST. It had no request identity, so a second
delivery after a lost response could create another card. The new code keeps
the existing transaction boundary around card creation and field validation
and adds an atomic unique-key claim before either is persisted.

The intended invariants hold in the reviewed diff:

- A request key is exactly 256 random bits in the form and is validated as 64
  lowercase hexadecimal characters before use.
- The database stores SHA-256 of the action key, not the key itself. The
  payload digest is HMAC keyed by the same random key, so a database-only
  reader cannot test guessed debtor fields against an unkeyed digest.
- The receipt, card, and card revision commit together; validation failure
  rolls back the receipt and card.
- The receipt binds the authenticated user, canonical selected entity fields,
  action, and resulting card. Replays with a different user or payload fail.
- A fresh form gets a fresh key, preserving intentionally separate drafts,
  including drafts without an INN.
- The endpoint remains behind `current_user` and the existing same-origin
  mutation middleware. No SRO client, external request, or external record is
  involved.

No removed authorization or validation check was found. The route still checks
the entity kind and action, and the existing service still applies field,
identity, INN, and confirmation rules before commit.

## Historical context

Baseline blame places the original create/update route in `83fd7799` and the
entity-specific field selection in `cc34a5bc`. The reviewed change replaces
the two-step route call with an atomic wrapper but does not remove either
business validation or the original plain manual-card helper used by the
wrapper. The latter remains internal and is not called directly by the route.
No security-oriented code was removed or reintroduced from an earlier fix.

## Adversarial analysis

**Attacker model:** an unauthenticated requester, an authenticated staff member,
or an authenticated staff member holding a different form key.
**Entry point:** `POST /debtors/new`.

- An unauthenticated request is rejected by `current_user` before this service
  writes a receipt or card.
- A different origin is rejected by the existing same-origin mutation
  middleware.
- A staff member replaying the same key and exact payload gets the original
  card ID. A changed payload or different user with the same key receives 409;
  the existing receipt is not modified.
- A staff member with a fresh form key can create another card. That is the
  documented intentional action and is not treated as an authorization bypass.
- A database-only reader cannot recover the random key from its SHA-256 hash
  or use that key to verify guessed field payloads against the keyed HMAC.

No concrete exploit or security regression was found. Repeated independent
card creation remains available to an authorized employee, as it was before;
this feature only makes retries of one form action deterministic.

## Test coverage and verification

Coverage includes the hidden random key in the rendered form, route handling,
an actual authenticated HTTP replay redirect, and a PostgreSQL race between two
same-key creates. The database test also verifies one result, same-key changed
payload rejection, and creation with a separate fresh key. Migration failure,
schema-head, runtime-role, restore/adoption, and downgrade-refusal checks cover
the receipt table lifecycle.

`SRO_DRILL_TASKS=20 bash scripts/dev/verify.sh` passed on the reviewed tree:
784 Python tests, 107 disposable PostgreSQL integration tests, 148 Node tests,
runtime-role checks, migration and restore/adoption drills, Redis/RQ recovery,
and 1,000/1,000 synthetic claims in both thread and process modes. This is local
synthetic evidence, not production load or rollout qualification.

## Residual considerations

One receipt row is retained per successful manual create to keep the retry
outcome durable. This is small relative to the corresponding card and supports
the replay contract; table growth should be considered if the office later
defines a retention policy. Deleting receipts without a reviewed change would
allow an old action key to create a second card, so the migration intentionally
refuses downgrade.

No blocking remediation is recommended for `develop`. Before an
owner-authorized deployment, apply the migration through the normal release
workflow and verify runtime role grants in that environment.

**Confidence:** High for the changed local creation path and its tested
PostgreSQL concurrency boundary; limited to the reviewed commit and synthetic
runtime. No production database or external registry was accessed.
