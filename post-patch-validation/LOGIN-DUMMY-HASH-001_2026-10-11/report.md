# Differential Security Review: Login Dummy-Hash Timing Mitigation

Date: 2026-10-11
Baseline: `3ca9be1`
Patch under review: `d47a461`
Disposition: **REJECT / REVERT before deployment**

## Executive summary

| Severity | Findings |
| --- | ---: |
| Critical | 0 |
| High | 0 |
| Medium | 1 |
| Low | 0 |

The change reduces one username-enumeration timing difference by performing Argon2 verification for missing/deactivated accounts. It also makes unauthenticated requests for arbitrary login names consume the memory-hard password-verification path. The login endpoint has no distributed attempt throttle, and client address trust behind a proxy is not configured. The patch is therefore rejected pending a safe ingress/client-identity and rate-limit design; commit `d47a461` was reverted before deployment.

## Changed code and context

The patch changed `app/services/auth.py` and `tests/test_auth_session.py` (28 insertions, no removals). `authenticate()` is called by `app/api/routes_auth.py:30`; the measured blast radius is one production call site. No prior security validation was removed. Baseline history shows `authenticate()` returned immediately when the user was absent or inactive (`git blame` at baseline `3ca9be1`; original behavior in `b7a51df`).

The patch added a random process-local Argon2 hash and verification at `app/services/auth.py:18-22,43-53`. The current hasher parameters are time cost 3, memory cost 65,536 KiB, parallelism 4. It also added two tests in `tests/test_auth_session.py:87-106`. Auth and HTTP integration suites passed 14 tests, including verification that the submitted synthetic password does not match the dummy hash. These tests establish call behavior, not safe anonymous request capacity.

The login middleware at `app/security.py:25-78` limits the buffered credential request body to 16 KiB; it does not rate-limit attempts. `SameOriginMutationMiddleware` rejects browser cross-origin mutations, but a direct HTTP client can supply its own `Host` and `Origin` headers. The deployment's trusted proxy/client-address contract is unknown.

## Finding

### MEDIUM: Unbounded Argon2 verification enables anonymous resource exhaustion

**Attacker:** unauthenticated client able to reach the login endpoint.
**Entry point:** `POST /login` -> `routes_auth.login_submit()` -> `auth.authenticate()`.
**Exploitability:** EASY if the service is reachable; no account or valid username is required.
**Concrete impact:** concurrent requests can each request about 64 MiB of Argon2 working memory and consume three verification passes. This can exhaust application memory/worker capacity and deny service to staff. The exact saturation threshold depends on deployed worker count and memory limits, neither of which is known.

Attack sequence:

1. Send repeated `POST /login` requests using random login strings and syntactically same-host headers.
2. Each request misses the account lookup and now executes Argon2 verification against the dummy hash.
3. Parallel requests multiply memory and CPU use before the request completes.
4. Application login and other synchronous request handling can become unavailable.

The baseline did not perform Argon2 for unknown logins, so the patch expands the expensive anonymous path. A 16 KiB body cap does not bound request frequency. This is a patch-introduced risk, not a finding that the baseline endpoint is generally protected: valid-account password guesses already invoke Argon2.

## Recommendation and disposition

The dummy-hash mitigation must remain disabled until an ingress and application throttling contract is available. A future implementation needs a verified client identity behind the actual proxy topology, a distributed limiter that works across web processes, bounded memory/concurrency behavior, and an office-compatible threshold. Forwarded headers must not be trusted without an explicit trusted-proxy boundary. Validate both username timing behavior and concurrent resource bounds in an isolated environment.

The finding was addressed in this development session by reverting only commit `d47a461`; the implementation and its tests are not deployed. No external system was contacted for writes.

## Analysis limits

- No timing benchmark or concurrent saturation test was run; timing results are noisy and do not prove anonymity.
- Production ingress, trusted proxies, process count, memory limits and expected login burst are unknown.
- Review scope was limited to the changed authentication path and one-hop caller/middleware behavior; this is not a complete application security audit.
- Confidence is high that the patch adds unbounded Argon2 work for unknown logins; deployment impact magnitude is unqualified.
