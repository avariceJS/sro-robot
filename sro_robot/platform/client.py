"""Браузерная сессия платформы: вход, ожидание постбэков, доступ к элементам.

Клиент ничего не знает о конкретных карточках: он умеет открыть страницу,
найти элемент по ключу селектора, заполнить его и нажать «Сохранить».
"""

from __future__ import annotations

import re
import time
import uuid
from urllib.parse import parse_qs, urlsplit
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

from playwright.sync_api import Locator, Page, TimeoutError as PWTimeout, sync_playwright

from ..config import Config
from .errors import SroError
from .pages import COMMON_SELECTORS

# Поднимается от поля вверх по родителям и берёт ближайшую кнопку «Сохранить».
# Так кнопка модалки не путается с кнопкой карточки, которая видна позади неё.
_MARK_NEAREST_SAVE = """(selector) => {
    const anchor = document.querySelector(selector);
    if (!anchor) return false;
    const isSave = (el) => {
        const text = (el.innerText || el.value || '').trim();
        return /^Сохранить/.test(text) && el.getBoundingClientRect().width > 0;
    };
    for (let node = anchor; node && node !== document.body; node = node.parentElement) {
        const found = Array.from(
            node.querySelectorAll('a, button, input[type=submit], input[type=button]')
        ).find(isSave);
        if (found) {
            document.querySelectorAll('[data-robot-save]')
                .forEach(e => e.removeAttribute('data-robot-save'));
            found.setAttribute('data-robot-save', '1');
            return true;
        }
    }
    return false;
}"""


class SroClient:
    def __init__(
        self,
        cfg: Optional[Config] = None,
        dry_run: bool = True,
        allow_registry_mutations: bool = False,
        prepare_only: bool = False,
    ):
        self.cfg = cfg or Config.load()
        self.dry_run = dry_run
        self.allow_registry_mutations = allow_registry_mutations
        # This one-shot mode may open a new blank card and close it without
        # saving. It never grants the generic registry mutation permission.
        self.prepare_only = prepare_only
        self._pw = None
        self._ctx = None
        self.page: Optional[Page] = None
        self._shot_counter = 0
        self._shot_session = uuid.uuid4().hex
        self._selectors: dict[str, str] = dict(COMMON_SELECTORS)

    # --- жизненный цикл -----------------------------------------------------

    def __enter__(self) -> "SroClient":
        self._pw = sync_playwright().start()
        # Постоянный профиль хранит сессию: логин нужен один раз.
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.cfg.profile_dir),
            headless=self.cfg.headless,
            slow_mo=self.cfg.slow_mo,
            viewport={"width": 1600, "height": 950},
            locale="ru-RU",
        )
        self._ctx.set_default_timeout(self.cfg.timeout_ms)
        if self.prepare_only:
            self._ctx.route("**/*", self._guard_prepare_only_request)
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        return self

    def _guard_prepare_only_request(self, route) -> None:
        """Allow only login, new-card Add, and the explicitly authorized Close."""
        request = route.request
        base = urlsplit(self.cfg.base_url)
        target = urlsplit(request.url)
        if (target.scheme, target.netloc) != (base.scheme, base.netloc):
            route.continue_()
            return
        if request.method.upper() != "POST":
            route.continue_()
            return
        values = parse_qs(request.post_data or "", keep_blank_values=True)
        event_target = values.get("__EVENTTARGET", [""])[0]
        path = target.path.casefold()
        allowed = (
            (path.endswith("/default.aspx") or path.endswith("/access/loginform.aspx"))
            and any(name.casefold().endswith("$loginbutton") for name in values)
        ) or (
            path.endswith("/register/person_list.aspx")
            and event_target == "ctl00$pContent$BtnAddPerson"
        ) or (
            path.endswith("/register/person_card.aspx")
            and event_target == "ctl00$pContent$BtnItemPersonCancel"
        )
        if allowed:
            route.continue_()
            return
        route.abort("blockedbyclient")

    def __exit__(self, *exc) -> None:
        if self._ctx:
            self._ctx.close()
        if self._pw:
            self._pw.stop()

    def shot(self, name: str) -> Path:
        self._shot_counter += 1
        path = self.cfg.out_dir / f"{self._shot_session}_{self._shot_counter:02d}_{name}.png"
        self.page.screenshot(path=str(path), full_page=True)
        return path

    def settle(self, pause: float = 0.4) -> None:
        """Пауза после постбэка: WebForms перерисовывает часть страницы."""
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        time.sleep(pause)

    # --- доступ к элементам -------------------------------------------------

    def register_selectors(self, extra: dict[str, str]) -> None:
        """Сценарий добавляет селекторы своих страниц. config.yaml всё равно главнее."""
        self._selectors.update(extra)

    def known(self, key: str) -> bool:
        return bool(self.cfg.selectors.get(key) or self._selectors.get(key))

    def sel(self, key: str) -> str:
        selector = self.cfg.selectors.get(key) or self._selectors.get(key)
        if not selector:
            raise SroError(f"Не задан селектор «{key}»")
        return selector

    def el(self, key: str) -> Locator:
        return self.page.locator(self.sel(key)).first

    def fill(self, key: str, value: str) -> bool:
        if not value:
            return False
        field = self.el(key)
        field.wait_for(state="visible", timeout=self.cfg.timeout_ms)
        field.scroll_into_view_if_needed()
        field.fill(value)
        return True

    def select_by_text(self, key: str, text: str) -> None:
        """Выбор пункта списка: сначала точным совпадением, потом по вхождению."""
        select = self.el(key)
        select.wait_for(state="visible", timeout=self.cfg.timeout_ms)
        try:
            select.select_option(label=text)
            return
        except Exception:
            pass

        options = select.locator("option")
        for i in range(options.count()):
            option_text = (options.nth(i).inner_text() or "").strip()
            if text.lower() in option_text.lower():
                select.select_option(index=i)
                return
        raise SroError(f"В списке «{key}» нет значения «{text}»")

    def click_text(self, text: str, key: str = "", exact: bool = False) -> None:
        if re.match(r"^(?:добавить|сохранить|закрыть)\b", text.strip(), re.I):
            self.require_registry_mutations(text.strip())
        if key and self.known(key):
            self.el(key).click()
        else:
            self.page.get_by_text(text, exact=exact).first.click()
        self.settle()

    # --- вход и навигация ---------------------------------------------------

    def goto(self, path: str) -> None:
        """Переход прямой ссылкой: пункты меню дублируются, клик по ним ненадёжен."""
        self.page.goto(urljoin(self.cfg.base_url, path), wait_until="domcontentloaded")
        self.settle()

    def ensure_logged_in(self) -> None:
        # Вызывается каждым шагом сценария, поэтому на уже открытой сессии
        # не перезагружает страницу.
        if self.is_logged_in():
            return

        self.page.goto(self.cfg.base_url, wait_until="domcontentloaded")
        self.settle()

        if self.is_logged_in():
            return

        if not (self.cfg.login and self.cfg.password):
            raise SroError(
                "Нужен вход. Укажите SRO_LOGIN и SRO_PASSWORD в .env "
                "или войдите руками в открытом окне браузера."
            )

        self.fill("login_input", self.cfg.login)
        self.fill("password_input", self.cfg.password)
        self.el("login_button").click()
        self.settle(1.0)

        if not self.is_logged_in():
            self.shot("login_failed")
            raise SroError("Вход не удался — посмотрите скриншот в out/")

    def is_logged_in(self) -> bool:
        body = self.page.content()
        return any(m in body for m in ("Вы вошли как", "РЕЕСТРЫ", "Реестр СРО"))

    # --- кнопки формы -------------------------------------------------------

    def click_save(self, anchor_key: str = "") -> None:
        """`anchor_key` — поле внутри модалки: кнопок «Сохранить» на экране две."""
        self.require_registry_mutations("Save")
        if anchor_key:
            marked = self.page.evaluate(_MARK_NEAREST_SAVE, self.sel(anchor_key))
            if marked:
                self.page.locator("[data-robot-save]").first.click()
                self.settle()
                return

        buttons = self.page.locator(
            "xpath=//input[(@type='submit' or @type='button') and contains(@value,'Сохранить')]"
            " | //*[self::a or self::button][contains(normalize-space(.),'Сохранить')]"
        )
        visible = [buttons.nth(i) for i in range(buttons.count()) if buttons.nth(i).is_visible()]
        if not visible:
            self.shot("save_not_found")
            raise SroError("Кнопка «Сохранить» не найдена — смотрите скриншот в out/")

        (visible[-1] if anchor_key else visible[0]).click()
        self.settle()

    def click_close(self) -> None:
        self.require_registry_mutations("Close")
        if self.known("close_button"):
            self.page.once("dialog", lambda dialog: dialog.accept())
            self.el("close_button").click()
            self.settle()
            return
        buttons = self.page.locator(
            "xpath=//input[(@type='submit' or @type='button') and contains(@value,'Закрыть')]"
            " | //*[self::a or self::button][normalize-space(.)='Закрыть']"
        )
        for i in range(buttons.count()):
            if buttons.nth(i).is_visible():
                buttons.nth(i).click()
                self.settle()
                return

    def require_registry_mutations(self, action: str) -> None:
        """Fail closed before any state-changing SRO control is clicked."""
        if self.prepare_only:
            if action.strip().casefold() in {"add", "close"}:
                return
            raise SroError(
                f"SRO {action} is disabled in prepare-only mode; Save is permanently blocked"
            )
        if not self.allow_registry_mutations:
            raise SroError(
                f"SRO {action} is disabled; use local validation or an isolated fixture"
            )
