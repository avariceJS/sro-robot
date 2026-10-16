from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from urllib.parse import urlencode

sys.path.insert(0, os.environ["PPV_CHECKOUT"])
from sro_robot.platform.client import SroClient

BASE = "https://example.test"


class RouteProbe:
    def __init__(self, method: str, url: str, data: dict | list[tuple[str, str]] | None = None):
        self.request = SimpleNamespace(
            method=method,
            url=url,
            post_data=urlencode(data or []),
        )
        self.continued = False
        self.abort_reason: str | None = None

    def continue_(self) -> None:
        self.continued = True

    def abort(self, reason: str) -> None:
        self.abort_reason = reason


def dispatch(
    method: str,
    url: str,
    data: dict | list[tuple[str, str]] | None = None,
) -> RouteProbe:
    route = RouteProbe(method, url, data)
    client = SroClient(cfg=SimpleNamespace(base_url=f"{BASE}/default.aspx"), prepare_only=True)
    client._guard_prepare_only_request(route)
    return route


def blocked(route: RouteProbe) -> bool:
    return not route.continued and route.abort_reason == "blockedbyclient"


def continued(route: RouteProbe) -> bool:
    return route.continued and route.abort_reason is None


def check(name: str) -> None:
    if name == "safe-get":
        assert continued(dispatch("GET", f"{BASE}/register/person_card.aspx"))
        print("CONTROL_OK")
        return

    if name == "same-origin-delete":
        route = dispatch("DELETE", f"{BASE}/register/person_card.aspx")
        print("PPV_REACHED", flush=True)
        assert blocked(route), "prepare-only forwarded same-origin DELETE"
        return

    if name == "cross-origin-post":
        route = dispatch(
            "POST",
            "https://api.example.test/register/person_card.aspx",
            {"__EVENTTARGET": "ctl00$pContent$BtnItemPersonOk"},
        )
        print("PPV_REACHED", flush=True)
        assert blocked(route), "prepare-only forwarded cross-origin POST"
        return

    if name == "conflicting-login-event":
        route = dispatch(
            "POST",
            f"{BASE}/default.aspx",
            {
                "__EVENTTARGET": "ctl00$pContent$BtnItemPersonOk",
                "Login1$LoginButton": "Вход",
            },
        )
        print("PPV_REACHED", flush=True)
        assert blocked(route), "login-button field bypassed the non-login event target"
        return

    if name == "allowed-behavior":
        cases = {
            "get": continued(dispatch("GET", f"{BASE}/register/person_card.aspx")),
            "external_asset": continued(
                dispatch("GET", "https://static.example.test/site.css")
            ),
            "login": continued(
                dispatch(
                    "POST",
                    f"{BASE}/default.aspx",
                    {"Login1$UserName": "synthetic", "Login1$LoginButton": "Вход"},
                )
            ),
            "add": continued(
                dispatch(
                    "POST",
                    f"{BASE}/register/person_list.aspx",
                    {"__EVENTTARGET": "ctl00$pContent$BtnAddPerson"},
                )
            ),
            "close": continued(
                dispatch(
                    "POST",
                    f"{BASE}/register/person_card.aspx",
                    {"__EVENTTARGET": "ctl00$pContent$BtnItemPersonCancel"},
                )
            ),
        }
        assert all(cases.values()), "an authorized or read-only request was blocked"
        print(json.dumps(cases, sort_keys=True, separators=(",", ":")))
        return

    if name == "save-remains-blocked":
        route = dispatch(
            "POST",
            f"{BASE}/register/person_card.aspx",
            {"__EVENTTARGET": "ctl00$pContent$BtnItemPersonOk"},
        )
        assert blocked(route), "prepare-only allowed a Save event"
        print("SAVE_BLOCKED")
        return

    raise SystemExit(f"unknown probe: {name}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("expected one probe name")
    check(sys.argv[1])
