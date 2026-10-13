"""Read one explicitly identified appointment act from the public Fedresurs UI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath
from urllib.parse import urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

SEARCH_URL = "https://bankrot.fedresurs.ru/bankrupts"
APPOINTMENT_LABEL = "утверждении арбитражного управляющего"
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_PUBLICATION_CANDIDATES = 100


@dataclass(frozen=True)
class FedresursCandidate:
    url: str
    is_appointment: bool


@dataclass(frozen=True)
class FedresursDocument:
    content: bytes
    filename: str
    debtor_profile_url: str
    publication_url: str
    selection_reason: str
    candidates: tuple[FedresursCandidate, ...]


class FedresursLookupError(RuntimeError):
    """The public UI did not yield one verified appointment document."""


def _require_public_fedresurs_page(page) -> None:
    _require_public_fedresurs_url(page.url)


def _require_public_fedresurs_url(url: str) -> None:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as exc:
        raise FedresursLookupError(
            "Fedresurs opened an unexpected page; acquisition stopped"
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"bankrot.fedresurs.ru", "fedresurs.ru"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise FedresursLookupError("Fedresurs opened an unexpected page; acquisition stopped")


def _reject_access_challenge(page) -> None:
    body = page.locator("body").inner_text(timeout=5000).casefold()
    if any(
        marker in body
        for marker in (
            "captcha",
            "проверка, что вы не робот",
            "доступ ограничен",
            "ошибка 429",
        )
    ):
        raise FedresursLookupError(
            "Fedresurs requires a user check or has limited access; acquisition stopped"
        )


def _accept_cookie_notice(page) -> None:
    consent = page.get_by_role("button", name="Принять", exact=True)
    if consent.count() and consent.is_visible():
        consent.click()


def _click_public_result(page, locator):
    """Follow a normal result link in its declared tab/window behavior."""
    href = locator.evaluate("element => element.closest('a')?.getAttribute('href') || ''")
    if href:
        _require_public_fedresurs_url(urljoin(page.url, href))
    target = locator.evaluate("element => element.closest('a')?.target || ''")
    if target == "_blank":
        with page.expect_popup(timeout=10000) as popup_event:
            locator.click()
        return popup_event.value
    locator.click()
    return page


def fetch_appointment_ruling(case_number: str) -> FedresursDocument:
    """Search through the visible public UI and download its unique appointment PDF.

    The browser deliberately remains visible because headless Chromium did not
    render the public search control during qualification. No challenge handling,
    direct API calls, or undocumented endpoints are used.
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(accept_downloads=True)
        search = context.new_page()
        search.set_default_timeout(15000)
        stage = "opening the public search page"

        try:
            search.goto(SEARCH_URL, wait_until="domcontentloaded")
            _require_public_fedresurs_page(search)
            _reject_access_challenge(search)

            stage = "accepting the site's cookie notice"
            _accept_cookie_notice(search)

            stage = "locating the public debtor-search field"
            field = search.locator("input[type='text']:visible").first
            field.wait_for(state="visible", timeout=15000)
            stage = "submitting the case-number search"
            field.fill(case_number)
            search.locator("button:visible").first.click()
            result = search.get_by_text(case_number, exact=True)
            result.wait_for(state="visible", timeout=15000)
            if result.count() != 1:
                raise FedresursLookupError(
                    "Fedresurs returned multiple or missing exact case results; review required"
                )

            stage = "opening the exact debtor profile"
            profile_link = search.get_by_text("Вся информация", exact=False).last
            profile = _click_public_result(search, profile_link)
            profile.wait_for_load_state("domcontentloaded")
            _require_public_fedresurs_page(profile)
            _reject_access_challenge(profile)
            _accept_cookie_notice(profile)

            stage = "reading the bankruptcy-publication list"
            messages = profile.locator("a[href*='/bankruptmessages/']")
            publications = profile.locator("a[href*='/publications']")
            if publications.count() == 1:
                stage = "opening the debtor's full publication list"
                profile = _click_public_result(profile, publications)
                profile.wait_for_load_state("domcontentloaded")
                _require_public_fedresurs_page(profile)
                _reject_access_challenge(profile)
                _accept_cookie_notice(profile)
                messages = profile.locator("a[href*='/bankruptmessages/']")
            elif not urlparse(profile.url).path.rstrip("/").endswith("/publications"):
                raise FedresursLookupError(
                    "Fedresurs debtor profile has no unambiguous publication-list link"
                )
            messages.first.wait_for(state="visible", timeout=10000)
            if messages.count() > MAX_PUBLICATION_CANDIDATES:
                raise FedresursLookupError(
                    "Fedresurs publication inventory exceeds the safe review limit"
                )
            candidates = messages.filter(has_text=APPOINTMENT_LABEL)
            candidate_inventory_items = []
            for index in range(messages.count()):
                candidate = messages.nth(index)
                href = candidate.get_attribute("href")
                if not href:
                    raise FedresursLookupError(
                        "Fedresurs publication candidate has no source link"
                    )
                candidate_url = urljoin(profile.url, href)
                _require_public_fedresurs_url(candidate_url)
                candidate_inventory_items.append(
                    FedresursCandidate(
                        url=candidate_url,
                        is_appointment=APPOINTMENT_LABEL.casefold()
                        in (candidate.inner_text() or "").casefold(),
                    )
                )
            candidate_inventory = tuple(candidate_inventory_items)
            if candidates.count() != 1:
                raise FedresursLookupError(
                    "Fedresurs has multiple or no appointment publications; employee selection required"
                )

            publication_href = candidates.get_attribute("href")
            if not publication_href:
                raise FedresursLookupError("Fedresurs appointment publication has no source link")
            publication_url = urljoin(profile.url, publication_href)
            _require_public_fedresurs_url(publication_url)
            stage = "opening the selected appointment publication"
            message = _click_public_result(profile, candidates)
            message.wait_for_load_state("domcontentloaded")
            _require_public_fedresurs_page(message)
            _reject_access_challenge(message)
            _accept_cookie_notice(message)
            message.get_by_text(APPOINTMENT_LABEL, exact=False).wait_for(
                state="visible", timeout=15000
            )
            message_body = message.locator("body").inner_text(timeout=5000)
            if case_number.casefold() not in message_body.casefold():
                # Russian case numbers are case-insensitive; do not accept a
                # publication that only matched by debtor name/profile.
                raise FedresursLookupError(
                    "Appointment publication does not contain the requested case number"
                )
            if APPOINTMENT_LABEL not in message_body.casefold():
                raise FedresursLookupError(
                    "Publication is not explicitly identified as an appointment act"
                )

            stage = "checking the selected publication attachment"
            documents = message.locator(".doc-name.underlined")
            if documents.count() != 1:
                raise FedresursLookupError(
                    "Appointment publication does not have exactly one document; review required"
                )
            stage = "downloading the selected PDF"
            with message.expect_download(timeout=20000) as download_event:
                documents.click()
            download = download_event.value
            suggested_name = PurePath(download.suggested_filename).name
            if not suggested_name.casefold().endswith(".pdf"):
                raise FedresursLookupError("Fedresurs attachment is not identified as a PDF")
            downloaded_path = download.path()
            if downloaded_path is None:
                raise FedresursLookupError("Fedresurs download has no local file")
            temporary_path = Path(downloaded_path)
            if temporary_path.stat().st_size > MAX_DOCUMENT_BYTES:
                raise FedresursLookupError("Fedresurs PDF exceeds the configured size limit")
            data = temporary_path.read_bytes()
            if not data.startswith(b"%PDF-"):
                raise FedresursLookupError("Fedresurs attachment is not a valid PDF")
            return FedresursDocument(
                content=data,
                filename=suggested_name,
                debtor_profile_url=profile.url,
                publication_url=publication_url,
                selection_reason=(
                    "Unique publication explicitly labelled as an arbitration-manager "
                    "appointment; exact case number verified on the publication page."
                ),
                candidates=candidate_inventory,
            )
        except PlaywrightTimeoutError as exc:
            _reject_access_challenge(search)
            raise FedresursLookupError(
                f"Fedresurs timed out while {stage}; read-only acquisition stopped"
            ) from exc
        finally:
            browser.close()
