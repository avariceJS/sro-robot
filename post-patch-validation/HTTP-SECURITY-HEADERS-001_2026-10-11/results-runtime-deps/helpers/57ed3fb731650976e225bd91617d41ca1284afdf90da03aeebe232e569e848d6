from __future__ import annotations

import asyncio
import sys

from app.security import SameOriginMutationMiddleware, SecurityHeadersMiddleware


def capture(path: str, method: str = "GET", request_headers: list[tuple[bytes, bytes]] | None = None):
    async def run():
        messages = []

        async def downstream(scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 201,
                "headers": [
                    (b"content-type", b"text/html"),
                    (b"cache-control", b"public, max-age=3600"),
                    (b"x-frame-options", b"ALLOWALL"),
                    (b"x-frame-options", b"SAMEORIGIN"),
                ],
            })
            await send({"type": "http.response.body", "body": b"synthetic-response"})

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "headers": request_headers or [],
        }
        await SecurityHeadersMiddleware(downstream)(scope, None, send)
        start = next(item for item in messages if item["type"] == "http.response.start")
        body = next(item for item in messages if item["type"] == "http.response.body")["body"]
        headers = {}
        for key, value in start["headers"]:
            headers.setdefault(key.lower(), []).append(value)
        return start["status"], headers, body

    return asyncio.run(run())


def reject_cross_origin():
    async def run():
        messages = []
        downstream_called = False

        async def downstream(scope, receive, send):
            nonlocal downstream_called
            downstream_called = True
            raise AssertionError("cross-origin mutation reached downstream app")

        async def send(message):
            messages.append(message)

        app = SecurityHeadersMiddleware(SameOriginMutationMiddleware(downstream))
        await app({
            "type": "http",
            "method": "POST",
            "path": "/tasks/synthetic/mutate",
            "headers": [
                (b"host", b"robot.example"),
                (b"origin", b"https://attacker.example"),
            ],
        }, None, send)
        start = next(item for item in messages if item["type"] == "http.response.start")
        body = next(item for item in messages if item["type"] == "http.response.body")["body"]
        headers = {}
        for key, value in start["headers"]:
            headers.setdefault(key.lower(), []).append(value)
        return start["status"], headers, body, downstream_called

    return asyncio.run(run())


def main(mode: str) -> None:
    status, headers, body = capture("/tasks/synthetic")
    if mode == "control":
        assert status == 201 and body == b"synthetic-response"
        print("control=benign-dynamic-response-preserved")
    elif mode == "exploit-cache":
        print("PPV_REACHED", flush=True)
        assert headers.get(b"cache-control") == [b"no-store"]
        print("dynamic_cache=no-store")
    elif mode == "exploit-csp":
        print("PPV_REACHED", flush=True)
        policy = headers.get(b"content-security-policy", [b""])[0]
        assert b"script-src 'self'" in policy and b"object-src 'none'" in policy
        assert b"frame-ancestors 'none'" in policy
        print("dynamic_csp=restrictive")
    elif mode == "variant-cross-origin":
        status, response_headers, _body, downstream_called = reject_cross_origin()
        print("PPV_REACHED", flush=True)
        assert status == 403 and not downstream_called
        assert response_headers.get(b"cache-control") == [b"no-store"]
        assert b"content-security-policy" in response_headers
        print("cross_origin_rejection=protected")
    elif mode == "behavior-static-cache":
        status, static_headers, body = capture("/static/htmx.min.js")
        print(
            f"status={status};body={body.decode()};cache={static_headers[b'cache-control'][0].decode()}"
        )
    elif mode == "security-frame-override":
        frame_values = headers.get(b"x-frame-options", [])
        assert frame_values == [b"DENY"]
        print(f"status={status};body={body.decode()};frame=DENY;frame_count={len(frame_values)}")
    else:
        raise SystemExit(f"unknown probe mode: {mode}")


if __name__ == "__main__":
    main(sys.argv[1])
