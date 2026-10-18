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
STOP = re.compile(
    r"\b(?:ИНН\s*[:№-]?\s*\d|ОГРН(?:ИП)?\s*[:№-]?\s*\d|КПП\s*[:№-]?\s*\d|"
    r"адрес(?:\s+(?:регистрации|почтовый|фактический))?\s*[:№-]|место нахождения\s*[:№-])",
    re.I,
)
REPRESENTATION = re.compile(r"\bв\s+лице\b", re.I)
ADDRESS = re.compile(
    r"\b(?:(?:почтовый|фактический)\s+адрес|адрес(?:\s+(?:регистрации|почтовый|фактический))?|место нахождения)\s*[:№-]\s*",
    re.I,
)
TYPED_ADDRESS_LABELS = {
    "registration": re.compile(r"\bадрес(?:\s+регистрации)\s*[:№-]", re.I),
    "postal": re.compile(r"\bпочтовый\s+адрес\s*[:№-]", re.I),
    "actual": re.compile(r"\bфактический\s+адрес\s*[:№-]", re.I),
}
SHORT_NAME = re.compile(r"\b(?:краткое|сокращ[её]нное)\s+наименован\w*\s*[:№-]?\s*", re.I)
ADDRESS_NEXT_FIELD = re.compile(
    r"\b(?:ИНН|ОГРН(?:ИП)?|КПП|ОКПО|ОКВЭД|корреспондентский\s+сч[её]т|"
    r"расч[её]тный\s+сч[её]т|краткое\s+наименование|сокращ[её]нное\s+наименование|"
    r"(?:почтовый|фактический)\s+адрес|адрес(?:\s+регистрации)?)\b\s*[:№-]?",
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
    address_kind: Field = field(default_factory=Field)
    case_number: Field = field(default_factory=Field)
    court: Field = field(default_factory=Field)
    doc_date: Field = field(default_factory=Field)
    doc_type: Field = field(default_factory=Field)
    raw_text: str = ""


def _debtor_sections_with_offsets(text: str) -> list[tuple[str, int]]:
    sections: list[tuple[str, int]] = []
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
        raw_section = text[start:end]
        leading = len(raw_section) - len(raw_section.lstrip(" :—-\t\r\n"))
        section = raw_section.strip(" :—-\t\r\n")
        if section:
            sections.append((section, start + leading))
    return sections


def _debtor_sections(text: str) -> list[str]:
    return [section for section, _offset in _debtor_sections_with_offsets(text)]


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


def _field(
    value: str | None,
    warning: str | None = None,
    source: str = ENTITY_ROLE_SOURCE,
    location: tuple[int, int, int, str] | None = None,
) -> Field:
    variant, start, end, excerpt = location or (None, None, None, None)
    return Field(value, 0.65 if value else 0.0, source if value else "not_found", warning,
                 variant, start, end, excerpt)


def _literal_span(section: str, section_start: int, value: str | None, variant: int):
    if not value:
        return None
    # OCR whitespace is normalized by _clean; retain exact offsets in that text.
    start = section.find(value)
    if start < 0:
        return None
    absolute_start = section_start + start
    absolute_end = absolute_start + len(value)
    return (variant, absolute_start, absolute_end, section[max(0, start - 50):min(len(section), start + len(value) + 50)])


def _identifier_span(section: str, section_start: int, label: str, value: str | None,
                     length: int, variant: int):
    if not value:
        return None
    for match in re.finditer(
        rf"\b{label}\s*[:№-]?\s*([\d\s]{{{length},{length + 5}}})", section, re.I,
    ):
        if validators.digits(match.group(1)) == value:
            start, end = match.span(1)
            excerpt = section[max(0, start - 50):min(len(section), end + 50)]
            return (variant, section_start + start, section_start + end, excerpt)
    return None


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
    for variant_index, text in enumerate(variants):
        sections = _debtor_sections_with_offsets(text)
        candidates: list[tuple[str, str | None, str | None, str | None]] = []
        for section, section_start in sections:
            if IP_PREFIX.match(section):
                kind = "sole_proprietor"
                rest = IP_PREFIX.sub("", section, count=1)
                name_match = FIO.search(rest)
                name = name_match.group(0) if name_match else None
            elif LEGAL_PREFIX.match(section):
                kind = "legal_entity"
                name_stops = [match.start() for pattern in (STOP, SHORT_NAME, REPRESENTATION)
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
            ogrn, ogrn_warning = _single_token(section, r"ОГРН(?!ИП)", 13) if kind == "legal_entity" else (None, None)
            if ogrn and not validators.valid_ogrn(ogrn):
                ogrn, ogrn_warning = None, "ОГРН не прошёл проверку структуры или контрольной цифры"
            ogrnip, ogrnip_warning = _single_token(section, "ОГРНИП", 15) if kind == "sole_proprietor" else (None, None)
            if ogrnip and not validators.valid_ogrnip(ogrnip):
                ogrnip, ogrnip_warning = None, "ОГРНИП не прошёл проверку контрольной цифры"
            kpp, kpp_warning = _single_token(section, "КПП", 9) if kind == "legal_entity" else (None, None)
            address_matches = list(ADDRESS.finditer(section))
            address_match = address_matches[0] if address_matches else None
            address = None
            address_warning = None
            address_kind_warning = None
            address_location = None
            address_kind = None
            if len(address_matches) > 1:
                address_warning = "В разделе должника несколько адресов; проверьте адреса отдельно"
            if address_match:
                address_tail = section[address_match.end():]
                following_field = ADDRESS_NEXT_FIELD.search(address_tail)
                if following_field:
                    address_tail = address_tail[:following_field.start()]
                raw_address = address_tail.strip(" ,;:.\t\r\n")
                address_text = re.sub(r"\s+", " ", raw_address)[:512]
                address = address_text or None
                if address:
                    address_start = section_start + address_match.end() + len(address_tail) - len(address_tail.lstrip(" ,;:.\t\r\n"))
                    address_end = address_start + len(raw_address)
                    address_location = (
                        variant_index, address_start, address_end,
                        section[max(0, address_start - section_start - 50):min(len(section), address_end - section_start + 50)],
                    )
                typed = {kind for kind, pattern in TYPED_ADDRESS_LABELS.items() if pattern.search(section)}
                if len(typed) == 1:
                    address_kind = next(iter(typed))
                elif len(typed) > 1:
                    address_kind_warning = "В документе указано несколько типов адреса; проверьте адреса отдельно"
                elif address and not address_warning:
                    address_kind_warning = "Тип адреса явно не указан в документе; выберите после проверки"
                if len(address_matches) > 1:
                    address = None
                    address_location = None
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
                "spans": {
                    "entity_kind": (
                        (variant_index, section_start, section_start + (IP_PREFIX.match(section) or LEGAL_PREFIX.match(section)).end(),
                         section[:(IP_PREFIX.match(section) or LEGAL_PREFIX.match(section)).end()])
                        if IP_PREFIX.match(section) or LEGAL_PREFIX.match(section) else None
                    ),
                    "legal_name" if kind == "legal_entity" else "debtor_name": _literal_span(section, section_start, name, variant_index),
                    "legal_form": _literal_span(section, section_start, legal_form, variant_index),
                    "inn": _identifier_span(section, section_start, "ИНН", inn,
                                             10 if kind == "legal_entity" else 12, variant_index),
                    "ogrn": _identifier_span(section, section_start, r"ОГРН(?!ИП)", ogrn, 13, variant_index),
                    "ogrnip": _identifier_span(section, section_start, "ОГРНИП", ogrnip, 15, variant_index),
                    "kpp": _identifier_span(section, section_start, "КПП", kpp, 9, variant_index),
                    "address": address_location,
                    "address_kind": (
                        (variant_index, section_start + match.start(), section_start + match.end(), match.group(0))
                        if address_kind and (match := TYPED_ADDRESS_LABELS[address_kind].search(section))
                        else None
                    ),
                    "short_name": _literal_span(section, section_start, short_name, variant_index),
                },
                "ogrn_warning": ogrn_warning,
                "ogrnip_warning": ogrnip_warning,
                "kpp_warning": kpp_warning,
                "address_kind": address_kind,
                "address_kind_warning": address_kind_warning,
                "address_warning": address_warning,
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
            card.entity_kind = _field(kind, location=chosen[4]["spans"].get("entity_kind"))
            target = "legal_name" if kind == "legal_entity" else "debtor_name"
            setattr(card, target, _field(chosen[1], location=chosen[4]["spans"].get(target)))
            if kind == "legal_entity":
                card.legal_form = _field(chosen[3], location=chosen[4]["spans"].get("legal_form"))
            inn_values = {item[2] for item in per_variant if item[2]}
            agreed_inn = next(iter(inn_values)) if len(inn_values) == 1 else None
            inn_location = next((item[4]["spans"].get("inn") for item in per_variant
                                 if item[2] == agreed_inn and item[4]["spans"].get("inn")), None)
            card.inn = _field(
                agreed_inn,
                "Варианты OCR расходятся по ИНН" if len(inn_values) > 1
                else chosen[4].get("inn_warning"),
                location=inn_location,
            )
            def agreed_extra(key: str) -> Field:
                found = {item[4].get(key) for item in per_variant if item[0] == kind and item[4].get(key)}
                if len(found) > 1:
                    return _field(None, "Варианты OCR расходятся по реквизиту")
                warning_key = f"{key}_warning"
                warnings = {
                    item[4].get(warning_key)
                    for item in per_variant if item[0] == kind and item[4].get(warning_key)
                }
                if found:
                    agreed = next(iter(found))
                    location = next((item[4]["spans"].get(key) for item in per_variant
                                     if item[0] == kind and item[4].get(key) == agreed
                                     and item[4]["spans"].get(key)), None)
                    return _field(agreed, location=location)
                if len(warnings) > 1:
                    return _field(None, "Варианты OCR расходятся по реквизиту")
                return _field(None, next(iter(warnings)) if warnings else None)

            card.ogrn = agreed_extra("ogrn")
            card.ogrnip = agreed_extra("ogrnip")
            card.kpp = agreed_extra("kpp")
            card.address = agreed_extra("address")
            card.address_kind = agreed_extra("address_kind")
            card.short_name = agreed_extra("short_name")
            extra = chosen[4]
            if kind == "legal_entity" and not card.ogrn.value and not card.ogrn.warning:
                card.ogrn.warning = "ОГРН не извлечён; поле можно проверить вручную"
            if kind == "sole_proprietor" and not card.ogrnip.value and not card.ogrnip.warning:
                card.ogrnip.warning = "ОГРНИП не извлечён; поле можно проверить вручную"
    else:
        warning = "Тип или наименование должника не установлены однозначно; проверьте документ"
        card.entity_kind = _field(None, warning, "role_ambiguous")
        for name in ("legal_name", "short_name", "debtor_name", "inn", "ogrn", "ogrnip", "kpp", "address"):
            setattr(card, name, _field(None, warning, "role_ambiguous"))
    return card
