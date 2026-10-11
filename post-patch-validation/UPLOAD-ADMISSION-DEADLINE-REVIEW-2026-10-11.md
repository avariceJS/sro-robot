# Upload admission deadline and control-plane review

## Executive assessment

The upload path had byte and concurrency bounds but no application-level deadline while ASGI request-body chunks were being collected. A stalled client could therefore occupy one of the finite multipart admission slots without a time bound. The patch adds a configurable total body-read deadline, returns HTTP 408 before persistence when the deadline expires, and releases the per-process semaphore. Targeted and full synthetic verification passed.

Stage 1 remains **M1 partial**. The local tests establish bounded request collection and regression behavior; they do not qualify a deployed proxy, multiple web processes, production traffic, or operator workload.

## Scope and historical context

Reviewed the change from base `185eee7` in `app/security.py`, `app/settings.py`, `app/main.py`, `.env.example`, and their boundary/configuration tests and operator documentation. `git blame` shows the upload byte/concurrency middleware was introduced in `0311eed8`; its original `counted_receive` awaited `receive()` without a deadline. The relevant production call graph has one app registration in `app/main.py`, guarded to `POST /upload`; the intake route consumes the body through `request.form()`. The new setting reaches that middleware registration, and an application-boundary test asserts the effective configuration.

The timeout is measured from the first body receive through final body collection and is rechecked on every subsequent receive. It does not time-limit document persistence or queue work after the body has been read. Existing declared and streamed byte checks remain in place.

## Findings

### Fixed: stalled multipart body could hold admission capacity indefinitely

**Severity:** Low, resource-exhaustion boundary
**Coverage:** `UploadAdmissionTests.test_stalled_upload_body_times_out_and_releases_admission_capacity`; settings defaults/configuration/invalid values; application middleware wiring.

The patched middleware wraps each ASGI receive in the remaining total deadline. A timeout is translated to an internal signal and a generic 408 response if response headers have not started. The semaphore remains under `async with`, so timeout unwinding releases capacity. The test stalls the body receive, verifies 408 without proceeding into normal downstream work, and then successfully submits a second request through the same single-slot middleware instance. Existing tests retain the declared-length and streamed-size rejection controls.

### Residual: login verification cost may vary with legacy password-hash parameters

The random dummy hash uses current Argon2 parameters, while an existing account can still have an older stored hash until a successful login rehashes it. A wrong password for that account may therefore have a different verification cost from the missing-account path. The current patch corrects comments and owner-facing decision documentation to state the limited guarantee. Resolving all historical parameter variation would require a separately reviewed migration or credential-reset policy; neither is assumed here.

## Validation

Targeted tests passed for the upload admission middleware and settings. The final full local verifier passed 377 discovered tests (48 PostgreSQL-only cases skipped in discovery), then all 48 disposable PostgreSQL cases, restore/adoption and recovery drills, and synthetic thread/process claim probes. It did not access the shared database, start shared workers, or interact with SROArbitr. Full output SHA-256: `b139ed4cdbaffc01b269e91fad9958729e187a724c654178f4ed181a2de7c12b`.

## Limitations

- The deadline is per web process and applies only when the application consumes the request body. It does not bound sockets queued at the server or replace reverse-proxy request timeouts, connection limits, or body-size limits.
- The 300-second default is configurable and is a local bound compatible with the current 105 MiB request limit; measured office document-transfer behavior has not been supplied.
- No production-like slow-client test, multi-process load test, proxy test, or soak test was run.
- This focused review is not a complete authentication or deployment security audit. The legacy Argon2 cost variance above remains open.
