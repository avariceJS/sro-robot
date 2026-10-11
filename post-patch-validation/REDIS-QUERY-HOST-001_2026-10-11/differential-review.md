# Differential review — REDIS-QUERY-HOST-001

Base: `de537a1f5af370796c4da4dc381f1e1fab04f5ce`
Changed files: `app/settings.py`, `tests/test_settings.py`

## Review

The production settings loader calls `_require_redis_transport_security` before constructing the immutable `Settings` object. The guard's previous fallback (`parsed.hostname or localhost`) did not match redis-py's URL parsing when the URI authority was absent and `host` appeared in the query. In the pinned redis-py 8.1.0, `Redis.from_url` exposes the query host as the connection host. The patch now classifies that target, rejects repeated query hosts as ambiguous, and rejects an explicitly empty target. Authority hosts continue to take precedence, which matches redis-py's parser behavior. Plaintext non-loopback destinations still require `rediss://`; the pre-existing TLS certificate and hostname-verification checks remain unchanged.

The only production caller is `Settings.load`; the additional tests exercise its production-only validation. No network/client operation is added to the code path. Unit coverage checks a remote query host rejection, a loopback query host acceptance and duplicate-host rejection. The PPV exploit/variant checks prove baseline acceptance and patched rejection without a network connection; controls cover direct loopback parsing and the existing TLS guards. The full verifier also passed.

## Findings

No additional defect found in this diff. Remaining limitation: evidence targets the pinned redis-py URL parser and validates configuration/connection kwargs only; it does not establish a real TLS handshake or deployed Redis certificate/endpoint identity.
