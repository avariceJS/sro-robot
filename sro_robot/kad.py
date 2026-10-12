"""Получение опубликованного судебного акта через мобильную версию МКАД."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

KAD_URL = "https://mkad.arbitr.ru/Kad"
PUBLICATION_RE = re.compile(r"Дата публикации:\s*\d{2}\.\d{2}\.\d{4}", re.I)
DOCUMENT_URL_RE = re.compile(r"/(?:PdfDocument|Document/Pdf)/|\.pdf(?:$|\?)", re.I)


class KadLookupError(RuntimeError):
    """КАД не нашёл дело или не отдал опубликованный акт."""


def _page_warning(page) -> str:
    body = page.locator("body").inner_text(timeout=5000)
    if not body.strip():
        return "МКАД вернул пустую страницу; получение судебного акта остановлено"
    if re.search(r"\b429\b", body):
        return (
            "КАД/МКАД сообщил ошибку 429; поиск остановлен без автоматического повтора. "
            "Проверьте доступность сервиса и сетевой маршрут"
        )
    for phrase in ("captcha", "проверка, что вы не робот", "доступ ограничен"):
        if phrase.casefold() in body.casefold():
            return "МКАД требует проверку пользователя или ограничил доступ"
    return ""


def _dismiss_service_notice(page) -> None:
    """Закрывает только информационное уведомление о плановых сбоях КАД."""
    wrapper = page.locator(".b-promo_notification-popup_wrapper")
    if wrapper.count() and wrapper.is_visible():
        body = wrapper.inner_text(timeout=2000).casefold()
        # Закрываем только информационное уведомление КАД, не произвольные окна.
        if "сложности в работе" in body:
            exact_close = wrapper.locator(
                ".b-promo_notification-popup-close, .js-promo_notification-popup-close"
            ).first
            if exact_close.count() and exact_close.is_visible():
                exact_close.click(timeout=3000)
                wrapper.wait_for(state="hidden", timeout=5000)
                return
        return

    close_controls = page.locator(
        "[aria-label='Закрыть'], [title='Закрыть'], .close, button"
    )
    for control in close_controls.all():
        try:
            if not control.is_visible():
                continue
            label = " ".join(filter(None, (
                control.inner_text(timeout=500),
                control.get_attribute("aria-label"),
                control.get_attribute("title"),
                control.get_attribute("class"),
            )))
            if re.search(r"закрыть|close|(^|\s)[×✕x](\s|$)|close", label, re.I):
                control.click(timeout=2000)
                return
        except PlaywrightTimeoutError:
            continue


def _expand_event_list(page) -> None:
    """Нажимает доступную кнопку раскрытия списка, если КАД её показывает."""
    controls = page.locator("button, [role=button], a")
    for control in controls.all():
        try:
            if not control.is_visible():
                continue
            details = " ".join(filter(None, (
                control.inner_text(timeout=500),
                control.get_attribute("aria-label"),
                control.get_attribute("title"),
                control.get_attribute("class"),
                control.get_attribute("id"),
            )))
            if re.search(r"показать|развер|раскры|ещ[её]|загрузить|load.?more|expand|plus", details, re.I):
                control.click(timeout=2000)
                page.wait_for_load_state("domcontentloaded", timeout=5000)
                return
        except PlaywrightTimeoutError:
            continue


def _latest_document_link(page):
    """Возвращает первую ссылку акта из списка, упорядоченного КАД по новизне."""
    links = page.locator("a[href]")
    all_links = []
    published = []
    files = []
    for link in links.all():
        try:
            if not link.is_visible():
                continue
            text = link.inner_text(timeout=500)
            href = link.get_attribute("href") or ""
            all_links.append((link, href, text))
            if PUBLICATION_RE.search(text):
                published.append((link, href))
            if DOCUMENT_URL_RE.search(href):
                files.append((link, href))
        except PlaywrightTimeoutError:
            continue

    # Дата публикации — видимый пользователю признак, что акт уже размещён.
    # КАД отдаёт события сверху вниз, поэтому первая такая ссылка самая новая.
    if published:
        publication_link, publication_href = published[0]
        title_href = publication_link.evaluate("""el => {
          let parent = el.parentElement;
          for (let depth = 0; parent && depth < 8; depth++, parent = parent.parentElement) {
        const title = [...parent.querySelectorAll('a[href]')].find(a =>
              a !== el && a.innerText.trim().length > 30 && !/Дата публикации:/i.test(a.innerText));
            if (title) return title.href;
          }
          return '';
        }""")
        if title_href:
            for link, href, _ in all_links:
                if urljoin(page.url, href) == title_href:
                    return link, href
        return publication_link, publication_href
    if files:
        return files[0]
    return None


def _pdf_from_response(response) -> bytes | None:
    try:
        if response.status >= 400:
            return None
        data = response.body()
    except Exception:
        return None
    return data if data.startswith(b"%PDF-") else None


def _is_case_card_response(response) -> bool:
    return urlparse(response.url).path.rstrip("/").casefold() == "/kad/card"


def fetch_latest_ruling(case_number: str) -> tuple[bytes, str]:
    """Ищет дело на мобильной странице МКАД и скачивает опубликованный акт."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(15000)
        pdf_responses = []
        downloads = []
        popups = []
        page.on("response", lambda response: pdf_responses.append(response)
                if "pdf" in response.headers.get("content-type", "").casefold()
                or DOCUMENT_URL_RE.search(response.url) else None)
        page.on("download", lambda download: downloads.append(download))
        page.on("popup", lambda popup: popups.append(popup))

        try:
            page.goto(KAD_URL, wait_until="domcontentloaded")
            warning = _page_warning(page)
            if warning:
                raise KadLookupError(warning)
            _dismiss_service_notice(page)

            page.locator("input[name='CaseNumbers[]']").fill(case_number)
            # Уведомление может появиться с задержкой и накрыть кнопку уже после ввода.
            _dismiss_service_notice(page)
            page.locator(
                "form[action='/Kad/Search'] input[type='submit'][value='Найти']"
            ).click()
            try:
                page.wait_for_function("window.location.hash === '#results'", timeout=30000)
                case_link = page.get_by_role("link", name=case_number, exact=True).first
                case_link.wait_for(state="visible", timeout=30000)
            except PlaywrightTimeoutError as exc:
                warning = _page_warning(page)
                if warning:
                    raise KadLookupError(warning) from exc
                notice = page.locator("body").inner_text(timeout=5000)
                if "сложности в работе" in notice.casefold():
                    raise KadLookupError("КАД сегодня сообщает о сбоях; поиск дела не завершился") from exc
                if "укажите атрибуты дела для поиска" in notice.casefold():
                    raise KadLookupError(
                        "КАД не обработал поисковый запрос; проверьте работу сайта и повторите позже"
                    ) from exc
                raise KadLookupError(f"Дело {case_number} не найдено в КАД") from exc

            with page.expect_response(
                _is_case_card_response,
                timeout=30000,
            ):
                case_link.click()
            warning = _page_warning(page)
            if warning:
                raise KadLookupError(warning)
            _dismiss_service_notice(page)

            _expand_event_list(page)
            candidate = _latest_document_link(page)
            if candidate is None:
                raise KadLookupError("В карточке дела не найден опубликованный судебный акт")

            link, href = candidate
            document_url = urljoin(page.url, href) if href else ""
            link.click()
            page.wait_for_timeout(1200)

            # PDF может открыться во вкладке, встроенном просмотрщике или как загрузка.
            for response in reversed(pdf_responses):
                data = _pdf_from_response(response)
                if data:
                    return data, urlparse(response.url).path.rsplit("/", 1)[-1] or "ruling.pdf"

            for download in downloads:
                path = download.path()
                if path:
                    data = open(path, "rb").read()
                    if data.startswith(b"%PDF-"):
                        return data, download.suggested_filename or "ruling.pdf"

            for popup in popups:
                popup.wait_for_load_state("domcontentloaded", timeout=10000)
                embedded = popup.locator("iframe[src], object[data], embed[src]")
                if embedded.count():
                    src = (embedded.first.get_attribute("src")
                           or embedded.first.get_attribute("data") or "")
                    document_url = urljoin(popup.url, src)
                elif DOCUMENT_URL_RE.search(popup.url):
                    document_url = popup.url
                if document_url:
                    response = context.request.get(document_url, timeout=30000)
                    data = _pdf_from_response(response)
                    if data:
                        return data, urlparse(document_url).path.rsplit("/", 1)[-1] or "ruling.pdf"

            if document_url:
                response = context.request.get(document_url, timeout=30000)
                data = _pdf_from_response(response)
                if data:
                    return data, urlparse(document_url).path.rsplit("/", 1)[-1] or "ruling.pdf"
            raise KadLookupError("КАД открыл документ, но PDF-файл получить не удалось")
        finally:
            context.close()
            browser.close()
