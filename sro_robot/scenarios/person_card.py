"""Сценарий: карточка должника-физлица в реестре физических лиц."""

from __future__ import annotations

from playwright.sync_api import TimeoutError as PWTimeout

from ..core.address import split_address
from ..core.models import DebtorCard
from ..core.morph import guess_sex
from ..platform import SroClient, SroError
from ..platform.pages import PERSON_LIST_PATH, PERSON_SELECTORS
from .base import FieldSpec, Preflight, ReviewGate, Scenario, ScenarioResult

ADDRESS_FIELD_KEYS = {
    "index": "addr_index",
    "area": "addr_area",
    "district": "addr_district",
    "city": "addr_city",
    "street": "addr_street",
    "build": "addr_build",
    "room": "addr_room",
}

ADDRESS_KIND_OPTIONS = {
    "registration": "Адрес регистрации",
    "actual": "Фактический адрес",
}

CARD_STATUS = "Должник"


class PersonCardScenario(Scenario):
    name = "person_card"
    title = "Карточка должника-физлица"
    list_path = PERSON_LIST_PATH
    selectors = PERSON_SELECTORS

    fields = (
        FieldSpec("case_number", "Номер дела", required=True),
        FieldSpec("doc_date", "Дата документа", required=True),
        FieldSpec("last_name", "Фамилия", required=True),
        FieldSpec("first_name", "Имя", required=True),
        FieldSpec("middle_name", "Отчество"),
        FieldSpec("inn", "ИНН"),
        FieldSpec("address", "Адрес"),
        FieldSpec("address_kind", "Тип адреса", choices=(
            ("registration", "Адрес регистрации"),
            ("actual", "Фактический адрес"),
        )),
    )

    def preflight(self, client: SroClient, card: DebtorCard) -> Preflight:
        missing = self.validate(card)
        if missing:
            return Preflight(missing=missing)

        client.ensure_logged_in()
        registry = self.registry(client)
        registry.open()
        # Дубликаты ищутся по ИНН, затем по ФИО: тёзки встречаются, ИНН — нет.
        return Preflight(duplicates=registry.find_duplicates(card.inn.value or "", card.fio))

    def run(self, client: SroClient, card: DebtorCard,
            gate: ReviewGate | None = None) -> ScenarioResult:
        result = ScenarioResult()

        client.ensure_logged_in()
        registry = self.registry(client)
        registry.open()
        registry.click_add()

        client.el("last_name").wait_for(state="visible", timeout=client.cfg.timeout_ms)
        client.settle()

        general = [
            ("last_name", "Фамилия", card.last_name.value),
            ("first_name", "Имя", card.first_name.value),
            ("middle_name", "Отчество", card.middle_name.value),
            ("inn", "ИНН", card.inn_or_case),
            ("comment", "Комментарий", card.case_number.value),
        ]
        for key, title, value in general:
            if value and client.fill(key, value):
                result.filled[title] = value

        sex = guess_sex(card.middle_name.value or "", card.first_name.value or "")
        if sex:
            try:
                client.select_by_text("sex_select", sex)
                result.filled["Пол"] = sex
            except SroError:
                pass

        client.select_by_text("status_select", CARD_STATUS)
        result.filled["Статус"] = CARD_STATUS
        result.screenshots.append(str(client.shot("general_filled")))

        if card.address.value:
            result.filled.update(self._add_address(client, card))
            result.screenshots.append(str(client.shot("address_filled")))

        decision = "close" if client.dry_run else "save"
        if gate is not None:
            decision = gate.ask(result.screenshots)

        if decision != "save":
            # Закрываем черновик, чтобы в реестре не оставались карточки
            # «* Фамилия не указана» от пробных прогонов.
            client.click_close()
            result.ok = True
            result.message = "Форма заполнена и закрыта без сохранения"
            return result

        client.click_save()
        client.settle(1.0)
        result.ok = True
        result.saved = True
        result.record_url = client.page.url
        result.message = "Карточка создана"
        result.screenshots.append(str(client.shot("saved")))
        return result

    def _add_address(self, client: SroClient, card: DebtorCard) -> dict:
        filled: dict = {}

        client.click_text("Контакты")
        client.click_text("Добавить адрес")
        client.el("address_kind_select").wait_for(state="visible", timeout=client.cfg.timeout_ms)
        client.settle(0.5)

        kind = ADDRESS_KIND_OPTIONS.get(card.address_kind.value or "actual", "Фактический адрес")
        client.select_by_text("address_kind_select", kind)
        filled["Тип адреса"] = kind

        parts = split_address(card.address.value)
        placed = False
        for part_key, selector_key in ADDRESS_FIELD_KEYS.items():
            value = parts.get(part_key)
            if not value:
                continue
            try:
                if client.fill(selector_key, value):
                    filled[part_key] = value
                    placed = True
            except (SroError, PWTimeout):
                continue

        if not placed:
            # Как при ручном вводе: весь адрес одной строкой в поле «Улица».
            client.fill("addr_street", card.address.value)
            filled["Улица"] = card.address.value

        client.click_save(anchor_key="address_kind_select")
        client.settle(0.6)
        return filled
