"""Браузерная сессия платформы: вход, ожидание постбэков, доступ к элементам.

Клиент ничего не знает о конкретных карточках: он умеет открыть страницу,
найти элемент по ключу селектора, заполнить его и нажать «Сохранить».
"""

from __future__ import annotations

import json
import os
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
PERSON_SAVE_TARGET = "ctl00$pContent$BtnItemPersonOk"
PERSON_SAVE_SELECTOR = "#ctl00_pContent_BtnItemPersonOk"
FIRM_SAVE_TARGET = "ctl00$pContent$BtnItemFirmOk"
FIRM_SAVE_SELECTOR = "#ctl00_pContent_BtnItemFirmOk"
FIRM_ADDRESS_ADD_TARGET = "ctl00$pContent$TabContainer1$TabPanel6$ctrl_contact1$btnAddAddress"
FIRM_ADDRESS_CANCEL_TARGET = "ctl00$pContent$TabContainer1$TabPanel6$ctrl_contact1$BtnItemAddressCancel"
FIRM_ADDRESS_SAVE_TARGET = "ctl00$pContent$TabContainer1$TabPanel6$ctrl_contact1$BtnItemAddressOk"
FIRM_ADDRESS_SAVE_SELECTOR = "#ctl00_pContent_TabContainer1_TabPanel6_ctrl_contact1_BtnItemAddressOk"
PERSON_ADDRESS_ADD_TARGET = "ctl00$pContent$TabContainer1$TabPanel5$ctrl_contact1$btnAddAddress"
PERSON_ADDRESS_CANCEL_TARGET = "ctl00$pContent$TabContainer1$TabPanel5$ctrl_contact1$BtnItemAddressCancel"
PERSON_ADDRESS_SAVE_TARGET = "ctl00$pContent$TabContainer1$TabPanel5$ctrl_contact1$BtnItemAddressOk"
PERSON_ADDRESS_SAVE_SELECTOR = "#ctl00_pContent_TabContainer1_TabPanel5_ctrl_contact1_BtnItemAddressOk"
# Read-only reconciliation on 2026-11-05 confirmed firm Add persists a blank
# GUID even after Close. The owner authorized technical drafts; exact owned-GUID
# Add/Cancel/Close preview was qualified. Operator Save remains a separate
# one-shot, read-back checked action and has not been exercised on real SRO.
FIRM_PREVIEW_QUALIFIED = True
# Person nested address controls are source-mapped but their live Add/Cancel
# effect has not yet been reconciled by exact-GUID readback.
PERSON_ADDRESS_PREVIEW_QUALIFIED = False


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
        operator_save_enabled: bool = False,
        owned_add_required: bool = False,
    ):
        self.cfg = cfg or Config.load()
        self.dry_run = dry_run
        self.allow_registry_mutations = allow_registry_mutations
        # Guarded mode permits scoped Add/Close. Firm Add persists a technical
        # blank draft; a separate operator decision is required for Save.
        self.prepare_only = prepare_only
        if operator_save_enabled and not prepare_only:
            raise SroError("Operator Save requires guarded preview mode")
        self.operator_save_enabled = operator_save_enabled
        self.owned_add_required = owned_add_required
        if owned_add_required and not prepare_only:
            raise SroError("Owned Add requires guarded preview mode")
        self._operator_save_url: str | None = None
        self._operator_save_used = False
        self._operator_save_request_pending = False
        self._operator_save_request = None
        self._operator_save_target: str | None = None
        self._new_person_form_url: str | None = None
        self._new_firm_form_url: str | None = None
        self._owned_person_form_url: str | None = None
        self._owned_person_preview_url: str | None = None
        self._owned_firm_form_url: str | None = None
        self._owned_firm_preview_url: str | None = None
        self._firm_add_pending = False
        self._firm_add_used = False
        self._person_add_pending = False
        self._person_add_used = False
        self._firm_address_add_pending = False
        self._firm_address_add_used = False
        self._firm_address_cancel_pending = False
        self._firm_address_cancel_used = False
        self._firm_address_save_pending = False
        self._firm_address_save_used = False
        self._firm_address_save_click_used = False
        self._firm_address_save_request = None
        self._person_address_add_pending = False
        self._person_address_add_used = False
        self._person_address_cancel_pending = False
        self._person_address_cancel_used = False
        self._person_address_save_pending = False
        self._person_address_save_used = False
        self._person_address_save_click_used = False
        self._person_address_save_request = None
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
        """Allow preview requests and at most one exact operator-approved Save."""
        request = route.request
        base = urlsplit(self.cfg.base_url)
        target = urlsplit(request.url)
        method = request.method.upper()
        if method in {"GET", "HEAD", "OPTIONS"}:
            # The assignment-list Add link points at assignment_card.aspx
            # without a record identifier. Its GET semantics are unverified,
            # so fail closed even though ordinary read-only GETs are allowed.
            assignment_query = parse_qs(target.query, keep_blank_values=True)
            if target.path.casefold().endswith("/register/assignment_card.aspx") and not (
                len(assignment_query.get("id", [])) == 1 and assignment_query["id"][0].strip()
            ):
                route.abort("blockedbyclient")
                return
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
        control_fields = [key for key in values if re.search(r"(?:^|[$_])btn", key, re.I)]
        if self._operator_save_request_pending and request.url == self._operator_save_url:
            save_target = self._operator_save_target
            save_fields = values.get(save_target, []) if save_target else []
            exact_save = (
                (event_target == save_target and not save_fields)
                or (not event_target and len(save_fields) == 1)
            ) and bool(save_target) and all(key == save_target for key in control_fields)
            exact_save = exact_save and not any(values.get("__EVENTARGUMENT", []))
            if (exact_save and request.frame == self.page.main_frame
                    and (save_target != FIRM_SAVE_TARGET or FIRM_PREVIEW_QUALIFIED)):
                # Consume before network: a timeout or uncertain response never
                # grants another request, including an automatic browser retry.
                self._operator_save_request_pending = False
                self._operator_save_request = request
                route.continue_()
                return
        path = target.path.casefold()
        allowed = (
            _is_login_post(path, values)
        ) or (
            path.endswith("/register/person_list.aspx")
            and event_target == "ctl00$pContent$ctrl_search$BtnSearch"
            and not any(values.get("__EVENTARGUMENT", []))
        ) or (
            path.endswith("/register/person_list.aspx")
            and event_target == "ctl00$pContent$BtnAddPerson"
            and (not self.owned_add_required or (
                path == "/register/person_list.aspx"
                and not target.query
                and request.frame == self.page.main_frame
                and not any(values.get("__EVENTARGUMENT", []))
                and self._person_add_pending and not self._person_add_used
            ))
        ) or (
            path.endswith("/register/person_card.aspx")
            and event_target == "ctl00$pContent$BtnItemPersonCancel"
            and (not self.owned_add_required or (
                path == "/register/person_card.aspx"
                and request.url in {self._new_person_form_url, self._owned_person_preview_url}
                and request.frame == self.page.main_frame
                and not any(values.get("__EVENTARGUMENT", []))
            ))
        ) or (
            path == "/register/person_card.aspx"
            and (request.url in {self._new_person_form_url, self._owned_person_preview_url}
                 or request.url == self._owned_person_form_url)
            and request.frame == self.page.main_frame
            and PERSON_ADDRESS_PREVIEW_QUALIFIED and self.owned_add_required
            and self._person_address_add_pending and not self._person_address_add_used
            and event_target == PERSON_ADDRESS_ADD_TARGET
            and not any(values.get("__EVENTARGUMENT", []))
        ) or (
            path == "/register/person_card.aspx"
            and (request.url in {self._new_person_form_url, self._owned_person_preview_url}
                 or (request.url == self._owned_person_form_url and self.operator_save_enabled
                     and not self._person_address_save_click_used))
            and request.frame == self.page.main_frame
            and PERSON_ADDRESS_PREVIEW_QUALIFIED and self.owned_add_required
            and self._person_address_cancel_pending and not self._person_address_cancel_used
            and event_target == PERSON_ADDRESS_CANCEL_TARGET
            and not any(values.get("__EVENTARGUMENT", []))
        ) or (
            path == "/register/person_card.aspx"
            and request.url == self._owned_person_form_url
            and request.frame == self.page.main_frame
            and PERSON_ADDRESS_PREVIEW_QUALIFIED and self.owned_add_required
            and self.operator_save_enabled
            and self._person_address_save_pending and not self._person_address_save_used
            and event_target == PERSON_ADDRESS_SAVE_TARGET
            and not any(values.get("__EVENTARGUMENT", []))
        ) or (
            path == "/register/firm_list.aspx"
            and not target.query
            and request.frame == self.page.main_frame
            and event_target == "ctl00$pContent$ctrl_search$BtnSearch"
            and not any(values.get("__EVENTARGUMENT", []))
        ) or (
            path == "/register/firm_list.aspx"
            and not target.query
            and request.frame == self.page.main_frame
            and FIRM_PREVIEW_QUALIFIED
            and self.owned_add_required
            and self._firm_add_pending
            and not self._firm_add_used
            and self._new_firm_form_url is None
            and event_target == "ctl00$pContent$BtnAddFirm"
            and not any(values.get("__EVENTARGUMENT", []))
        ) or (
            path == "/register/firm_card.aspx"
            and request.url in {self._new_firm_form_url, self._owned_firm_preview_url}
            and request.frame == self.page.main_frame
            and event_target == "ctl00$pContent$BtnItemFirmCancel"
            and not any(values.get("__EVENTARGUMENT", []))
        ) or (
            path == "/register/firm_card.aspx"
            and (request.url in {self._new_firm_form_url, self._owned_firm_preview_url}
                 or request.url == self._owned_firm_form_url)
            and request.frame == self.page.main_frame
            and FIRM_PREVIEW_QUALIFIED and self.owned_add_required
            and self._firm_address_add_pending and not self._firm_address_add_used
            and event_target == FIRM_ADDRESS_ADD_TARGET
            and not any(values.get("__EVENTARGUMENT", []))
        ) or (
            path == "/register/firm_card.aspx"
            and (request.url in {self._new_firm_form_url, self._owned_firm_preview_url}
                 or (request.url == self._owned_firm_form_url and self.operator_save_enabled
                     and not self._firm_address_save_click_used))
            and request.frame == self.page.main_frame
            and FIRM_PREVIEW_QUALIFIED and self.owned_add_required
            and self._firm_address_cancel_pending and not self._firm_address_cancel_used
            and event_target == FIRM_ADDRESS_CANCEL_TARGET
            and not any(values.get("__EVENTARGUMENT", []))
        ) or (
            path == "/register/firm_card.aspx"
            and request.url == self._owned_firm_form_url
            and request.frame == self.page.main_frame
            and FIRM_PREVIEW_QUALIFIED and self.owned_add_required
            and self.operator_save_enabled
            and self._firm_address_save_pending and not self._firm_address_save_used
            and event_target == FIRM_ADDRESS_SAVE_TARGET
            and not any(values.get("__EVENTARGUMENT", []))
        )
        if allowed and all(key == event_target for key in control_fields):
            if path.endswith("/register/person_list.aspx") and event_target == "ctl00$pContent$BtnAddPerson" and self._person_add_pending:
                self._person_add_pending = False
                self._person_add_used = True
            if path == "/register/person_card.aspx" and event_target == PERSON_ADDRESS_ADD_TARGET:
                self._person_address_add_pending = False
                self._person_address_add_used = True
            if path == "/register/person_card.aspx" and event_target == PERSON_ADDRESS_CANCEL_TARGET:
                self._person_address_cancel_pending = False
                self._person_address_cancel_used = True
            if path == "/register/person_card.aspx" and event_target == PERSON_ADDRESS_SAVE_TARGET:
                self._person_address_save_pending = False
                self._person_address_save_used = True
                self._person_address_save_request = request
            if path == "/register/firm_list.aspx" and event_target == "ctl00$pContent$BtnAddFirm":
                # Burn the committed Add claim before network. An uncertain
                # response cannot cause another technical record on retry.
                self._firm_add_pending = False
                self._firm_add_used = True
            if path == "/register/firm_card.aspx" and event_target == FIRM_ADDRESS_ADD_TARGET:
                self._firm_address_add_pending = False
                self._firm_address_add_used = True
            if path == "/register/firm_card.aspx" and event_target == FIRM_ADDRESS_CANCEL_TARGET:
                self._firm_address_cancel_pending = False
                self._firm_address_cancel_used = True
            if path == "/register/firm_card.aspx" and event_target == FIRM_ADDRESS_SAVE_TARGET:
                self._firm_address_save_pending = False
                self._firm_address_save_used = True
                self._firm_address_save_request = request
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
                    self._operator_save_url = None
                    self._operator_save_request_pending = False
                    self._operator_save_request = None
                    self._operator_save_target = None
                    self._new_person_form_url = None
                    self._new_firm_form_url = None
                    self._owned_person_form_url = None
                    self._owned_person_preview_url = None
                    self._owned_firm_form_url = None
                    self._owned_firm_preview_url = None
                    self._firm_add_pending = False
                    self._firm_add_used = False
                    self._person_add_pending = False
                    self._person_add_used = False
                    self._firm_address_add_pending = False
                    self._firm_address_add_used = False
                    self._firm_address_cancel_pending = False
                    self._firm_address_cancel_used = False
                    self._firm_address_save_pending = False
                    self._firm_address_save_used = False
                    self._firm_address_save_click_used = False
                    self._firm_address_save_request = None
                    self._person_address_add_pending = False
                    self._person_address_add_used = False
                    self._person_address_cancel_pending = False
                    self._person_address_cancel_used = False
                    self._person_address_save_pending = False
                    self._person_address_save_used = False
                    self._person_address_save_click_used = False
                    self._person_address_save_request = None
                    self.session_state = None
                    self.page = None
                    self._ctx = None
                    self._browser = None
                    self._pw = None

    def shot(self, name: str) -> Path:
        if not re.fullmatch(r"[a-z0-9_]{1,32}", name):
            raise SroError("Invalid screenshot label")
        self._shot_counter += 1
        path = self.cfg.out_dir / f"{self._shot_session}_{self._shot_counter:02d}_{name}.png"
        screenshot = self.page.screenshot(full_page=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as artifact:
                descriptor = -1
                artifact.write(screenshot)
                artifact.flush()
                os.fsync(artifact.fileno())
            if hasattr(os, "O_DIRECTORY"):
                directory_fd = os.open(
                    self.cfg.out_dir,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
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
            raise SroError("Внешняя сессия СРО истекла; войдите снова.", code="external_session_expired")

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

    def arm_owned_firm_add(self) -> None:
        """Called only after the task's durable one-shot Add claim commits."""
        if (
            not FIRM_PREVIEW_QUALIFIED or not self.prepare_only
            or not self.owned_add_required
            or self.operator_save_enabled
            or self._firm_add_pending or self._firm_add_used
            or self._new_firm_form_url is not None
        ):
            raise SroError("Owned firm Add claim is invalid")
        self._firm_add_pending = True

    def arm_owned_person_add(self) -> None:
        """Called only after the task's durable one-shot person Add claim."""
        if (
            not self.prepare_only or not self.owned_add_required
            or self.operator_save_enabled
            or self._person_add_pending or self._person_add_used
            or self._new_person_form_url is not None
        ):
            raise SroError("Owned person Add claim is invalid")
        self._person_add_pending = True

    def require_owned_person_add_armed(self) -> None:
        if not self._person_add_pending or self._person_add_used:
            raise SroError("Owned person Add was not armed after task claim")

    def require_owned_firm_add_armed(self) -> None:
        if not self._firm_add_pending or self._firm_add_used:
            raise SroError("Owned firm Add was not armed after task claim")

    def arm_owned_firm_address_add(self) -> None:
        """Allow one nested address editor open on this owned preview GUID."""
        from .firm_form import firm_record_url

        if (
            not FIRM_PREVIEW_QUALIFIED or not self.prepare_only
            or not self.owned_add_required or self.operator_save_enabled
            or self._firm_address_add_pending or self._firm_address_add_used
            or self.page.url not in {self._new_firm_form_url, self._owned_firm_preview_url}
            or not firm_record_url(self.page.url, self.cfg.base_url)
        ):
            raise SroError("Owned firm address Add is not authorized")
        self._firm_address_add_pending = True

    def arm_owned_person_address_add(self) -> None:
        """Open one nested editor on the owned person preview GUID."""
        if (
            not PERSON_ADDRESS_PREVIEW_QUALIFIED or not self.prepare_only
            or not self.owned_add_required or self.operator_save_enabled
            or self._person_address_add_pending or self._person_address_add_used
            or self.page.url not in {self._new_person_form_url, self._owned_person_preview_url}
            or not urlsplit(self.page.url).query
        ):
            raise SroError("Owned person address Add is not authorized")
        self._person_address_add_pending = True

    def arm_owned_firm_address_cancel(self) -> None:
        """Allow one exact nested Cancel after its editor Add was consumed."""
        if (
            not FIRM_PREVIEW_QUALIFIED or not self.prepare_only
            or not self.owned_add_required or self.operator_save_enabled
            or not self._firm_address_add_used
            or self._firm_address_cancel_pending or self._firm_address_cancel_used
            or self.page.url not in {self._new_firm_form_url, self._owned_firm_preview_url}
        ):
            raise SroError("Owned firm address Cancel is not authorized")
        self._firm_address_cancel_pending = True

    def arm_owned_person_address_cancel(self) -> None:
        """Cancel one opened person editor before any nested Save."""
        if (
            not PERSON_ADDRESS_PREVIEW_QUALIFIED or not self.prepare_only
            or not self.owned_add_required or self.operator_save_enabled
            or not self._person_address_add_used
            or self._person_address_cancel_pending or self._person_address_cancel_used
            or self.page.url not in {self._new_person_form_url, self._owned_person_preview_url}
        ):
            raise SroError("Owned person address Cancel is not authorized")
        self._person_address_cancel_pending = True

    def arm_operator_firm_address_add(self) -> None:
        """Allow one editor Add on the operator-authorized owned firm GUID."""
        from .firm_form import firm_record_url

        if (
            not FIRM_PREVIEW_QUALIFIED or not self.prepare_only
            or not self.owned_add_required or not self.operator_save_enabled
            or self._operator_save_url is not None
            or self._firm_address_add_pending or self._firm_address_add_used
            or self.page.url != self._owned_firm_form_url
            or not firm_record_url(self.page.url, self.cfg.base_url)
        ):
            raise SroError("Operator firm address Add is not authorized")
        self._firm_address_add_pending = True

    def arm_operator_person_address_add(self) -> None:
        """Open one editor after the operator's committed person Save claim."""
        if (
            not PERSON_ADDRESS_PREVIEW_QUALIFIED or not self.prepare_only
            or not self.owned_add_required or not self.operator_save_enabled
            or self._operator_save_url is not None
            or self._person_address_add_pending or self._person_address_add_used
            or self.page.url != self._owned_person_form_url
        ):
            raise SroError("Operator person address Add is not authorized")
        self._person_address_add_pending = True

    def arm_operator_firm_address_cancel(self) -> None:
        """Close an editor that failed before its nested Save was attempted."""
        if (
            not FIRM_PREVIEW_QUALIFIED or not self.prepare_only
            or not self.owned_add_required or not self.operator_save_enabled
            or self._operator_save_url is not None
            or not self._firm_address_add_used
            or self._firm_address_save_click_used
            or self._firm_address_cancel_pending or self._firm_address_cancel_used
            or self.page.url != self._owned_firm_form_url
        ):
            raise SroError("Operator firm address Cancel is not authorized")
        self._firm_address_cancel_pending = True

    def arm_operator_person_address_cancel(self) -> None:
        """Cancel the person editor only before its nested Save attempt."""
        if (
            not PERSON_ADDRESS_PREVIEW_QUALIFIED or not self.prepare_only
            or not self.owned_add_required or not self.operator_save_enabled
            or self._operator_save_url is not None
            or not self._person_address_add_used
            or self._person_address_save_click_used
            or self._person_address_cancel_pending or self._person_address_cancel_used
            or self.page.url != self._owned_person_form_url
        ):
            raise SroError("Operator person address Cancel is not authorized")
        self._person_address_cancel_pending = True

    def click_operator_firm_address_save(self) -> None:
        """Consume the nested address Save before network; never auto retry."""
        if (
            not FIRM_PREVIEW_QUALIFIED or not self.prepare_only
            or not self.owned_add_required or not self.operator_save_enabled
            or self._operator_save_url is not None
            or not self._firm_address_add_used
            or self._firm_address_cancel_used
            or self._firm_address_save_click_used
            or self.page.url != self._owned_firm_form_url
        ):
            raise SroError("Operator firm address Save is not authorized")
        self._firm_address_save_click_used = True
        self._firm_address_save_pending = True
        try:
            self.page.locator(FIRM_ADDRESS_SAVE_SELECTOR).click()
            self.settle(1.0)
            response = (self._firm_address_save_request.response()
                        if self._firm_address_save_request else None)
            if response is None or not 200 <= response.status < 400:
                raise SroError("Результат сохранения адреса СРО неизвестен; повтор запрещён")
        finally:
            self._firm_address_save_pending = False

    def click_operator_person_address_save(self) -> None:
        """Consume the nested person-address Save before network; no retry."""
        if (
            not PERSON_ADDRESS_PREVIEW_QUALIFIED or not self.prepare_only
            or not self.owned_add_required or not self.operator_save_enabled
            or self._operator_save_url is not None
            or not self._person_address_add_used
            or self._person_address_cancel_used
            or self._person_address_save_click_used
            or self.page.url != self._owned_person_form_url
        ):
            raise SroError("Operator person address Save is not authorized")
        self._person_address_save_click_used = True
        self._person_address_save_pending = True
        try:
            self.page.locator(PERSON_ADDRESS_SAVE_SELECTOR).click()
            self.settle(1.0)
            response = (self._person_address_save_request.response()
                        if self._person_address_save_request else None)
            if response is None or not 200 <= response.status < 400:
                raise SroError("Результат сохранения адреса СРО неизвестен; повтор запрещён")
        finally:
            self._person_address_save_pending = False

    def bind_owned_person_form(self, form_url: str) -> None:
        """Bind a fresh-GET person GUID already committed to the task ledger."""
        current = urlsplit(form_url)
        base = urlsplit(self.cfg.base_url)
        identity = parse_qs(current.query, keep_blank_values=True)
        if (
            not self.prepare_only or not self.operator_save_enabled
            or not self.owned_add_required
            or self._owned_person_form_url is not None
            or self.page.url != form_url
            or (current.scheme, current.netloc) != (base.scheme, base.netloc)
            or current.path.casefold() != "/register/person_card.aspx"
            or set(identity) != {"id"} or len(identity["id"]) != 1
            or not identity["id"][0].strip()
            or not current.query.startswith("id=") or "&" in current.query
            or ";" in current.query or current.fragment
        ):
            raise SroError("Owned person form identity is invalid")
        self._owned_person_form_url = form_url

    def bind_owned_person_preview(self, form_url: str) -> None:
        """Allow Close only on an exact, read-back checked owned person GUID."""
        current = urlsplit(form_url)
        base = urlsplit(self.cfg.base_url)
        identity = parse_qs(current.query, keep_blank_values=True)
        if (
            not self.prepare_only or not self.owned_add_required
            or self.operator_save_enabled
            or self._owned_person_preview_url is not None
            or self.page.url != form_url
            or (current.scheme, current.netloc) != (base.scheme, base.netloc)
            or current.path.casefold() != "/register/person_card.aspx"
            or set(identity) != {"id"} or len(identity["id"]) != 1
            or not identity["id"][0].strip()
            or not current.query.startswith("id=") or "&" in current.query
            or ";" in current.query or current.fragment
        ):
            raise SroError("Owned person preview identity is invalid")
        self._owned_person_preview_url = form_url

    def bind_owned_firm_form(self, form_url: str) -> None:
        """Bind only the exact firm GUID committed before this browser opened."""
        from .firm_form import firm_record_url

        if (
            not FIRM_PREVIEW_QUALIFIED
            or not self.prepare_only or not self.operator_save_enabled
            or not self.owned_add_required
            or self._owned_firm_form_url is not None
            or self.page.url != form_url
            or not firm_record_url(form_url, self.cfg.base_url)
        ):
            raise SroError("Owned firm form identity is invalid")
        self._owned_firm_form_url = form_url

    def bind_owned_firm_preview(self, form_url: str) -> None:
        """Allow Close only on an exact, read-back checked owned firm GUID."""
        from .firm_form import firm_record_url

        if (
            not FIRM_PREVIEW_QUALIFIED
            or not self.prepare_only or not self.owned_add_required
            or self.operator_save_enabled
            or self._owned_firm_preview_url is not None
            or self.page.url != form_url
            or not firm_record_url(form_url, self.cfg.base_url)
        ):
            raise SroError("Owned firm preview identity is invalid")
        self._owned_firm_preview_url = form_url

    def arm_operator_person_save(self, form_url: str) -> None:
        """Called only after the durable employee decision and form read-back."""
        current = urlsplit(form_url)
        base = urlsplit(self.cfg.base_url)
        identity = parse_qs(current.query, keep_blank_values=True)
        add_bound_identity = (
            form_url in {self._new_person_form_url, self._owned_person_form_url}
            and set(identity) == {"id"}
            and len(identity["id"]) == 1
            and bool(identity["id"][0].strip())
        )
        if (
            not self.prepare_only
            or not self.operator_save_enabled
            or self._operator_save_used
            or self._operator_save_url is not None
            or self.page.url != form_url
            or (current.scheme, current.netloc) != (base.scheme, base.netloc)
            or current.path.casefold() != "/register/person_card.aspx"
            or (self.owned_add_required and form_url != self._owned_person_form_url)
            or (current.query and not add_bound_identity)
            or current.fragment
        ):
            raise SroError("Operator Save authorization does not match the new person form")
        self._operator_save_url = form_url
        self._operator_save_target = PERSON_SAVE_TARGET

    def click_operator_person_save(self) -> None:
        """Use only the inspected person-card Save; never a nested address Save."""
        if (
            not self.operator_save_enabled
            or not self.prepare_only
            or (self.owned_add_required and self._operator_save_url != self._owned_person_form_url)
            or self._operator_save_used
            or self._operator_save_url is None
            or self.page.url != self._operator_save_url
        ):
            raise SroError("Operator Save is not authorized for this form")
        self._operator_save_used = True
        self._operator_save_request_pending = True
        try:
            self.page.locator(PERSON_SAVE_SELECTOR).click()
            self.settle(1.0)
            response = self._operator_save_request.response() if self._operator_save_request else None
            if response is None or not 200 <= response.status < 400:
                raise SroError("Результат запроса сохранения СРО неизвестен; повтор запрещён")
        finally:
            self._operator_save_request_pending = False

    def arm_operator_firm_save(self, form_url: str) -> None:
        """Bind exactly one operator decision to the fresh, Add-created firm ID."""
        from .firm_form import firm_record_url

        if (
            not self.prepare_only
            or not self.operator_save_enabled
            or not self.owned_add_required
            or not FIRM_PREVIEW_QUALIFIED
            or self._operator_save_used
            or self._operator_save_url is not None
            or form_url != self._owned_firm_form_url
            or self.page.url != form_url
            or not firm_record_url(form_url, self.cfg.base_url)
        ):
            raise SroError("Operator Save authorization does not match the new firm form")
        self._operator_save_url = form_url
        self._operator_save_target = FIRM_SAVE_TARGET

    def click_operator_firm_save(self) -> None:
        """Click only the top-level firm Save after the exact guard is armed."""
        if (
            not self.operator_save_enabled
            or not self.prepare_only
            or not self.owned_add_required
            or not FIRM_PREVIEW_QUALIFIED
            or self._operator_save_used
            or self._operator_save_url is None
            or self._operator_save_target != FIRM_SAVE_TARGET
            or self.page.url != self._operator_save_url
        ):
            raise SroError("Operator firm Save is not authorized for this form")
        self._operator_save_used = True
        self._operator_save_request_pending = True
        try:
            self.page.locator(FIRM_SAVE_SELECTOR).click()
            self.settle(1.0)
            response = self._operator_save_request.response() if self._operator_save_request else None
            if response is None or not 200 <= response.status < 400:
                raise SroError("Результат запроса сохранения СРО неизвестен; повтор запрещён")
        finally:
            self._operator_save_request_pending = False

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
