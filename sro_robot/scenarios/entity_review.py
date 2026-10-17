"""Internal-only review contract for organization and sole-proprietor documents."""

from __future__ import annotations

from ..core.entity_extract import EntityDebtorCard
from ..platform import SroError
from .base import FieldSpec, Preflight, Scenario, ScenarioResult


class EntityReviewScenario(Scenario):
    name = "entity_review"
    title = "Проверка документа организации или ИП"
    card_class = EntityDebtorCard
    fields = (
        FieldSpec("entity_kind", "Тип должника", required=True, choices=(
            ("legal_entity", "Юридическое лицо"),
            ("sole_proprietor", "Индивидуальный предприниматель"),
        )),
        FieldSpec("legal_name", "Наименование организации"),
        FieldSpec("short_name", "Сокращённое наименование (только из документа)"),
        FieldSpec("legal_form", "Организационно-правовая форма"),
        FieldSpec("debtor_name", "ФИО индивидуального предпринимателя"),
        FieldSpec("inn", "ИНН"),
        FieldSpec("ogrn", "ОГРН"),
        FieldSpec("ogrnip", "ОГРНИП"),
        FieldSpec("kpp", "КПП"),
        FieldSpec("address", "Адрес"),
        FieldSpec("address_kind", "Тип адреса", choices=(
            ("registration", "Регистрационный"),
            ("postal", "Почтовый"),
            ("actual", "Фактический"),
        )),
        FieldSpec("case_number", "Номер дела"),
        FieldSpec("court", "Арбитражный суд"),
        FieldSpec("doc_date", "Дата документа"),
        FieldSpec("doc_type", "Тип документа"),
    )

    def validate(self, card):
        # This scenario exists for the local review and internal-card workflow.
        # It is deliberately not a qualified external SRO payload.
        return ["Подготовка внешней формы СРО для этого типа должника не настроена"]

    def preflight(self, client, card):
        return Preflight(missing=self.validate(card))

    def run(self, client, card, gate=None):
        raise SroError("Внешняя операция для организаций и ИП не реализована")
