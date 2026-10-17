"""Conservative local extraction for explicit organization and entrepreneur debtors.

The parser only reads fields from a short, explicitly labelled debtor section.
Every result remains a review suggestion; no entity result is auto-approved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import validators
from .extract import (
    ROLE_BOUNDARY,
    _clean,
    _vote,
    _metadata_warnings,
    find_case_number,
    find_court,
    find_doc_date,
    find_doc_type,
)
from .models import Field

ENTITY_ROLE_SOURCE = "debtor_entity_role:v1"
IP_PREFIX = re.compile(
    r"^\s*(?:(?:индивидуальн\w*\s+предпринимател\w*)|ИП)\b\s*[:№-]?\s*",
    re.I,
)
LEGAL_PREFIX = re.compile(
    r"^\s*(?:(?:общество\s+с\s+ограниченной\s+ответственностью)|"
    r"(?:публичн\w*\s+)?акционерн\w*\s+обществ\w*|"
    r"(?:непубличн\w*\s+)?акционерн\w*\s+обществ\w*|"
    r"ООО|ПАО|АО|ЗАО|ОАО|ГУП|МУП)\b",
    re.I,
)
PERSON_PREFIX = re.compile(r"^\s*(?:гражданин\w*|гражданк\w*|физическ\w*\s+лиц\w*)\b", re.I)
FIO = re.compile(r"[А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+)?")
STOP = re.compile(r"\b(?:ИНН|ОГРН(?:ИП)?|КПП|адрес(?: регистрации)?|место нахождения)\b", re.I)
ADDRESS = re.compile(r"\b(?:адрес(?: регистрации)?|место нахождения)\s*[:№-]?\s*", re.I)
SHORT_NAME = re.compile(r"\b(?:краткое|сокращ[её]нное)\s+наименован\w*\s*[:№-]?\s*", re.I)
ADDRESS_NEXT_FIELD = re.compile(
    r"\b(?:ИНН|ОГРН(?:ИП)?|КПП|ОКПО|ОКВЭД|корреспондентский\s+сч[её]т|"
    r"расч[её]тный\s+сч[её]т|краткое\s+наименование|сокращ[её]нное\s+наименование)\b\s*[:№-]?",
    re.I,
)
LEGAL_FORM = re.compile(
    r"^(общество\s+с\s+ограниченной\s+ответственностью|"
    r"публичн\w*\s+акционерн\w*\s+обществ\w*|"
    r"непубличн\w*\s+акционерн\w*\s+обществ\w*|ООО|ПАО|АО|ЗАО|ОАО|ГУП|МУП)\b",
    re.I,
)


@dataclass
class EntityDebtorCard:
    entity_kind: Field = field(default_factory=Field)
    legal_name: Field = field(default_factory=Field)
    short_name: Field = field(default_factory=Field)
    legal_form: Field = field(default_factory=Field)
    debtor_name: Field = field(default_factory=Field)
    inn: Field = field(default_factory=Field)
    ogrn: Field = field(default_factory=Field)
    ogrnip: Field = field(default_factory=Field)
    kpp: Field = field(default_factory=Field)
    address: Field = field(default_factory=Field)
    case_number: Field = field(default_factory=Field)
    court: Field = field(default_factory=Field)
    doc_date: Field = field(default_factory=Field)
    doc_type: Field = field(default_factory=Field)
    raw_text: str = ""


def _debtor_sections(text: str) -> list[str]:
    sections: list[str] = []
    for marker in re.finditer(r"\bдолжник(?:а|у|ом|е|и|ов|ами|ах)?\b\s*[:—-]?", text, re.I):
        prefix = text[max(0, marker.start() - 100):marker.start()]
        if re.search(
            r"(?:представител\w*|управляющ\w*|кредитор\w*|заявител\w*)"
            r"(?:\s+[а-яё]+){0,2}[\s-]+$",
            prefix,
            re.I,
        ):
            continue
        start = marker.end()
        next_role = ROLE_BOUNDARY.search(text, start)
        end = min(len(text), start + 800, next_role.start() if next_role else len(text))
        section = text[start:end].strip(" :—-\t\r\n")
        if section:
            sections.append(section)
    return sections


def detect_entity_kind(text: str) -> str | None:
    """Return a kind only when one explicit legal/IP prefix is in debtor context."""
    kinds: set[str] = set()
    for section in _debtor_sections(_clean(text)):
        if IP_PREFIX.match(section):
            kinds.add("sole_proprietor")
        elif LEGAL_PREFIX.match(section):
            kinds.add("legal_entity")
        elif PERSON_PREFIX.match(section):
            kinds.add("person")
    return next(iter(kinds)) if len(kinds) == 1 else None


def has_explicit_entity_marker(text: str) -> bool:
    return any(
        IP_PREFIX.match(section) or LEGAL_PREFIX.match(section)
        for section in _debtor_sections(_clean(text))
    )


def _field(value: str | None, warning: str | None = None, source: str = ENTITY_ROLE_SOURCE) -> Field:
    return Field(value, 0.65 if value else 0.0, source if value else "not_found", warning)


def _single_token(section: str, label: str, length: int) -> tuple[str | None, str | None]:
    values = {
        validators.digits(match.group(1))
        for match in re.finditer(rf"\b{label}\s*[:№-]?\s*([\d\s]{{{length},{length + 5}}})", section, re.I)
    }
    values.discard("")
    if not values:
        return None, None
    exact = {value for value in values if len(value) == length}
    if len(exact) == 1 and len(values) == 1:
        return next(iter(exact)), None
    return None, "Реквизит неоднозначен или имеет неверную длину"


def parse_entity(variants: list[str]) -> EntityDebtorCard:
    variants = [_clean(value) for value in variants if value and value.strip()]
    if not variants:
        raise ValueError("Нет текста для разбора")
    card = EntityDebtorCard(raw_text=max(variants, key=len))
    card.case_number = _vote(variants, find_case_number)
    card.court = _vote(variants, find_court)
    card.doc_date = _vote(variants, find_doc_date)
    card.doc_type = _vote(variants, find_doc_type)
    _metadata_warnings(card, variants)
    per_variant: list[tuple[str | None, str | None, str | None, str | None, dict]] = []
    for text in variants:
        sections = _debtor_sections(text)
        candidates: list[tuple[str, str | None, str | None, str | None]] = []
        for section in sections:
            if IP_PREFIX.match(section):
                kind = "sole_proprietor"
                rest = IP_PREFIX.sub("", section, count=1)
                name_match = FIO.search(rest)
                name = name_match.group(0) if name_match else None
            elif LEGAL_PREFIX.match(section):
                kind = "legal_entity"
                name_stops = [match.start() for pattern in (STOP, SHORT_NAME)
                              if (match := pattern.search(section))]
                name = section[:min(name_stops)] if name_stops else section.splitlines()[0]
                name = name.strip(" ,;:.\t\r\n")[:512] or None
            else:
                continue
            inn, inn_warning = _single_token(section, "ИНН", 10 if kind == "legal_entity" else 12)
            if inn and not validators.valid_inn(inn):
                inn = None
                inn_warning = "ИНН не прошёл проверку контрольной суммы"
            form_match = LEGAL_FORM.match(section) if kind == "legal_entity" else None
            legal_form = form_match.group(0).strip() if form_match else None
            ogrn, _ = _single_token(section, "ОГРН", 13) if kind == "legal_entity" else (None, None)
            ogrnip, _ = _single_token(section, "ОГРНИП", 15) if kind == "sole_proprietor" else (None, None)
            kpp, _ = _single_token(section, "КПП", 9) if kind == "legal_entity" else (None, None)
            address_match = ADDRESS.search(section)
            address = None
            if address_match:
                address_tail = section[address_match.end():]
                following_field = ADDRESS_NEXT_FIELD.search(address_tail)
                if following_field:
                    address_tail = address_tail[:following_field.start()]
                address_text = re.sub(r"\s+", " ", address_tail).strip(" ,;:.\t")[:512]
                address = address_text or None
            short_name_match = SHORT_NAME.search(section)
            short_name = None
            if short_name_match:
                short_name_tail = section[short_name_match.end():]
                following_field = ADDRESS_NEXT_FIELD.search(short_name_tail)
                if following_field:
                    short_name_tail = short_name_tail[:following_field.start()]
                short_name = re.sub(r"\s+", " ", short_name_tail).strip(" ,;:.\t")[:512] or None
            candidates.append((kind, name, inn, legal_form, {
                "ogrn": ogrn, "ogrnip": ogrnip, "kpp": kpp, "address": address,
                "short_name": short_name,
                "inn_warning": inn_warning or ("ИНН не указан" if not inn else None),
            }))
        if len(candidates) == 1:
            per_variant.append(candidates[0])
        elif candidates:
            per_variant.append((None, None, None, None, {}))
        else:
            per_variant.append((None, None, None, None, {}))

    identities = {(kind, name.casefold() if name else None) for kind, name, _inn, _form, _extra in per_variant if kind and name}
    if len(identities) == 1 and all(kind for kind, *_ in per_variant):
        kind, _normalized_name = next(iter(identities))
        chosen = next((item for item in per_variant if item[0] == kind and item[1]), None)
        if chosen:
            card.entity_kind = _field(kind)
            target = "legal_name" if kind == "legal_entity" else "debtor_name"
            setattr(card, target, _field(chosen[1]))
            if kind == "legal_entity":
                card.legal_form = _field(chosen[3])
            inn_values = {item[2] for item in per_variant if item[2]}
            card.inn = _field(
                next(iter(inn_values)) if len(inn_values) == 1 else None,
                "Варианты OCR расходятся по ИНН" if len(inn_values) > 1
                else chosen[4].get("inn_warning"),
            )
            def agreed_extra(key: str) -> Field:
                found = {item[4].get(key) for item in per_variant if item[0] == kind and item[4].get(key)}
                if len(found) > 1:
                    return _field(None, "Варианты OCR расходятся по реквизиту")
                return _field(next(iter(found)) if found else None)

            card.ogrn = agreed_extra("ogrn")
            card.ogrnip = agreed_extra("ogrnip")
            card.kpp = agreed_extra("kpp")
            card.address = agreed_extra("address")
            card.short_name = agreed_extra("short_name")
            extra = chosen[4]
            if kind == "legal_entity" and not card.ogrn.value:
                card.ogrn.warning = "ОГРН не извлечён; поле можно проверить вручную"
            if kind == "sole_proprietor" and not card.ogrnip.value:
                card.ogrnip.warning = "ОГРНИП не извлечён; поле можно проверить вручную"
    else:
        warning = "Тип или наименование должника не установлены однозначно; проверьте документ"
        card.entity_kind = _field(None, warning, "role_ambiguous")
        for name in ("legal_name", "short_name", "debtor_name", "inn", "ogrn", "ogrnip", "kpp", "address"):
            setattr(card, name, _field(None, warning, "role_ambiguous"))
    return card
