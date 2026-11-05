"""Guarded preview of a new organization or sole-proprietor SRO card."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from ..core.entity_extract import EntityDebtorCard
from ..core.validators import digits, is_digit_presentation, valid_inn, valid_ogrn, valid_ogrnip
from ..platform import SroError
from ..platform import client as client_module
from ..platform.firm_address import (
    preview_address, save_address, verify_no_address_change, verify_saved_address,
)
from ..platform.firm_form import (
    assert_blank_firm_baseline, capture_add_firm, firm_record_url,
    read_firm_baseline, select_exact_option,
)
from ..platform.pages import FIRM_LIST_PATH, FIRM_SELECTORS
from .base import Preflight, ReviewGate, ScenarioResult
from .entity_review import EntityReviewScenario


_LEGAL_FORMS = {
    "общество с ограниченной ответственностью": "ООО",
    "акционерное общество": "АО",
    "публичное акционерное общество": "ПАО",
    "закрытое акционерное общество": "ЗАО",
    "открытое акционерное общество": "ОАО",
    "муниципальное унитарное предприятие": "МУП",
    "федеральное государственное унитарное предприятие": "ФГУП",
}
_KNOWN_OPF = {"ООО", "АО", "ПАО", "ЗАО", "ОАО", "МУП", "ФГУП", "АНО", "НП", "ИП"}


@dataclass
class FirmScenarioResult(ScenarioResult):
    # This non-PII catalogue identity must be stored with the screenshot
    # digest and preview token before a later, fresh form may be saved.
    destination_snapshot: dict[str, str] = field(default_factory=dict)
    owned_draft_url: str = ""


class FirmCardScenario(EntityReviewScenario):
    """The existing entity review fields mapped to the observed firm form."""

    supports_prepare = True
    list_path = FIRM_LIST_PATH
    selectors = FIRM_SELECTORS

    @staticmethod
    def _kind(card: EntityDebtorCard) -> str:
        return (card.entity_kind.value or "").strip()

    @classmethod
    def _opf(cls, card: EntityDebtorCard) -> str:
        if cls._kind(card) == "sole_proprietor":
            return "ИП"
        value = (card.legal_form.value or "").strip()
        if not value:
            value = (card.legal_name.value or "").strip().split("«", 1)[0].strip()
        label = _LEGAL_FORMS.get(value.casefold(), value.upper())
        return label if label in _KNOWN_OPF and label != "ИП" else ""

    @classmethod
    def _name(cls, card: EntityDebtorCard) -> str:
        return ((card.debtor_name if cls._kind(card) == "sole_proprietor" else card.legal_name).value or "").strip()

    @classmethod
    def _ogrn(cls, card: EntityDebtorCard) -> str:
        return ((card.ogrnip if cls._kind(card) == "sole_proprietor" else card.ogrn).value or "").strip()

    def validate(self, card: EntityDebtorCard) -> list[str]:
        missing: list[str] = []
        kind = self._kind(card)
        if kind not in {"legal_entity", "sole_proprietor"}:
            missing.append("Тип должника")
        if not self._name(card):
            missing.append("Наименование организации" if kind == "legal_entity" else "ФИО ИП")
        if not self._opf(card):
            missing.append("ОПФ из документа")
        inn = (card.inn.value or "").strip()
        if not valid_inn(inn) or not is_digit_presentation(inn) or len(digits(inn)) != (12 if kind == "sole_proprietor" else 10):
            missing.append("Проверенный ИНН")
        ogrn = self._ogrn(card)
        if ogrn and (not is_digit_presentation(ogrn) or not (valid_ogrnip(ogrn) if kind == "sole_proprietor" else valid_ogrn(ogrn))):
            missing.append("Проверенный ОГРН/ОГРНИП")
        kpp = (card.kpp.value or "").strip()
        if kpp and (kind != "legal_entity" or not is_digit_presentation(kpp) or len(digits(kpp)) != 9):
            missing.append("КПП юридического лица")
        if (card.address.value or "").strip() and (card.address_kind.value or "").strip() not in {
            "registration", "postal", "actual",
        }:
            missing.append("Тип адреса организации")
        return missing

    def validate_prepare(self, card: EntityDebtorCard) -> list[str]:
        kind = self._kind(card)
        missing = []
        if kind not in {"legal_entity", "sole_proprietor"}:
            missing.append("Тип должника")
        if not self._name(card):
            missing.append("Наименование организации" if kind == "legal_entity" else "ФИО ИП")
        if (card.address.value or "").strip() and (card.address_kind.value or "").strip() not in {
            "registration", "postal", "actual",
        }:
            missing.append("Тип адреса организации")
        return missing

    @staticmethod
    def save_blockers(card: EntityDebtorCard) -> list[str]:
        # Address writes have their own one-shot guarded postback and readback.
        # Other incomplete fields are handled by validate(card).
        return []

    def preflight(self, client, card: EntityDebtorCard, *,
                  allow_partial: bool = False,
                  owned_record_url: str | None = None,
                  expected_baseline: dict[str, str] | None = None) -> Preflight:
        missing = self.validate_prepare(card) if allow_partial else self.validate(card)
        if missing:
            return Preflight(missing=missing)
        if owned_record_url is not None and (
            not firm_record_url(owned_record_url, client.cfg.base_url)
            or not isinstance(expected_baseline, dict)
        ):
            raise SroError("Owned firm lookup prerequisites are invalid", code="firm_lookup_invalid")
        client.ensure_logged_in()
        registry = self.registry(client)
        registry.open()
        candidates = []
        inn = (card.inn.value or "").strip()
        ogrn = self._ogrn(card)
        queries = (
            (digits(inn) if valid_inn(inn) and is_digit_presentation(inn) else "", "firm_inn"),
            (digits(ogrn) if is_digit_presentation(ogrn) and (
                valid_ogrnip(ogrn) if self._kind(card) == "sole_proprietor" else valid_ogrn(ogrn)
            ) else "", "firm_ogrn"),
        )
        for query, key in queries:
            if not query:
                continue
            hits = registry.search(query)
            search_url = client.page.url
            for hit in hits:
                target = urljoin(search_url, hit.card_url)
                if not firm_record_url(target, client.cfg.base_url):
                    raise SroError("Unexpected firm lookup URL", code="firm_lookup_invalid")
                client.goto(target)
                if client.page.url != target or not client.is_logged_in():
                    raise SroError("Firm lookup left the requested card", code="firm_lookup_invalid")
                if target == owned_record_url:
                    baseline = read_firm_baseline(client)
                    if baseline != expected_baseline:
                        raise SroError("Owned firm draft changed", code="firm_preview_drift")
                    assert_blank_firm_baseline(baseline)
                    continue
                candidates.append(hit)
                if digits(client.el(key).input_value()) == query:
                    return Preflight(duplicates=[hit], duplicate_identity_confirmed=True)
            registry.open()
        if not candidates:
            hits = registry.search(self._name(card))
            search_url = client.page.url
            for hit in hits:
                target = urljoin(search_url, hit.card_url)
                if not firm_record_url(target, client.cfg.base_url):
                    raise SroError("Unexpected firm lookup URL", code="firm_lookup_invalid")
                if target == owned_record_url:
                    client.goto(target)
                    if client.page.url != target or not client.is_logged_in():
                        raise SroError("Firm lookup left the requested card", code="firm_lookup_invalid")
                    baseline = read_firm_baseline(client)
                    if baseline != expected_baseline:
                        raise SroError("Owned firm draft changed", code="firm_preview_drift")
                    assert_blank_firm_baseline(baseline)
                    continue
                candidates.append(hit)
        return Preflight(duplicates=candidates)

    @classmethod
    def _values(cls, card: EntityDebtorCard) -> dict[str, str]:
        name = cls._name(card)
        inn = (card.inn.value or "").strip()
        ogrn = cls._ogrn(card)
        kpp = (card.kpp.value or "").strip()
        return {
            "firm_short_name": ((card.short_name.value or "").strip() if cls._kind(card) == "legal_entity" else "") or name,
            "firm_full_name": name,
            "firm_inn": digits(inn) if valid_inn(inn) and is_digit_presentation(inn) else "",
            "firm_ogrn": digits(ogrn) if is_digit_presentation(ogrn) and (
                valid_ogrnip(ogrn) if cls._kind(card) == "sole_proprietor" else valid_ogrn(ogrn)
            ) else "",
            "firm_kpp": digits(kpp) if cls._kind(card) == "legal_entity" and is_digit_presentation(kpp) and len(digits(kpp)) == 9 else "",
        }

    @classmethod
    def _snapshot(cls, client) -> tuple[str, ...]:
        return tuple(client.el(key).input_value() for key in (
            "firm_short_name", "firm_full_name", "firm_inn", "firm_ogrn", "firm_kpp",
        )) + tuple(client.el(key).input_value() for key in (
            "firm_opf", "firm_status",
        )) + tuple(client.el(key).locator("option:checked").inner_text().strip() for key in (
            "firm_opf", "firm_status",
        ))

    @classmethod
    def _verify(cls, client, values: dict[str, str], opf: str, opf_value: str) -> None:
        for key, expected in values.items():
            if (client.el(key).input_value() or "").strip() != expected:
                raise SroError(f"SRO firm read-back mismatch for {key}", code="firm_readback_mismatch")
        for key, expected in (("firm_opf", opf), ("firm_status", "Должник")):
            actual = (client.el(key).locator("option:checked").inner_text() or "").strip()
            if actual.casefold() != expected.casefold():
                raise SroError(f"SRO firm read-back mismatch for {key}", code="firm_readback_mismatch")
        if client.el("firm_opf").input_value() != opf_value:
            raise SroError("SRO firm OPF identity changed", code="firm_readback_mismatch")

    @classmethod
    def _verify_saved(cls, client, values: dict[str, str], opf: str, opf_value: str, preview_url: str) -> None:
        current = client.page.url
        before = urlsplit(preview_url)
        base = urlsplit(client.cfg.base_url)
        if before.query:
            if (
                getattr(client, "_owned_firm_form_url", None) != preview_url
                or not firm_record_url(preview_url, client.cfg.base_url)
                or (current != preview_url and not cls._firm_list_url(current, base))
            ):
                raise SroError("SRO firm Save outcome unknown; do not retry")
            target = preview_url
        else:
            if not firm_record_url(current, client.cfg.base_url):
                raise SroError("SRO firm Save outcome unknown; do not retry")
            target = current
        parsed = urlsplit(target)
        client.goto(parsed.path + "?" + parsed.query)
        if client.page.url != target or not client.is_logged_in():
            raise SroError("SRO firm Save outcome unknown; do not retry")
        cls._verify(client, values, opf, opf_value)

    @staticmethod
    def _firm_list_url(url: str, base) -> bool:
        parsed = urlsplit(url)
        return (parsed.scheme, parsed.netloc) == (base.scheme, base.netloc) and parsed.path.casefold() == FIRM_LIST_PATH and not parsed.query and not parsed.fragment

    @classmethod
    def verify_owned_blank_after_close(cls, client, owned_record_url: str,
                                       expected_baseline: dict[str, str]) -> None:
        """Read back the exact Add-created GUID; Close must discard all fills."""
        if (
            not firm_record_url(owned_record_url, client.cfg.base_url)
            or not isinstance(expected_baseline, dict)
            or not cls._firm_list_url(client.page.url, urlsplit(client.cfg.base_url))
            or not client.is_logged_in()
        ):
            raise SroError("Firm Close could not be verified", code="firm_close_unverified")
        client.goto(owned_record_url)
        if client.page.url != owned_record_url or not client.is_logged_in():
            raise SroError("Firm Close could not be verified", code="firm_close_unverified")
        client.el("firm_short_name").wait_for(state="visible", timeout=client.cfg.timeout_ms)
        client.settle()
        if read_firm_baseline(client) != expected_baseline:
            raise SroError("Firm Close retained filled fields", code="firm_close_unverified")
        assert_blank_firm_baseline(expected_baseline)
        verify_no_address_change(client, 0)

    def run(self, client, card: EntityDebtorCard, gate: ReviewGate | None = None,
            *, on_add=None, owned_record_url: str | None = None,
            expected_baseline: dict[str, str] | None = None,
            expected_destination: dict[str, str] | None = None) -> FirmScenarioResult:
        preparing = owned_record_url is None
        saving = not preparing and gate is not None
        missing = self.validate(card) if saving else self.validate_prepare(card)
        if missing:
            raise SroError("Firm form lacks verified fields: " + ", ".join(missing), code="firm_payload_invalid")
        if not client.prepare_only:
            raise SroError("Firm form requires guarded prepare-only mode", code="firm_prepare_required")
        if not client_module.FIRM_PREVIEW_QUALIFIED:
            raise SroError("Firm Add persistence is not yet reconciled", code="firm_add_unqualified")
        if preparing:
            if not callable(on_add) or gate is not None or expected_baseline is not None:
                raise SroError("Firm Add requires a durable ownership callback", code="firm_add_unqualified")
        elif (on_add is not None
              or not firm_record_url(owned_record_url, client.cfg.base_url)
              or not isinstance(expected_baseline, dict)):
            raise SroError("Owned firm Save prerequisites are missing", code="firm_preview_drift")
        result = FirmScenarioResult()
        client.ensure_logged_in()
        client.register_selectors(self.selectors)
        if preparing:
            client.require_owned_firm_add_armed()
            registry = self.registry(client)
            registry.open()
            add_url = registry.click_add()
            client.el("firm_short_name").wait_for(state="visible", timeout=client.cfg.timeout_ms)
            client.settle()
            baseline = capture_add_firm(client, add_url)
            assert_blank_firm_baseline(baseline)
            # Add itself persists a blank GUID. Record its ownership before
            # filling, closing, screenshots, or any operation that may fail.
            on_add(add_url, baseline)
            result.owned_draft_url = add_url
        else:
            client.goto(owned_record_url)
            if client.page.url != owned_record_url or not client.is_logged_in():
                raise SroError("Owned firm URL could not be read back", code="firm_preview_drift")
            client.el("firm_short_name").wait_for(state="visible", timeout=client.cfg.timeout_ms)
            client.settle()
            baseline = read_firm_baseline(client)
            if baseline != expected_baseline:
                raise SroError("Owned firm draft changed after preview", code="firm_preview_drift")
            assert_blank_firm_baseline(baseline)
            if saving:
                client.bind_owned_firm_form(owned_record_url)
            else:
                client.bind_owned_firm_preview(owned_record_url)
            result.owned_draft_url = owned_record_url

        values = self._values(card)
        for key in ("firm_short_name", "firm_full_name", "firm_inn"):
            if values[key]:
                client.fill(key, values[key])
                result.filled[key] = values[key]
        opf = self._opf(card)
        if opf:
            select_exact_option(client, "firm_opf", opf)
        select_exact_option(client, "firm_status", "Должник")
        opf = (client.el("firm_opf").locator("option:checked").inner_text() or "").strip()
        opf_value = client.el("firm_opf").input_value()
        result.destination_snapshot = {"opf_label": opf, "opf_value": opf_value}
        if self._opf(card):
            result.filled["firm_opf"] = opf
        result.filled["firm_status"] = "Должник"

        client.el("firm_identifiers_tab").click()
        client.settle()
        for key in ("firm_ogrn", "firm_kpp"):
            if values[key]:
                client.fill(key, values[key])
                result.filled[key] = values[key]
        self._verify(client, values, opf, opf_value)
        result.screenshots.append(str(client.shot("firm_identifiers_filled")))
        client.el("firm_general_tab").click()
        client.settle()
        self._verify(client, values, opf, opf_value)
        result.screenshots.insert(0, str(client.shot("firm_general_filled")))
        if (card.address.value or "").strip() and not saving:
            address_filled, address_shot = preview_address(
                client, card.address.value, card.address_kind.value or "",
            )
            result.filled.update(address_filled)
            result.screenshots.append(address_shot)
            # Nested WebForms postbacks must not silently discard top fields.
            self._verify(client, values, opf, opf_value)
        preview_url = client.page.url
        snapshot = self._snapshot(client)

        decision = gate.ask(result.screenshots) if gate else "close"
        if decision != ("save" if saving else "close"):
            raise SroError("Unknown firm review decision", code="firm_decision_invalid")
        if decision == "save":
            if not getattr(client, "operator_save_enabled", False):
                raise SroError("Firm Save requires operator approval", code="firm_save_not_authorized")
            if not expected_destination or result.destination_snapshot != expected_destination:
                raise SroError("Firm destination changed; create a new preview", code="firm_preview_drift")
            if client.page.url != preview_url or not client.is_logged_in():
                raise SroError("SRO firm form changed before Save", code="firm_preview_drift")
            self._verify(client, values, opf, opf_value)
            if self._snapshot(client) != snapshot:
                raise SroError("SRO firm form changed before Save", code="firm_preview_drift")
            address_receipt = None
            if (card.address.value or "").strip():
                # The one-shot task save_started claim was committed before
                # this browser opened. A nested-write uncertainty holds the
                # whole Save; no address or top-level retry is attempted.
                address_receipt = save_address(
                    client, card.address.value, card.address_kind.value or "",
                )
                client.el("firm_general_tab").click()
                client.settle()
                self._verify(client, values, opf, opf_value)
            client.arm_operator_firm_save(preview_url)
            client.click_operator_firm_save()
            self._verify_saved(client, values, opf, opf_value, preview_url)
            if address_receipt is not None:
                verify_saved_address(client, address_receipt)
                result.filled["firm_address"] = card.address.value
            result.ok = result.saved = True
            result.record_url = client.page.url
            result.message = "Карточка организации создана"
            result.screenshots.append(str(client.shot("firm_saved")))
            return result

        client.click_close()
        if not self._firm_list_url(client.page.url, urlsplit(client.cfg.base_url)) or not client.is_logged_in():
            raise SroError("SRO did not confirm closing the unsaved firm form", code="firm_close_unverified")
        result.ok = True
        result.message = "Техническая карточка создана; форма закрыта без итогового сохранения"
        if (card.address.value or "").strip():
            result.message += "; адрес показан в предпросмотре и отменён до итогового Save"
        return result
