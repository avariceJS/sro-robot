# SRO Single-Login Differential Security Review

## Executive summary

| Severity | Confirmed findings |
|---|---:|
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 0 |

**Overall code risk:** High-sensitivity authentication change; no exploitable defect was confirmed in the reviewed diff and synthetic tests.

**Recommendation:** Conditional. Keep this candidate off production until the new adapter completes one authorized live login and session-reuse check without another credential retry, and the release migration is reviewed against the actual target database. This recommendation reflects missing live evidence, not a confirmed vulnerability.

**Scope:** Working-tree diff from `c20e85b` on `codex/sro-single-auth`: 22 changed/new files before this report (authentication route, identity/session services and schema, external browser adapter, worker, config, tests and Memory Bank). Authentication, encryption, external navigation and session lifecycle received high-risk review. This is a focused differential review, not an independent full-system audit.

## What changed

- `/login` now verifies employee-supplied SROArbitr credentials in a fresh browser context and issues an app session only after the configured authenticated-page marker and an SRO-domain cookie are observed.
- The app maps a verified SRO username to one local audit actor. It can provision that actor without a separate Robot password, rejects conflicting identities, and handles a concurrent first-login uniqueness race with rollback and re-read.
- Playwright storage state is authenticated-encrypted with AES-GCM before it reaches PostgreSQL. HKDF derives the key from `APP_SECRET_KEY`; associated data binds the ciphertext to the local employee and login-session IDs. Workers receive only the requesting employee's decrypted state in memory.
- The employee browser receives only the normal signed app session cookie. SRO cookies are not returned to it or placed in Redis/RQ. Logout revokes the local login row and clears encrypted state. The worker checks the external authenticated marker before preview work and revokes only the session that the SRO rejected.
- Runtime configuration no longer loads the shared `SRO_LOGIN` / `SRO_PASSWORD` values. The web preview path fails closed without an injected per-user state. Existing local SRO mutation guards remain in place.
- Migration `ea912b30c761` adds nullable encrypted-state and expiry fields. Expected schema head advances accordingly.

## Critical findings

No confirmed security vulnerability was found in the changed code after reviewing the authentication route, session encryption, identity lookup, worker handoff, legacy credential loading and existing SRO request guard.

The following are release gates and evidence limits rather than confirmed vulnerabilities:

### Live adapter qualification is partial

The first new-adapter attempt returned a sanitized `unavailable` result. Non-credential diagnostics confirmed that the public form posts to `/access/loginform.aspx` with the expected button field, the adapter admits that path, and local headless Chromium can load it. Two subsequent controlled attempts with the already-authorized owner account succeeded. A fresh Chromium context reused captured state, and the owner-authorized blank person form accepted seven synthetic values, returned the expected values on read-back, then closed with `saved=false`. No search or real record mutation was performed. This qualifies one account's live login, cookie-domain acceptance, cross-context reuse and unsaved blank-form preview. It does not qualify SRO-side logout, externally forced expiry, CAPTCHA/MFA behavior, another employee or production deployment.

### No stable external user identifier is established

The current local identity key is the exact verified SRO username because no stable SRO user ID was established. Username changes can split local history; ambiguous or case-only conflicts fail closed. Do not infer that a username is an immutable external identity.

### One actual employee account only

Synthetic coverage checks user/session binding and parallel application sessions, but real cross-employee isolation cannot be proven using one external account. A second actual employee account is required to qualify real simultaneous multi-user use.

### Local logout is not an SRO-side logout

Local logout revokes application access and deletes the linked encrypted state. It does not call an SRO logout endpoint. The app can no longer use that state, but the SRO-issued remote session may remain valid until the external platform expires or revokes it.

## Adversarial checks

- **Unauthenticated user attempts local-password bypass:** `/login` no longer calls the local password verifier; no app session is issued before SRO verification and encrypted-state persistence both succeed.
- **Attacker submits repeated credentials:** existing per-peer/per-normalized-login throttling reserves before external verification; backend-unavailable attempts release the reservation, while rejected credentials and challenges consume failure budget. The login body and concurrent attempts remain bounded.
- **Attacker replays another employee's ciphertext:** AES-GCM authentication fails when the employee ID or login-session ID differs. Corrupt ciphertext is cleared and the external state is unavailable.
- **Attacker injects cookies for another host:** every cookie domain and local-storage origin must now match the exact HTTPS SRO origin; unknown serialized fields are rejected. SRO cookies never enter the browser's app-domain cookie.
- **Expired/revoked SRO session is reused:** workers open a fresh context with the selected employee's state and require the authenticated marker; they do not fall back to `.env` credentials or a shared profile. A rejected session is revoked locally.
- **Prepare-only automation reaches a save action:** the existing request guard allows only the owner-authorized blank-card Add and Close path; Save/Send and other POST requests remain blocked. Live preview used only a new blank card, seven synthetic values and Close; no real record search, save, update or deletion was performed.
- **Context teardown fails:** cleanup still closes browser/runtime handles and clears the `SroClient` reference; worker code drops its decrypted state reference immediately after the ephemeral context exits.

## Test coverage and evidence

- Full Python suite on the reviewed tree: **607 tests run, 88 PostgreSQL Docker tests skipped; suite passed**. The successful output was captured only in a temporary directory and summarized by SHA-256 `6ff7a1a12a6c9f0d148a4117b0e81b86aa7990aaf1136f0b83765b5b1a2cd386` (3,911 bytes).
- Focused authentication/encryption/HTTP/session/form-guard suite: **60 tests passed** after the unexpected-cookie-domain case was added.
- Earlier temporary PostgreSQL 16 smoke applied the complete migration chain through `ea912b30c761`; synthetic users had isolated ciphertext, per-session logout and expiry behavior passed, and runtime schema guard differences were empty. The temporary cluster was removed.
- `git diff --check` and Python compilation passed after the final context-cleanup edits.
- No production VPS, application database or queue was touched. The live SRO was accessed only for the owner-authorized authentication and blank-card Add/fill/read-back/Close sequence; no real record was created, saved, updated, searched or deleted.

## Blast radius

| Function or boundary | Direct production callers found | Risk |
|---|---:|---|
| `authenticate_sro_credentials` | 1 web route | High; external credentials and identity proof |
| `resolve_sro_user` | 1 web route | High; employee attribution and auto-provisioning |
| `attach_external_session` | 1 web route | High; key/session persistence boundary |
| `active_external_state` | 1 SRO preview worker | High; employee isolation |
| `login_session_is_active` | app auth dependency | High; every authenticated route depends on it |
| `revoke_login_session` | login replacement and logout | Medium; revocation and cleanup |
| `validate_storage_state` | seal/open paths | High; all persisted external state |

The review found no removed SRO mutation guard or relaxed `Save` control. Historical commits `4bf4240` and `36d68af` establish the baseline local-only dry-run and prepare-only request guard; this diff preserves those protections.

## Recommendations

### Before release

- [x] With the already authorized owner account, verify successful new-adapter login, exact cookie-domain acceptance, fresh-context reuse, and the permitted unsaved blank-card preview. Never save an SRO record.
- [ ] Qualify live SRO-side logout and forced expiry behavior without bypassing platform controls. Local revocation/expiry is covered by tests, but the remote endpoints were not exercised.
- [ ] Obtain a second authorized employee account before claiming real multi-user external isolation.
- [ ] Review migration `ea912b30c761` against the target release source and database backup/rollback procedure. Do not apply it to production in this candidate.
- [ ] Keep SRO mutation disabled. The permitted blank-form preview must close without Save.

### Follow-up

- [ ] Establish whether SRO exposes a stable user ID and a supported logout mechanism. Until then, retain exact-username mapping and describe local logout accurately.
- [ ] Add an integration test using two independent SRO-issued synthetic states through the worker boundary, including stale-session revocation and two-device logout behavior.
- [ ] Revisit live adapter qualification after any WebForms or anti-bot flow change; fail closed and do not automate CAPTCHA/MFA.
