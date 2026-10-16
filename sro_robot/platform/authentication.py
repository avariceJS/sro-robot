"""One-attempt, isolated SROArbitr authentication for the office web login."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from ..config import Config
from .client import _is_login_post
from .pages import COMMON_SELECTORS

MAX_AUTH_TIMEOUT_MS = 45_000
AUTH_MARKER = "вы вошли как"
CHALLENGE_MARKERS = ("captcha", "капча", "код подтверждения", "подтвердите вход")


class SroLoginError(RuntimeError):
    """A safe, credential-free description of an external login failure."""

    def __init__(self, reason: str, stage: str = "verify"):
        if reason not in {"unavailable", "rejected", "challenge", "changed"}:
            raise ValueError("Unsupported SRO login failure reason")
        if stage not in {"launch", "open_login", "submit", "verify"}:
            raise ValueError("Unsupported SRO login stage")
        self.reason = reason
        self.stage = stage
        super().__init__("SRO authentication " + reason)


@dataclass(frozen=True)
class SroLoginResult:
    storage_state: bytes
    sro_login: str


def authenticate_sro_credentials(
    login: str,
    password: str,
    cfg: Config | None = None,
) -> SroLoginResult:
    """Verify credentials once and return only the resulting short-lived cookie state.

    The browser context is ephemeral and isolated from the shared operator profile.
    Only one ordinary WebForms login POST is permitted. Challenges are reported and
    never automated or bypassed. No screenshots, response bodies or cookie values
    are logged or written to disk.
    """
    if not isinstance(login, str) or not login.strip() or not isinstance(password, str) or not password:
        raise SroLoginError("rejected")
    cfg = cfg or Config.load()
    base = urlsplit(cfg.base_url)
    if base.scheme != "https" or not base.hostname:
        raise SroLoginError("changed")
    state: dict[str, bool] = {"login_post_seen": False}
    browser = context = None
    stage = "launch"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, timeout=MAX_AUTH_TIMEOUT_MS)
            context = browser.new_context(locale="ru-RU")
            context.set_default_timeout(12_000)

            def guard(route) -> None:
                request = route.request
                target = urlsplit(request.url)
                if (target.scheme, target.netloc) != (base.scheme, base.netloc):
                    route.abort()
                    return
                if request.method.upper() != "POST":
                    route.continue_()
                    return
                values = parse_qs(request.post_data or "", keep_blank_values=True)
                if not state["login_post_seen"] and _is_login_post(target.path, values):
                    state["login_post_seen"] = True
                    route.continue_()
                    return
                route.abort()

            context.route("**/*", guard)
            page = context.new_page()
            stage = "open_login"
            page.goto(cfg.base_url, wait_until="domcontentloaded", timeout=MAX_AUTH_TIMEOUT_MS)
            username_field = page.locator(cfg.selectors.get("login_input") or COMMON_SELECTORS["login_input"])
            password_field = page.locator(cfg.selectors.get("password_input") or COMMON_SELECTORS["password_input"])
            submit_button = page.locator(cfg.selectors.get("login_button") or COMMON_SELECTORS["login_button"])
            if not username_field.count() or not password_field.count() or not submit_button.count():
                raise SroLoginError("changed")
            username_field.fill(login)
            password_field.fill(password)
            # Drop our local reference as soon as the browser field has been filled.
            password = ""
            stage = "submit"
            submit_button.click()
            try:
                page.wait_for_load_state("domcontentloaded", timeout=20_000)
            except PlaywrightError:
                pass
            page.wait_for_timeout(700)

            stage = "verify"
            body_text = page.locator("body").inner_text(timeout=5_000).casefold()
            if any(marker in body_text for marker in CHALLENGE_MARKERS):
                raise SroLoginError("challenge")
            if not state["login_post_seen"]:
                raise SroLoginError("unavailable")
            if AUTH_MARKER not in body_text:
                raise SroLoginError("rejected")

            cookies = context.cookies()
            expected_domain = base.hostname.casefold()
            if not any(
                str(cookie.get("domain", "")).casefold().lstrip(".") == expected_domain
                for cookie in cookies
            ):
                # Credentials were accepted by the page marker, but the cookie
                # domain is not one this adapter can safely persist and reuse.
                raise SroLoginError("changed")
            serialized = json.dumps(context.storage_state(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            return SroLoginResult(serialized, login.strip())
    except SroLoginError:
        raise
    except (PlaywrightError, OSError, TimeoutError) as exc:
        error = SroLoginError("unavailable", stage)
        error.failure_type = type(exc).__name__
        raise error from None
    except Exception as exc:
        # Exception messages may contain remote page details. Keep them out of logs,
        # RQ payloads and responses.
        error = SroLoginError("unavailable", stage)
        error.failure_type = type(exc).__name__
        raise error from None
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
