"""Сценарий: карточка должника-физлица в реестре физических лиц."""

from __future__ import annotations

from urllib.parse import parse_qs, urljoin, urlsplit

from playwright.sync_api import TimeoutError as PWTimeout

from ..core.address import split_address
from ..core.models import DebtorCard
from ..core.morph import guess_sex
from ..core.validators import digits, is_digit_presentation, valid_inn
from ..platform import SroClient, SroError
from ..platform import client as client_module
from ..platform.firm_address import (
    preview_person_address,
    save_person_address,
    verify_person_address_blank,
    verify_person_no_address_change,
    verify_saved_person_address,
)
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
    "postal": "Почтовый адрес",
    "registration": "Адрес регистрации",
    "actual": "Фактический адрес",
}

CARD_STATUS = "Должник"


class PersonCardScenario(Scenario):
    name = "person_card"
    title = "Карточка должника-физлица"
    supports_prepare = True
    list_path = PERSON_LIST_PATH
    selectors = PERSON_SELECTORS

    fields = (
        FieldSpec("case_number", "Номер дела", required=True),
        FieldSpec("doc_date", "Дата документа", required=True),
        FieldSpec("last_name", "Фамилия", required=True),
        FieldSpec("first_name", "Имя", required=True),
        FieldSpec("middle_name", "Отчество"),
        FieldSpec("inn", "ИНН"),
        FieldSpec("snils", "СНИЛС"),
        FieldSpec("address", "Адрес"),
        FieldSpec("address_kind", "Тип адреса", choices=(
            ("postal", "Почтовый адрес"),
            ("registration", "Адрес регистрации"),
            ("actual", "Фактический адрес"),
        )),
    )

    def validate_prepare(self, card: DebtorCard) -> list[str]:
        """A coherent name may be previewed before case/date are reviewed."""
        missing = []
        if not (card.last_name.value or "").strip():
            missing.append("Фамилия")
        if not (card.first_name.value or "").strip():
            missing.append("Имя")
        if (card.address.value or "").strip() and card.address_kind.value not in ADDRESS_KIND_OPTIONS:
            missing.append("Тип адреса")
        if (card.address.value or "").strip() and not getattr(
            client_module, "PERSON_ADDRESS_PREVIEW_QUALIFIED", False,
        ):
            missing.append("Адрес физического лица ожидает проверку формы СРО")
        return missing

    @staticmethod
    def save_blockers(card: DebtorCard) -> list[str]:
        """A supplied INN must be representable in the owned person form."""
        blockers = []
        inn = (card.inn.value or "").strip()
        if inn and (
            not is_digit_presentation(inn)
            or len(digits(inn)) != 12
            or not valid_inn(inn)
        ):
            blockers.append("ИНН физического лица должен быть корректным 12-значным номером")
        if (card.address.value or "").strip():
            if card.address_kind.value not in ADDRESS_KIND_OPTIONS:
                blockers.append("Тип адреса")
            if not getattr(client_module, "PERSON_ADDRESS_PREVIEW_QUALIFIED", False):
                blockers.append("Адрес физического лица ожидает проверку формы СРО")
        return blockers

    def preflight(self, client: SroClient, card: DebtorCard,
                  *, allow_partial: bool = False) -> Preflight:
        missing = self.validate_prepare(card) if allow_partial else self.validate(card)
        if missing:
            return Preflight(missing=missing)

        client.ensure_logged_in()
        registry = self.registry(client)
        registry.open()
        inn = digits(card.inn.value or "")
        if len(inn) != 12 or not valid_inn(inn):
            return Preflight(duplicates=registry.search(card.fio))
        candidates = registry.search(inn)
        search_page_url = client.page.url
        if not candidates:
            return Preflight(duplicates=registry.search(card.fio))
        base = urlsplit(client.cfg.base_url)
        for candidate in candidates:
            target = urljoin(search_page_url, candidate.card_url)
            if not self._is_verified_person_record_url(target, base):
                raise SroError("Unexpected person lookup URL", code="person_lookup_invalid")
            client.goto(target)
            if client.page.url != target or not client.is_logged_in():
                raise SroError("Person lookup left the requested card", code="person_lookup_invalid")
            client.el("inn").wait_for(state="visible", timeout=client.cfg.timeout_ms)
            if digits(client.el("inn").input_value()) == inn:
                return Preflight(duplicates=[candidate], duplicate_identity_confirmed=True)
        return Preflight(duplicates=candidates)

    def run(self, client: SroClient, card: DebtorCard,
            gate: ReviewGate | None = None, *,
            on_add=None,
            owned_record_url: str | None = None,
            expected_baseline: dict[str, str] | None = None) -> ScenarioResult:
        result = ScenarioResult()
        owned_flow = on_add is not None or owned_record_url is not None
        has_address = bool((card.address.value or "").strip())
        if on_add is not None and gate is not None:
            raise SroError("Owned person Add cannot carry a Save decision", code="person_decision_invalid")
        if (owned_flow and getattr(client, "operator_save_enabled", False)
                and (owned_record_url is None or gate is None)):
            raise SroError("Owned person Save requires a bound review decision", code="person_decision_invalid")
        if (owned_flow and getattr(client, "operator_save_enabled", False)
                and (self.validate(card) or self.save_blockers(card))):
            raise SroError("Owned person Save fields are incomplete", code="person_payload_invalid")
        if owned_flow and self.validate_prepare(card):
            raise SroError("Owned person preview fields are incomplete", code="person_payload_invalid")

        client.ensure_logged_in()
        if owned_record_url is not None:
            if (not client.prepare_only or on_add is not None
                    or not isinstance(expected_baseline, dict)):
                raise SroError("Owned person draft binding is invalid", code="person_owned_drift")
            self.verify_owned_blank_after_close(
                client, owned_record_url, expected_baseline, check_address=False,
            )
            if getattr(client, "operator_save_enabled", False):
                client.bind_owned_person_form(owned_record_url)
            else:
                client.bind_owned_person_preview(owned_record_url)
            if getattr(client_module, "PERSON_ADDRESS_PREVIEW_QUALIFIED", False):
                verify_person_address_blank(client)
        else:
            if on_add is not None:
                client.require_owned_person_add_armed()
            registry = self.registry(client)
            registry.open()
            add_return_url = registry.click_add()
            client.el("last_name").wait_for(state="visible", timeout=client.cfg.timeout_ms)
            client.settle()
            if client.prepare_only:
                baseline = self._require_blank_new_card(client, add_return_url)
                if on_add is not None:
                    base = urlsplit(client.cfg.base_url)
                    if not self._is_verified_person_record_url(add_return_url, base):
                        raise SroError("SRO Add did not return an identified draft", code="new_person_identity_unbound")
                    on_add(add_return_url, baseline)

        form_inn = (
            card.inn.value if is_digit_presentation(card.inn.value or "")
            and valid_inn(card.inn.value or "")
            and len(digits(card.inn.value or "")) == 12 else ""
        ) if owned_flow else card.inn_or_case

        general = [
            ("last_name", "Фамилия", card.last_name.value),
            ("first_name", "Имя", card.first_name.value),
            ("middle_name", "Отчество", card.middle_name.value),
            ("inn", "ИНН", form_inn),
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
        self._verify_general_fields(client, card, sex, expected_inn=form_inn)
        preview_url = client.page.url
        preview_values = self._general_field_snapshot(client)
        result.screenshots.append(str(client.shot("general_filled")))

        if has_address and owned_flow and not getattr(client, "operator_save_enabled", False):
            address_filled, address_shot = preview_person_address(
                client, card.address.value, card.address_kind.value or "",
            )
            result.filled.update(address_filled)
            result.screenshots.append(address_shot)
            self._verify_general_fields(client, card, sex, expected_inn=form_inn)
        elif has_address and not client.prepare_only:
            result.filled.update(self._add_address(client, card))
            result.screenshots.append(str(client.shot("address_filled")))

        decision = "close" if client.dry_run or client.prepare_only else "save"
        if gate is not None:
            decision = gate.ask(result.screenshots)

        operator_save = client.prepare_only and getattr(client, "operator_save_enabled", False)
        if client.prepare_only and decision != "close" and not (operator_save and decision == "save"):
            raise SroError("Prepare-only mode permits closing without saving only")

        if decision != "save":
            # Close discards the filled form fields. An Add-created technical
            # blank row may remain and is tracked by the owned GUID ledger.
            client.click_close()
            if client.prepare_only:
                closed_url = urlsplit(client.page.url)
                trusted = urlsplit(client.cfg.base_url)
                if (
                    client.page.url == preview_url
                    or (closed_url.scheme, closed_url.netloc) != (trusted.scheme, trusted.netloc)
                    or closed_url.path.casefold() == "/register/person_card.aspx"
                    or not client.is_logged_in()
                ):
                    raise SroError(
                        "SRO did not confirm closing the unsaved person form",
                        code="person_close_unverified",
                    )
            result.ok = True
            result.message = "Форма заполнена и закрыта; сохранение полей не выполнялось"
            return result

        if operator_save:
            # The employee approved the screenshot of this exact filled form.
            # Re-read after waiting: stale navigation, session expiry or field
            # drift must consume no browser Save attempt.
            if client.page.url != preview_url or not client.is_logged_in():
                raise SroError("Сессия или форма СРО изменилась; сохранение не выполнено")
            self._verify_general_fields(client, card, sex, expected_inn=form_inn)
            if self._general_field_snapshot(client) != preview_values:
                raise SroError("Форма СРО изменилась после просмотра; сохранение не выполнено")
            address_receipt = None
            if has_address:
                # The task's one-shot save_started claim precedes this child
                # write. Any uncertain child or main Save must remain held.
                verify_person_address_blank(client)
                address_receipt = save_person_address(
                    client, card.address.value, card.address_kind.value or "",
                )
                self._verify_general_fields(client, card, sex, expected_inn=form_inn)
                if self._general_field_snapshot(client) != preview_values:
                    raise SroError("Поля карточки СРО изменились после сохранения адреса")
            client.arm_operator_person_save(preview_url)
            client.click_operator_person_save()
            self._verify_saved_person(client, card, sex, preview_url, expected_inn=form_inn)
            if address_receipt is not None:
                verify_saved_person_address(client, address_receipt)
                result.filled["Адрес"] = card.address.value
        else:
            client.click_save()
        client.settle(1.0)
        result.ok = True
        result.saved = True
        result.record_url = client.page.url
        result.message = "Карточка создана"
        result.screenshots.append(str(client.shot("saved")))
        return result

    @staticmethod
    def _verify_general_fields(client: SroClient, card: DebtorCard, sex: str | None,
                               *, expected_inn: str | None = None) -> None:
        """Read back the real form values before presenting the preview."""
        expected = {
            "last_name": card.last_name.value,
            "first_name": card.first_name.value,
            "middle_name": card.middle_name.value,
            "inn": card.inn_or_case if expected_inn is None else expected_inn,
            "comment": card.case_number.value,
        }
        for key, value in expected.items():
            actual = client.el(key).input_value()
            if actual.strip() != (value or "").strip():
                raise SroError(
                    f"SRO form read-back mismatch for {key}",
                    code="person_readback_mismatch",
                )

        for key, value in (("sex_select", sex), ("status_select", CARD_STATUS)):
            if not value:
                continue
            selected = client.el(key).locator("option:checked").inner_text().strip()
            if selected.casefold() != value.casefold():
                raise SroError(
                    f"SRO form read-back mismatch for {key}",
                    code="person_readback_mismatch",
                )

    @staticmethod
    def _general_field_snapshot(client: SroClient) -> tuple[str, ...]:
        return tuple(
            client.el(key).input_value()
            for key in ("last_name", "first_name", "middle_name", "inn", "comment")
        ) + tuple(
            client.el(key).locator("option:checked").inner_text()
            for key in ("sex_select", "status_select")
        )

    @classmethod
    def _verify_saved_person(cls, client: SroClient, card: DebtorCard,
                             sex: str | None, preview_url: str,
                             *, expected_inn: str | None = None) -> None:
        """Reload the exact identified card and verify persisted form values."""
        current_url = client.page.url
        before = urlsplit(preview_url)
        base = urlsplit(client.cfg.base_url)
        add_form_url = (
            getattr(client, "_new_person_form_url", None)
            or getattr(client, "_owned_person_form_url", None)
        )
        identified_preview = bool(before.query)

        if identified_preview:
            target_url = preview_url
            if (add_form_url != preview_url
                    or not cls._is_verified_person_record_url(preview_url, base)
                    or (before.scheme, before.netloc) != (base.scheme, base.netloc)):
                raise SroError("Результат сохранения СРО неизвестен; требуется проверка без повтора")
            if current_url != preview_url and not cls._is_person_list_url(current_url, base):
                raise SroError("Результат сохранения СРО неизвестен; требуется проверка без повтора")
        else:
            if (before.path.casefold() != "/register/person_card.aspx"
                    or (before.scheme, before.netloc) != (base.scheme, base.netloc)
                    or before.query or before.fragment
                    or not cls._is_verified_person_record_url(current_url, base)):
                raise SroError("Результат сохранения СРО неизвестен; требуется проверка без повтора")
            target_url = current_url

        target = urlsplit(target_url)
        target_path = target.path + (f"?{target.query}" if target.query else "")
        client.goto(target_path)
        if (client.page.url != target_url
                or not cls._is_verified_person_record_url(client.page.url, base)
                or not client.is_logged_in()):
            raise SroError("Результат сохранения СРО неизвестен; требуется проверка без повтора")
        cls._verify_general_fields(client, card, sex, expected_inn=expected_inn)

    @staticmethod
    def _is_verified_person_record_url(url: str, base) -> bool:
        try:
            target = urlsplit(url)
            identity = parse_qs(target.query, keep_blank_values=True)
        except (TypeError, ValueError):
            return False
        return bool(
            (target.scheme, target.netloc) == (base.scheme, base.netloc)
            and target.path.casefold() == "/register/person_card.aspx"
            and set(identity) == {"id"}
            and len(identity["id"]) == 1
            and identity["id"][0].strip()
            and target.query.startswith("id=")
            and "&" not in target.query
            and ";" not in target.query
            and not target.fragment
        )

    @staticmethod
    def _is_person_list_url(url: str, base) -> bool:
        try:
            target = urlsplit(url)
        except (TypeError, ValueError):
            return False
        return bool(
            (target.scheme, target.netloc) == (base.scheme, base.netloc)
            and target.path.casefold() == "/register/person_list.aspx"
            and not target.query
            and not target.fragment
        )

    @classmethod
    def _require_blank_new_card(cls, client: SroClient,
                                add_return_url: str | None = None) -> dict[str, str]:
        current_url = client.page.url
        current = urlsplit(client.page.url)
        base = urlsplit(client.cfg.base_url)
        if (current.path.casefold() != "/register/person_card.aspx"
                or (current.scheme, current.netloc) != (base.scheme, base.netloc)
                or current.fragment):
            raise SroError(
                "SRO did not open the new person card",
                code="new_person_url_invalid",
            )
        if current.query:
            add_bound_identity = (
                add_return_url == current_url
                and cls._is_verified_person_record_url(current_url, base)
            )
            if not add_bound_identity:
                raise SroError(
                    "SRO opened an identified person card without matching Add provenance; no fields were changed",
                    code="new_person_identity_unbound",
                )
        baseline = cls._blank_baseline(client)
        client._new_person_form_url = current_url
        return baseline

    @staticmethod
    def _blank_baseline(client: SroClient) -> dict[str, str]:
        baseline = {
            key: client.el(key).input_value() or ""
            for key in ("last_name", "first_name", "middle_name", "inn", "comment")
        }
        for key, prefix in (("sex_select", "sex"), ("status_select", "status")):
            selected = client.el(key).locator("option:checked")
            baseline[f"{prefix}_label"] = selected.inner_text().strip()
            baseline[f"{prefix}_value"] = selected.get_attribute("value") or ""
        allowed_surnames = {"", "фамилия не указана", "* фамилия не указана"}
        if (baseline["last_name"].strip().casefold() not in allowed_surnames
                or any(baseline[key].strip() for key in ("first_name", "middle_name", "inn", "comment"))):
            raise SroError(
                "SRO new person form was not blank; no fields were changed",
                code="new_person_not_blank",
            )
        return baseline

    @classmethod
    def verify_owned_blank_after_close(
        cls, client: SroClient, owned_record_url: str,
        expected_baseline: dict[str, str],
        *, check_address: bool = True,
    ) -> None:
        """Read back the exact owned GUID; filled preview fields must not persist."""
        base = urlsplit(client.cfg.base_url)
        if (not isinstance(expected_baseline, dict)
                or not cls._is_verified_person_record_url(owned_record_url, base)):
            raise SroError("Owned person draft binding is invalid", code="person_owned_drift")
        target = urlsplit(owned_record_url)
        client.goto(target.path + "?" + target.query)
        if client.page.url != owned_record_url or not client.is_logged_in():
            raise SroError("Owned person draft changed after Close", code="person_owned_drift")
        client.el("last_name").wait_for(state="visible", timeout=client.cfg.timeout_ms)
        client.settle()
        try:
            current_baseline = cls._blank_baseline(client)
        except SroError as exc:
            raise SroError("Owned person draft changed after Close", code="person_owned_drift") from exc
        if current_baseline != expected_baseline:
            raise SroError("Owned person draft changed after Close", code="person_owned_drift")
        if check_address and getattr(client_module, "PERSON_ADDRESS_PREVIEW_QUALIFIED", False):
            if getattr(client, "operator_save_enabled", False):
                verify_person_address_blank(client)
            else:
                verify_person_no_address_change(client, 0)

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
