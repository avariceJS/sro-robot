# Cloud office differential security review — 2026-10-14

## Executive summary

**Baseline:** `03cfe2b76072f513ebc3640b085d5c21799e95b5`, branch `codex/cloud-office-20261014`. This report reviews the uncommitted working tree as a proposed feature, not a security patch or a deployed system. The review is static: no application, browser, infrastructure, test, or network command was run.

| Severity | Count | Assessment |
| --- | ---: | --- |
| Critical | 0 | No demonstrated public exploit in the supplied Nginx path. |
| High | 0 | No removed authentication or provenance check found. |
| Medium | 0 | Two issues identified during review were corrected in the working tree. |
| Low | 0 | The helper permission description was corrected in the working tree. |

The configured origin flows from private settings to an exact cloud host permission and two extension checks. Default packaging keeps the existing local helper sources and manifest. The cloud CSRF branch now rejects noncanonical public `Host` values and requires the configured HTTPS `Origin` or an HTTPS `Referer` on that host. The supplied Nginx template pins the upstream `Host`, and Uvicorn binds to loopback. These are static conclusions only. The new cloud branches have no added tests or browser/proxy qualification. **Do not use this review as production readiness evidence.**

## Scope and method

The repository contains about 181 Python, JavaScript, template, shell, and service files in the relevant trees. This was a focused review of the changed security-sensitive source/configuration and one-hop dependencies: CSRF/session middleware, KAD import, extension background/manifest, Compose configuration, and the existing runtime binding checker. Accompanying README and operational notes were updated after the source review. No test files changed. The working tree changed during review; the findings below describe the inspected candidate, not a frozen release artifact.

History was inspected with `git blame` and `git log`. The original same-origin middleware was added in `af156b0` and its duplicate-header defense in `22df877`; both protections remain. The service bridge and local origin checks came from `2c94da7`, with date binding added in `3fd2c36`; the default array values remain the same. The Compose loopback bindings date to `c7764d3`. No earlier security fix was visibly reverted.

## Findings and review-time corrections

No unresolved security defect was demonstrated in this static review. Three concrete issues were found and corrected before this report was finalized:

| Severity at discovery | Attack or failure path | Current disposition |
| --- | --- | --- |
| Medium | A one-shot detached Compose unit without container restart policies would remain active after PostgreSQL or Redis exited, leaving web and OCR without service recovery. | [`deploy/cloud/docker-compose.restart.yml`](../deploy/cloud/docker-compose.restart.yml) now assigns Docker `restart: unless-stopped` to both containers. [`deploy/cloud/systemd/sro-robot-infra.service:10-20`](../deploy/cloud/systemd/sro-robot-infra.service) uses the overlay, explicit `--project-name deploy`, and documents Docker as restart owner. Web/OCR units also gain `PartOf` coupling. This is a static correction; daemon restart and queue recovery are unverified. |
| Medium | `https://office.home.arpa` would have passed the explicit-origin parser despite the private-use DNS restriction and become an extension permission. | [`app/public_origin.py:37-40`](../app/public_origin.py) now rejects `home.arpa` and subdomains. The setting remains canonical ASCII HTTPS with no path, port, userinfo, query, or fragment. DNS resolution to private addresses is not proven by syntax validation. |
| Low | The cloud installation page said no other sites were connected while the manifest retained localhost permissions. | [`app/web/templates/kad_helper.html:6-15`](../app/web/templates/kad_helper.html) now discloses the local 8000 workflow as well as KAD and the configured office domain. Browser host permissions still span all ports on localhost; bridge code limits service operation to 8000. The packaged README remains local-focused and should be read against the authenticated cloud installation page. |

The earlier proposed CSRF fallback weakness was also removed during review: [`app/security.py:739-765`](../app/security.py) now enters legacy behavior only for local hostnames when a public origin is configured; any other host must match the configured authority before its HTTPS Origin or Referer is checked. This conclusion is based on source inspection, not request execution.

## Trust boundaries and retained controls

| Boundary | Static observation |
| --- | --- |
| Configuration to browser permission | [`app/settings.py:53-55`](../app/settings.py) accepts an explicit validated origin only with `APP_DEBUG=0`; [`app/api/routes_kad_helper.py:48-57`](../app/api/routes_kad_helper.py) revalidates the snapshotted setting and builds a ZIP from an explicit file allowlist. The manifest adds one exact DNS pattern. Two script substitutions require exactly one marker each and fail closed on layout drift. No request `Host` enters archive generation. |
| Browser page to extension | `bridge.js` and `service.js` retain the root-page check and exact `URL.origin` allowlist. Existing service-tab, request ID, case, date, KAD card, candidate, selection, and import bindings are unchanged. The source manifest remains local-only. |
| Public HTTP request to app | [`app/security.py:721-815`](../app/security.py) preserves singleton `Host`/`Origin`/`Referer` handling from `22df877`. With public origin configured, non-loopback hosts must be the configured authority (optionally `:443`), and mutations require exact HTTPS Origin or a verified HTTPS Referer. [`app/main.py:73-79`](../app/main.py) adds TrustedHost only in that mode. |
| Internet to local process | [`deploy/cloud/nginx.conf.template:4-58`](../deploy/cloud/nginx.conf.template) drops unknown hosts, redirects canonical HTTP to HTTPS, pins the upstream Host and forwarded protocol, and proxies to loopback. [`deploy/cloud/systemd/sro-robot-web.service:15`](../deploy/cloud/systemd/sro-robot-web.service) binds Uvicorn to `127.0.0.1` and trusts forwarded headers only from that peer. These rules have not been checked on a real proxy. |

The cloud origin is not a substitute for employee authorization. KAD import still calls `current_user` at POST time in [`app/api/routes_kad_import.py:24`](../app/api/routes_kad_import.py). A shared browser can change login session during a long KAD capture and attribute import to the later user; this is a preexisting workflow risk, not introduced by this diff. The office rollout needs a server-issued run/session binding or an explicit user-change stop before that workflow is qualified.

## Blast radius and coverage

| Changed entry | Production callers / reach | Existing coverage | New cloud coverage |
| --- | --- | --- | --- |
| `public_app_origin()` | 2 direct calls: settings load and helper archive. Startup governs all web/worker processes; archive governs every authenticated helper download. | None before this feature. | None added. |
| `_same_host_origin()` | 1 direct middleware call, but applies to every mutating HTTP route. | `tests/test_security.py` covers local/default same-host and duplicate-header behavior. | No configured-cloud Host, Origin, Referer, or proxy cases added. |
| `helper_archive()` / `_archive_source()` | 1 HTTP route; 11 allowlisted ZIP members, with 3 transformed in cloud mode. | `tests/test_kad_helper_distribution.py` checks deterministic default ZIP and source manifest. | No generated cloud ZIP consistency or browser load check. |
| `serviceURL()` / content script | 2 bridge call sites and one service-content startup; all automatic KAD imports depend on them. | Local origins, tab/request binding, date/candidate protocol tested in existing Node suites. | No generated archive execution in Firefox/Chromium. |
| Systemd, Compose overlay, and Nginx scaffold | All production requests and DB/Redis/worker lifecycle if installed. | Existing Compose checker expects project `deploy` and loopback bindings. | No real host, restart, TLS, or reverse-proxy qualification. |

No tests were added or run as part of this review. Static inspection cannot verify browser match-pattern interpretation, Firefox temporary add-on loading, Chrome/Edge permission prompts, proxy behavior, certificate chain, secure-cookie behavior on local port 8000, or service restart recovery. Root added a one-day HSTS header to the final Nginx candidate after this review; it does not request subdomain coverage or preloading. Actual certificate renewal, proxy/header behavior and TLS policy still require deployment qualification.

## Disposition

The origin feature is bounded and retains the existing local default. Review-time corrections address the three identified issues, but their behavior is untested. Qualify the exact configured ZIP and reverse-proxy path on a synthetic, isolated installation. Do not enable the supplied service units or expose real documents until the office domain, SSH access, runtime roles, backup/restore, TLS, worker recovery, and user-session workflow have been verified. This review makes no approval or production-readiness claim.
