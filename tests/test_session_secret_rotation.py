from __future__ import annotations

import unittest

from starlette.applications import Starlette
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient


def _app(secret_key: str) -> Starlette:
    async def login(request: Request) -> Response:
        request.session["user_id"] = "synthetic-user-7"
        return JSONResponse({"authenticated": True})

    async def whoami(request: Request) -> Response:
        return JSONResponse({"user_id": request.session.get("user_id")})

    app = Starlette(routes=[Route("/login", login), Route("/whoami", whoami)])
    app.add_middleware(SessionMiddleware, secret_key=secret_key, https_only=True)
    return app


class SessionSecretRotationTests(unittest.TestCase):
    def test_rotating_the_signing_secret_revokes_old_cookies_in_both_directions(self) -> None:
        old_secret = "synthetic-old-session-secret-0000000000000000"
        new_secret = "synthetic-new-session-secret-1111111111111111"

        with TestClient(_app(old_secret), base_url="https://testserver") as old_client:
            old_cookie = old_client.get("/login").cookies["session"]
            self.assertEqual(old_client.get("/whoami").json()["user_id"], "synthetic-user-7")

        with TestClient(_app(new_secret), base_url="https://testserver") as new_client:
            new_client.cookies.set("session", old_cookie)
            response = new_client.get("/whoami")
            self.assertIsNone(response.json()["user_id"])
            new_cookie = new_client.get("/login").cookies["session"]

        with TestClient(_app(old_secret), base_url="https://testserver") as old_client:
            old_client.cookies.set("session", new_cookie)
            response = old_client.get("/whoami")
            self.assertIsNone(response.json()["user_id"])


if __name__ == "__main__":
    unittest.main()
