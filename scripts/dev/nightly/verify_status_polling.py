#!/usr/bin/env python3
"""Verify local task-list polling load and htmx terminal stop behavior."""
from __future__ import annotations

import argparse
import json
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

SYNTHETIC_USER = "nightly-operator"
SYNTHETIC_PASSWORD = "nightly-synthetic-password-only"


def verify_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme, parsed.hostname, parsed.port) != ("http", "127.0.0.1", 18081):
        raise SystemExit("Refusing: browser polling verification is restricted to http://127.0.0.1:18081")
    return value.rstrip("/")


def verify_htmx_terminal_stop(page, base_url: str) -> dict[str, object]:
    request_count = 0

    def respond(route) -> None:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            route.fulfill(
                status=200,
                content_type="text/html",
                body=(
                    '<tbody id="task-list-body" '
                    f'hx-get="{base_url}/tasks/status-list" '
                    'hx-trigger="every 100ms" hx-swap="outerHTML">'
                    "<tr><td>active</td></tr></tbody>"
                ),
            )
        else:
            route.fulfill(
                status=286,
                content_type="text/html",
                body='<tbody id="task-list-body"><tr><td>terminal</td></tr></tbody>',
            )

    page.route("**/tasks/status-list", respond)
    page.set_content(
        "<table><tbody id=\"task-list-body\" "
        f"hx-get=\"{base_url}/tasks/status-list\" "
        "hx-trigger=\"every 100ms\" hx-swap=\"outerHTML\">"
        "<tr><td>initial</td></tr></tbody></table>"
    )
    page.evaluate("htmx.process(document.body)")
    page.wait_for_timeout(700)
    result = {
        "responses": request_count,
        "terminal_body_visible": "terminal" in page.locator("body").inner_text(),
    }
    if request_count != 2 or not result["terminal_body_visible"]:
        raise AssertionError(f"Expected one active poll and one final 286 response: {result}")
    return result


def measure_polling_rate_comparison(page, base_url: str) -> dict[str, object]:
    old_requests = 0

    def respond_old(route) -> None:
        nonlocal old_requests
        old_requests += 1
        route.fulfill(status=200, content_type="text/html", body='<span class="status">active</span>')

    old_pattern = "**/tasks/*/status"
    page.route(old_pattern, respond_old)
    old_rows = "".join(
        f'<tr><td id="status-{index}" hx-get="{base_url}/tasks/{index}/status" '
        'hx-trigger="every 2s" hx-swap="innerHTML"><span class="status">active</span></td></tr>'
        for index in range(100)
    )
    page.set_content(f"<table><tbody>{old_rows}</tbody></table>")
    page.evaluate("htmx.process(document.body)")
    page.wait_for_timeout(2300)
    page.unroute(old_pattern, respond_old)

    new_requests = 0

    def respond_new(route) -> None:
        nonlocal new_requests
        new_requests += 1
        route.fulfill(
            status=200,
            content_type="text/html",
            body=(
                '<tbody id="task-list-body" '
                f'hx-get="{base_url}/tasks/status-list" '
                'hx-trigger="every 2s" hx-swap="outerHTML"><tr><td>active</td></tr></tbody>'
            ),
        )

    new_pattern = "**/tasks/status-list"
    page.route(new_pattern, respond_new)
    page.set_content(
        f"""<table><tbody id="task-list-body" hx-get="{base_url}/tasks/status-list"
        hx-trigger="every 2s" hx-swap="outerHTML"><tr><td>active</td></tr></tbody></table>"""
    )
    page.evaluate("htmx.process(document.body)")
    page.wait_for_timeout(2300)
    page.unroute(new_pattern, respond_new)

    result = {
        "observation_window_ms_each": 2300,
        "old_per_row_poll_requests_for_100_rows": old_requests,
        "new_batch_poll_requests_for_100_rows": new_requests,
        "old_projected_requests_per_second_for_20_pages": old_requests * 20 / 2.0,
        "new_projected_requests_per_second_for_20_pages": new_requests * 20 / 2.0,
    }
    if old_requests != 100 or new_requests != 1:
        raise AssertionError(f"Unexpected browser polling counts: {result}")
    return result


def verify_application_page(page, base_url: str) -> dict[str, object]:
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    page.goto(f"{base_url}/login", wait_until="domcontentloaded")
    page.locator("#login").fill(SYNTHETIC_USER)
    page.locator("#password").fill(SYNTHETIC_PASSWORD)
    page.get_by_role("button", name="Войти").click()
    page.wait_for_url(f"{base_url}/", timeout=15000)
    body = page.locator("#task-list-body")
    body.wait_for()
    has_active_poll = body.get_attribute("hx-get") is not None
    page.wait_for_timeout(4500)
    status_requests = [
        url for url in requests
        if "/tasks/status-list" in url or url.endswith("/status")
    ]
    if not has_active_poll and status_requests:
        raise AssertionError(f"Terminal task list unexpectedly polled: {len(status_requests)} requests")
    if has_active_poll and len(status_requests) > 3:
        raise AssertionError(f"Single batch target emitted too many requests: {len(status_requests)}")
    return {
        "visible_rows": body.locator("tr").count(),
        "active_batch_poll_target": has_active_poll,
        "status_requests_over_4_5_seconds": len(status_requests),
        "non_local_hosts": sorted({urlsplit(url).netloc for url in requests if not url.startswith(base_url)}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18081")
    args = parser.parse_args()
    base_url = verify_base_url(args.base_url)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base_url}/login", wait_until="domcontentloaded")
        page.add_script_tag(url=f"{base_url}/static/htmx.min.js")
        htmx_version = page.evaluate("htmx.version")
        if htmx_version != "2.0.4":
            raise AssertionError(f"Expected bundled htmx 2.0.4, found {htmx_version}")
        rate_comparison = measure_polling_rate_comparison(page, base_url)
        htmx_stop = verify_htmx_terminal_stop(page, base_url)
        page.close()

        app_page = browser.new_page()
        application = verify_application_page(app_page, base_url)
        browser.close()
    result = {
        "scope": "synthetic local app and synthetic htmx response only",
        "htmx_version": htmx_version,
        "polling_rate_comparison": rate_comparison,
        "terminal_stop": htmx_stop,
        "application_page": application,
    }
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
