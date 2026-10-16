"""Браузерная сессия платформы: вход, ожидание постбэков, доступ к элементам.

Клиент ничего не знает о конкретных карточках: он умеет открыть страницу,
найти элемент по ключу селектора, заполнить его и нажать «Сохранить».
"""

from __future__ import annotations

import json
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


MANUAL_LOGIN_TIMEOUT_S = 180


def _is_login_post(path: str, values: dict) -> bool:
    """True only for the ordinary SRO login postback, not later registry actions."""
    normalized = path.casefold()
    if not (normalized.endswith("/default.aspx") or normalized.endswith("/access/loginform.aspx")):
        return False
    event_targets = values.get("__EVENTTARGET", [])
    if len(event_targets) > 1:
        return False
    event_target = event_targets[0].strip().casefold() if event_targets else ""
    if event_target and event_target.rsplit("$", 1)[-1] != "loginbutton":
        return False
    button_fields = [
        name for name in values if name.casefold().rsplit("$", 1)[-1] == "loginbutton"
    ]
    return bool(event_target or len(button_fields) == 1)


class SroClient:
    def __init__(
        self,
        cfg: Optional[Config] = None,
        dry_run: bool = True,
        allow_registry_mutations: bool = False,
        prepare_only: bool = False,
        session_state: bytes | None = None,
    ):
        self.cfg = cfg or Config.load()
        self.dry_run = dry_run
        self.allow_registry_mutations = allow_registry_mutations
        # This one-shot mode may open a new blank card and close it without
        # saving. It never grants the generic registry mutation permission.
        self.prepare_only = prepare_only
        self.session_state = session_state
        self._pw = None
        self._browser = None
        self._ctx = None
        self.page: Optional[Page] = None
        self._shot_counter = 0
        self._shot_session = uuid.uuid4().hex
        self._selectors: dict[str, str] = dict(COMMON_SELECTORS)
        self.manual_login_timeout_s = MANUAL_LOGIN_TIMEOUT_S

    # --- жизненный цикл -----------------------------------------------------

    def __enter__(self) -> "SroClient":
        self._pw = sync_playwright().start()
        if self.session_state is not None:
            try:
                state = json.loads(self.session_state)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
                self._pw.stop()
                self._pw = None
                raise SroError("Внешняя сессия СРО повреждена; войдите снова.") from exc
            self._browser = self._pw.chromium.launch(
                # Web jobs run under systemd/RQ and must not depend on a desktop.
                headless=True, slow_mo=0
            )
            self._ctx = self._browser.new_context(
                storage_state=state,
                viewport={"width": 1600, "height": 950},
                locale="ru-RU",
            )
        else:
            if self.prepare_only:
                self._pw.stop()
                self._pw = None
                raise SroError("Для подготовки формы нужна связанная сессия сотрудника СРО.")
            # Legacy operator CLI only. Web jobs use per-login server-side state.
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
        method = request.method.upper()
        if method in {"GET", "HEAD", "OPTIONS"}:
            route.continue_()
            return
        if method != "POST" or (target.scheme, target.netloc) != (base.scheme, base.netloc):
            route.abort("blockedbyclient")
            return
        values = parse_qs(request.post_data or "", keep_blank_values=True)
        event_targets = values.get("__EVENTTARGET", [])
        event_target = event_targets[0] if len(event_targets) == 1 else ""
        if len(event_targets) > 1:
            route.abort("blockedbyclient")
            return
        path = target.path.casefold()
        allowed = (
            _is_login_post(path, values)
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
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            try:
                if self._browser:
                    self._browser.close()
            finally:
                try:
                    if self._pw:
                        self._pw.stop()
                finally:
                    self.session_state = None
                    self.page = None
                    self._ctx = None
                    self._browser = None
                    self._pw = None

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

        if self.session_state is not None:
            self.page.goto(self.cfg.base_url, wait_until="domcontentloaded")
            self.settle()
            if self.is_logged_in():
                return
            raise SroError("Внешняя сессия СРО истекла; войдите снова.")

        self.page.goto(self.cfg.base_url, wait_until="domcontentloaded")
        self.settle()

        if self.is_logged_in():
            return

        if self.cfg.login and self.cfg.password:
            try:
                self.fill("login_input", self.cfg.login)
                self.fill("password_input", self.cfg.password)
                self.el("login_button").click()
                self.settle(1.0)
            except Exception:
                # A changed login form must not close the window. The operator
                # can sign in on the page that is already open.
                pass
            if self.is_logged_in():
                return

        self._wait_for_manual_login()

    def _wait_for_manual_login(self) -> None:
        """Keep the visible SRO window open so the operator can sign in once."""
        deadline = time.monotonic() + self.manual_login_timeout_s
        while time.monotonic() < deadline:
            if self.page.is_closed():
                raise SroError("Окно СРО закрыто до входа.")
            try:
                if self.is_logged_in():
                    return
            except Exception:
                pass
            time.sleep(1)
        raise SroError(
            "Вход в СРО не выполнен. Добавьте логин и пароль в локальный файл настроек "
            "или войдите в открытом окне СРО в течение трёх минут."
        )

    def is_logged_in(self) -> bool:
        body = self.page.content()
        return "Вы вошли как" in body

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
