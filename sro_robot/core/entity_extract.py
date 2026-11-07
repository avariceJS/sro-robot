"""Bounded organization debtor and explicit civil-participant extraction.

Debtor fields require explicit debtor-role evidence. Civil parties retain their
actual role as separate source-located review suggestions, never debtor authority.
No entity result is auto-approved.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from . import validators
from .extract import (
    ROLE_BOUNDARY,
    _attach_court_source,
    _attach_field_source,
    _clean,
    _locate_case_number,
    _locate_doc_date,
    _locate_doc_type,
    _vote,
    _metadata_warnings,
    _source_page_at,
    extract_applicant_evidence,
    find_case_number,
    find_court,
    find_doc_date,
    find_doc_type,
)
from .models import Field

ENTITY_ROLE_SOURCE = "debtor_entity_role:v1"
ENTITY_CANDIDATE_SOURCE = "debtor_entity_candidate:v1"
IP_PREFIX = re.compile(
    r"^\s*(?:(?:индивидуальн\w*\s+предпринимател\w*)|ИП)\b\s*[:№-]?\s*",
    re.I,
)
LEGAL_PREFIX = re.compile(
    r"^\s*(?:(?:обществ(?:о|а|у|ом|е)\s+с\s+ограниченной\s+ответственностью)|"
    r"(?:федеральн\w*\s+)?государственн\w*\s+унитарн\w*\s+предприят\w*|"
    r"муниципальн\w*\s+унитарн\w*\s+предприят\w*|"
    r"(?:(?:публичн|непубличн|открыт|закрыт)\w*\s+)?акционерн\w*\s+обществ\w*|"
    r"ООО|ПАО|АО|ЗАО|ОАО|ГУП|МУП)\b",
    re.I,
)
PERSON_PREFIX = re.compile(r"^\s*(?:гражданин\w*|гражданк\w*|физическ\w*\s+лиц\w*)\b", re.I)
FIO = re.compile(r"[А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+)?")
STOP = re.compile(
    r"\b(?:ИНН|ОГРН(?:ИП)?|КПП)\s*[:№-]?|"
    r"адрес(?:\s+(?:регистрации|почтовый|фактический))?\s*[:№-]|место нахождения\s*[:№-]",
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
    r"^(обществ(?:о|а|у|ом|е)\s+с\s+ограниченной\s+ответственностью|"
    r"(?:федеральн\w*\s+)?государственн\w*\s+унитарн\w*\s+предприят\w*|"
    r"муниципальн\w*\s+унитарн\w*\s+предприят\w*|"
    r"(?:(?:публичн|непубличн|открыт|закрыт)\w*\s+)?акционерн\w*\s+обществ\w*|ООО|ПАО|АО|ЗАО|ОАО|ГУП|МУП)\b",
    re.I,
)
REVERSE_DEBTOR_ROLE = re.compile(
    r"(?:\(\s*(?:далее\s*[-–—]\s*)?должник\s*[,)]|"
    r"далее\s*[-–—]\s*должник\b(?:\s*[,)]|$))",
    re.I,
)
LEGAL_ANYWHERE = re.compile(LEGAL_PREFIX.pattern.replace(r"^\s*", "", 1), re.I)

CIVIL_ROLE = re.compile(
    r"\b(?P<role>истец|истца|истцу|истцом|ответчик(?:а|у|ом|е|и|ов)?|"
    r"заявител(?:ь|я|ю|ем|е|и|ей))\b\s*[:—–-]?\s*", re.I,
)
CLAIM_BY = re.compile(r"\bпо\s+(?:иску|исковому\s+заявлению)\s+", re.I)
PARTICIPANT_END = re.compile(
    r"\b(?:(?:о|об)\s+[а-яё]+|"
    r"при\s+участии|установил|решил|определил)\b", re.I,
)


def _outside_quotes(text: str, position: int) -> bool:
    prefix = text[:position]
    return prefix.count("«") == prefix.count("»") and prefix.count('"') % 2 == 0


def _first_outside(pattern, text: str, start: int = 0):
    return next((match for match in pattern.finditer(text, start)
                 if _outside_quotes(text, match.start())), None)


def _sentence_end(text: str):
    """End an ownership clause without splitting quoted names or metadata."""
    for match in re.finditer(r"[.!?](?=\s|$)", text):
        prefix = text[:match.start()]
        if not _outside_quotes(text, match.start()) or prefix.count("(") != prefix.count(")"):
            continue
        # These are ordinary typed-address/name abbreviations, not claim
        # subjects. Other ambiguous punctuation fails closed at this boundary.
        if match.group(0) == "." and re.search(
            r"(?:\b[А-ЯЁA-Z]|\b(?:г|д|ул|кв|им|обл|пер|корп|стр))$", prefix,
        ):
            continue
        return match
    return None


def _quoted_name_end(section: str) -> int | None:
    opening = next((i for i, char in enumerate(section) if char in '«"'), None)
    if opening is None:
        return None
    if section[opening] == '"':
        closing = section.find('"', opening + 1)
        return closing + 1 if closing >= 0 else None
    depth = 0
    for index in range(opening, len(section)):
        if section[index] == "«":
            depth += 1
        elif section[index] == "»":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _role_name(value: str) -> str:
    word = value.casefold()
    return "plaintiff" if word.startswith("ист") else "defendant" if word.startswith("ответчик") else "applicant"


def _role_tail_has_entity(text: str, start: int) -> bool:
    tail = text[start:start + 800]
    if LEGAL_PREFIX.match(tail):
        return True
    stops = [m.start() for pattern in (ROLE_BOUNDARY, PARTICIPANT_END) if (m := _first_outside(pattern, tail))]
    recipient = next((m for m in re.finditer(r"\bк\s+", tail, re.I) if _outside_quotes(tail, m.start())), None)
    if recipient:
        stops.append(recipient.start())
    if stops:
        tail = tail[:min(stops)]
    sentence = _sentence_end(tail)
    if sentence:
        tail = tail[:sentence.start()]
    # Labelled valid company OGRN + explicit civil role supports legal type
    # without maintaining an exhaustive legal-form name list.
    ogrn, warning = _single_token(tail, r"ОГРН(?!ИП)", 13)
    name = re.split(r"\(|\bОГРН\b", tail, maxsplit=1, flags=re.I)[0].strip(" ,;:.\t\r\n")
    return bool(ogrn and not warning and validators.valid_ogrn(ogrn) and name
                and len(name) <= 512 and re.match(r"[А-ЯЁа-яё]", name) and not re.search(r"\b(?:представител\w*|управляющ\w*)\b", name, re.I))


def _participant_sections(text: str):
    """Explicit civil roles remain participants, never inferred debtor roles."""
    found = []
    for marker in CIVIL_ROLE.finditer(text):
        if not _outside_quotes(text, marker.start()):
            continue
        before = text[max(0, marker.start() - 100):marker.start()]
        if re.search(r"(?:представител\w*|управляющ\w*)\s+$", before, re.I):
            continue
        immediate_legal_form = bool(LEGAL_PREFIX.match(text[marker.end():]))
        explicit_label = ":" in marker.group(0)
        if (immediate_legal_form or explicit_label) and _role_tail_has_entity(text, marker.end()):
            found.append((marker.end(), marker.start(), marker.end(), _role_name(marker.group('role')), None))
        # Reverse roles must be explicit parenthetical labels immediately after
        # a bounded organization occurrence, not a later narrative mention.
        reverse = re.search(r"\(\s*(?:далее\s*[-–—]\s*)?$", before, re.I)
        if reverse:
            prefix_start = max(0, marker.start() - 650)
            prefix = text[prefix_start:marker.start()]
            legal = [m for m in LEGAL_ANYWHERE.finditer(prefix) if _outside_quotes(prefix, m.start())]
            if legal:
                start = prefix_start + legal[-1].start()
                between = text[start:marker.start()]
                opening = marker.start() - len(before) + reverse.start()
                name_chunk = text[start:opening].rstrip()
                last_quote = max(name_chunk.rfind("»"), name_chunk.rfind('"'))
                trailing_prose = last_quote >= 0 and bool(name_chunk[last_quote + 1:].strip(" ,;"))
                if (not trailing_prose and not re.search(r"\b(?:кредитор\w*|представител\w*|управляющ\w*)\b", between, re.I)):
                    found.append((start, marker.start(), marker.end(), _role_name(marker.group('role')), marker.start() - len(before) + reverse.start()))
    for marker in CLAIM_BY.finditer(text):
        if not _outside_quotes(text, marker.start()):
            continue
        if _role_tail_has_entity(text, marker.end()):
            found.append((marker.end(), marker.start(), marker.end(), "plaintiff", None))
        # `к` identifies a defendant only inside this bounded explicit claim.
        tail = text[marker.end():marker.end() + 1000]
        sentence = _sentence_end(tail)
        if sentence:
            tail = tail[:sentence.start()]
        procedure = _first_outside(PARTICIPANT_END, tail)
        if procedure:
            tail = tail[:procedure.start()]
        for recipient in re.finditer(r"\bк\s+", tail, re.I):
            start = marker.end() + recipient.end()
            if _role_tail_has_entity(text, start) and _outside_quotes(tail, recipient.start()):
                found.append((start, marker.end() + recipient.start(), start, "defendant", None))
    result, seen = [], set()
    for start, role_start, role_end, role, reverse_end in sorted(found):
        # A repeated narrative role may bind the same organization occurrence;
        # preserve one bundle and never combine fields across occurrences.
        if (start, role) in seen:
            continue
        seen.add((start, role))
        limit = min(len(text), start + 800)
        boundary = _first_outside(ROLE_BOUNDARY, text, start)
        if boundary:
            limit = min(limit, boundary.start())
        procedure = _first_outside(PARTICIPANT_END, text, start)
        if procedure:
            limit = min(limit, procedure.start())
        if reverse_end is not None:
            # Keep the identifying parenthesis and requisites after its role,
            # but stop before the next independent legal organization below.
            limit = min(len(text), start + 800)
            boundary = _first_outside(ROLE_BOUNDARY, text, role_end)
            if boundary:
                limit = min(limit, boundary.start())
            if procedure:
                limit = min(limit, procedure.start())
        section = text[start:limit]
        sentence = _sentence_end(section)
        if sentence:
            limit = start + sentence.start()
            section = text[start:limit]
        if role == "plaintiff":
            # A claim's `к` switches ownership even when the defendant has no
            # recognized legal prefix (public agency, person, abbreviation).
            recipient = next((m for m in re.finditer(r"\bк\s+", section, re.I)
                              if _outside_quotes(section, m.start())), None)
            if recipient:
                limit = start + recipient.start()
                section = text[start:limit]
        legal = list(LEGAL_ANYWHERE.finditer(section))
        for another in legal[1:]:
            if _outside_quotes(section, another.start()):
                limit = start + another.start()
                section = text[start:limit]
                break
        # The reverse role marker is itself a name stop. It is retained in
        # section so subsequent IDs in the same parenthesis stay available.
        name_end = reverse_end - start if reverse_end is not None else _quoted_name_end(section)
        if section.strip():
            result.append((section.rstrip(" ,;:.\t\r\n"), start, limit == start + 800,
                           role, role_start, role_end, name_end))
    return result


def has_entity_participant(text: str) -> bool:
    return bool(_participant_sections(_clean(text)))


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
    review_suggestions: list[dict] = field(default_factory=list)
    review_suggestions_complete: bool = True
    applicant_evidence: dict | None = None
    raw_text: str = ""


def _debtor_sections_with_offsets_and_truncation(text: str) -> list[tuple[str, int, bool]]:
    sections: list[tuple[str, int, bool]] = []
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
        section_limit = start + 800
        end = min(len(text), section_limit, next_role.start() if next_role else len(text))
        truncated = end == section_limit and end < len(text) and (
            next_role is None or next_role.start() > end
        )
        raw_section = text[start:end]
        leading = len(raw_section) - len(raw_section.lstrip(" :—-\t\r\n"))
        section = raw_section.strip(" :—-\t\r\n")
        if section:
            sections.append((section, start + leading, truncated))
    # Some court acts identify an organization before a parenthetical debtor
    # role: "ООО «... » (далее — должник)". The exact role is explicit evidence;
    # later OCR disagreement can still limit the result to a review candidate.
    for marker in REVERSE_DEBTOR_ROLE.finditer(text):
        window_start = max(0, marker.start() - 650)
        prefix = text[window_start:marker.start()]
        # A legal form inside a quoted name belongs to that name, not to a
        # second debtor. Keep the outer legal form in nested corporate titles.
        legal = [match for match in LEGAL_ANYWHERE.finditer(prefix)
                 if prefix[:match.start()].count("«") == prefix[:match.start()].count("»")
                 and prefix[:match.start()].count('"') % 2 == 0]
        if not legal:
            continue
        nearest = legal[-1]
        if marker.start() - (window_start + nearest.start()) > 600:
            continue
        between = text[window_start + nearest.end():marker.start()]
        if re.search(r"\b(?:представител\w*|кредитор\w*|заявител\w*)\b", between, re.I):
            continue
        start = window_start + nearest.start()
        if any(offset <= start < offset + len(section) for section, offset, _ in sections):
            continue
        next_role = ROLE_BOUNDARY.search(text, marker.end())
        end = min(len(text), start + 800, next_role.start() if next_role else len(text))
        raw_section = text[start:end]
        section = raw_section.strip(" :—-\t\r\n")
        if section:
            sections.append((section, start, end == start + 800 and end < len(text)))
    return sections


def _debtor_sections_with_offsets(text: str) -> list[tuple[str, int]]:
    return [
        (section, offset)
        for section, offset, _truncated in _debtor_sections_with_offsets_and_truncation(text)
    ]


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
    location: tuple[int, int, int, str, int | None] | None = None,
) -> Field:
    variant, start, end, excerpt, page = location or (None, None, None, None, None)
    return Field(value, 0.65 if value else 0.0, source if value else "not_found", warning,
                 variant, start, end, excerpt, page)


def _literal_span(
    section: str, section_start: int, value: str | None, variant: int, source_text: str,
):
    if not value:
        return None
    # OCR whitespace is normalized by _clean; retain exact offsets in that text.
    start = section.find(value)
    end = start + len(value)
    if start < 0:
        # A legal name may wrap across OCR lines. Its displayed value uses
        # spaces, while the source span still covers the original line break.
        pattern = re.compile(r"\s+".join(re.escape(part) for part in value.split()))
        match = pattern.search(section)
        if match is None:
            return None
        start, end = match.span()
    absolute_start = section_start + start
    absolute_end = section_start + end
    return (
        variant, absolute_start, absolute_end,
        section[max(0, start - 50):min(len(section), end + 50)],
        _source_page_at(source_text, absolute_start),
    )


def _identifier_span(section: str, section_start: int, label: str, value: str | None,
                     length: int, variant: int, source_text: str):
    if not value:
        return None
    for match in re.finditer(
        rf"\b{label}\s*[:№-]?\s*([\d\s]{{{length},{length + 5}}})", section, re.I,
    ):
        if validators.digits(match.group(1)) == value:
            start, end = match.span(1)
            excerpt = section[max(0, start - 50):min(len(section), end + 50)]
            absolute_start = section_start + start
            return (
                variant, absolute_start, section_start + end, excerpt,
                _source_page_at(source_text, absolute_start),
            )
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


def _section_candidate(section: str, section_start: int, section_truncated: bool,
                       variant_index: int, text: str, name_end: int | None = None,
                       generic_legal: bool = False):
    if IP_PREFIX.match(section):
        kind = "sole_proprietor"
        rest = IP_PREFIX.sub("", section, count=1)
        name_match = FIO.search(rest)
        name = name_match.group(0) if name_match else None
    elif LEGAL_PREFIX.match(section) or generic_legal:
        kind = "legal_entity"
        name_stops = [match.start() for pattern in (STOP, SHORT_NAME, REPRESENTATION, REVERSE_DEBTOR_ROLE)
                      if (match := pattern.search(section))]
        if name_end is not None:
            name_stops.append(name_end)
        if generic_legal and "(" in section:
            name_stops.append(section.index("("))
        identity_name = section[:min(name_stops)] if name_stops else section.splitlines()[0]
        identity_name = re.sub(r"\s+", " ", identity_name.strip(" ,;:.(\t\r\n"))
        name_warning = (
            "Полное наименование превышает лимит 512 символов и обрезано; проверьте по оригиналу"
            if len(identity_name) > 512 else None
        )
        name = identity_name[:512] or None
    else:
        return None
    if kind != "legal_entity":
        identity_name = name or ""
        name_warning = None
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
        address_text = re.sub(r"\s+", " ", raw_address)
        address = address_text or None
        if address:
            address_start = section_start + address_match.end() + len(address_tail) - len(address_tail.lstrip(" ,;:.\t\r\n"))
            address_end = address_start + len(raw_address)
            address_location = (
                variant_index, address_start, address_end,
                section[max(0, address_start - section_start - 50):min(len(section), address_end - section_start + 50)],
                _source_page_at(text, address_start),
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
        short_name = re.sub(r"\s+", " ", short_name_tail).strip(" ,;:.\t") or None
    return (kind, name, inn, legal_form, {
        "ogrn": ogrn, "ogrnip": ogrnip, "kpp": kpp, "address": address,
        "short_name": short_name,
        "identity_name": identity_name,
        "name_warning": name_warning,
        "section_truncated": section_truncated,
        "spans": {
            "entity_kind": (
                (variant_index, section_start,
                 section_start + (IP_PREFIX.match(section) or LEGAL_PREFIX.match(section)).end(),
                 section[:(IP_PREFIX.match(section) or LEGAL_PREFIX.match(section)).end()],
                 _source_page_at(text, section_start))
                if IP_PREFIX.match(section) or LEGAL_PREFIX.match(section) else _identifier_span(
                    section, section_start, r"ОГРН(?!ИП)", ogrn, 13, variant_index, text,
                ) if generic_legal else None
            ),
            "legal_name" if kind == "legal_entity" else "debtor_name": _literal_span(
                section, section_start, name, variant_index, text,
            ),
            "legal_form": _literal_span(section, section_start, legal_form, variant_index, text),
            "inn": _identifier_span(section, section_start, "ИНН", inn,
                                    10 if kind == "legal_entity" else 12, variant_index, text),
            "ogrn": _identifier_span(
                section, section_start, r"ОГРН(?!ИП)", ogrn, 13, variant_index, text,
            ),
            "ogrnip": _identifier_span(
                section, section_start, "ОГРНИП", ogrnip, 15, variant_index, text,
            ),
            "kpp": _identifier_span(
                section, section_start, "КПП", kpp, 9, variant_index, text,
            ),
            "address": address_location,
            "address_kind": (
                (variant_index, section_start + match.start(), section_start + match.end(), match.group(0),
                 _source_page_at(text, section_start + match.start()))
                if address_kind and (match := TYPED_ADDRESS_LABELS[address_kind].search(section))
                else None
            ),
            "short_name": _literal_span(
                section, section_start, short_name, variant_index, text,
            ),
        },
        "ogrn_warning": ogrn_warning,
        "ogrnip_warning": ogrnip_warning,
        "kpp_warning": kpp_warning,
        "address_kind": address_kind,
        "address_kind_warning": address_kind_warning,
        "address_warning": address_warning,
        "inn_warning": inn_warning,
        "inn_present": bool(re.search(r"\bИНН\b", section, re.I)),
    })


def parse_entity(variants: list[str]) -> EntityDebtorCard:
    variants = [_clean(value) for value in variants if value and value.strip()]
    if not variants:
        raise ValueError("Нет текста для разбора")
    card = EntityDebtorCard(raw_text=max(variants, key=len))
    card.case_number = _vote(variants, find_case_number)
    card.court = _vote(variants, find_court)
    _attach_court_source(card.court, variants)
    card.doc_date = _vote(variants, find_doc_date)
    card.doc_type = _vote(variants, find_doc_type)
    _attach_field_source(card.case_number, variants, _locate_case_number)
    _attach_field_source(card.doc_date, variants, _locate_doc_date)
    _attach_field_source(card.doc_type, variants, _locate_doc_type)
    _metadata_warnings(card, variants)
    card.applicant_evidence = extract_applicant_evidence(variants, card.case_number)
    per_variant: list[tuple[str | None, str | None, str | None, str | None, dict]] = []
    multi_candidate_variant = False
    for variant_index, text in enumerate(variants):
        sections = _debtor_sections_with_offsets_and_truncation(text)
        candidates: list[tuple[str, str | None, str | None, str | None]] = []
        for section, section_start, section_truncated in sections:
            candidate = _section_candidate(section, section_start, section_truncated, variant_index, text)
            if candidate:
                candidates.append(candidate)
        if len(candidates) == 1:
            per_variant.append(candidates[0])
        elif candidates:
            multi_candidate_variant = True
            per_variant.append((None, None, None, None, {}))
        else:
            per_variant.append((None, None, None, None, {}))

    # Preserve bounded, occurrence-level alternatives for employee review.
    # Exact agreement is still required for the authoritative entity fields
    # below; OCR variants with different legal names must never be merged.
    card.review_suggestions_complete = len(variants) <= 5 and all(
        kind and name for kind, name, *_rest in per_variant
    )
    for kind, name, inn, _form, extra in per_variant[:5]:
        if not kind or not name:
            continue
        target = "legal_name" if kind == "legal_entity" else "debtor_name"
        candidate_fields: dict[str, dict] = {}
        for attr, value in (("entity_kind", kind), (target, name), ("inn", inn)):
            location = extra.get("spans", {}).get(attr)
            if value and location:
                variant, start, end, excerpt, page = location
                candidate_fields[attr] = {
                    "value": value,
                    "confidence": 0.35,
                    "source_variant": variant,
                    "source_start": start,
                    "source_end": end,
                    "source_excerpt": excerpt[:180],
                    "source_page": page,
                }
        card.review_suggestions.append({
            "kind": "entity_candidate",
            "warning": "OCR-варианты наименования или роли расходятся; проверьте оригинал",
            "copyable_fields": [target, "inn"],
            "fields": candidate_fields,
        })
    identities = {
        (kind, str(extra.get("identity_name") or name).casefold())
        for kind, name, _inn, _form, extra in per_variant if kind and name
    }
    if len(identities) == 1 and all(kind for kind, *_ in per_variant):
        kind, _normalized_name = next(iter(identities))
        chosen = next((item for item in per_variant if item[0] == kind and item[1]), None)
        if chosen:
            card.entity_kind = _field(kind, location=chosen[4]["spans"].get("entity_kind"))
            target = "legal_name" if kind == "legal_entity" else "debtor_name"
            name_warnings = {
                item[4].get("name_warning")
                for item in per_variant if item[0] == kind and item[4].get("name_warning")
            }
            name_warning = next(iter(name_warnings)) if len(name_warnings) == 1 else (
                "Варианты OCR содержат слишком длинные или разные наименования; проверьте оригинал"
                if name_warnings else None
            )
            setattr(card, target, _field(
                chosen[1], name_warning, location=chosen[4]["spans"].get(target),
            ))
            if kind == "legal_entity":
                card.legal_form = _field(chosen[3], location=chosen[4]["spans"].get("legal_form"))
            inn_values = {item[2] for item in per_variant if item[2]}
            agreed_inn = next(iter(inn_values)) if len(inn_values) == 1 else None
            inn_location = next((item[4]["spans"].get("inn") for item in per_variant
                                 if item[2] == agreed_inn and item[4]["spans"].get("inn")), None)
            inn_warnings = {
                item[4].get("inn_warning") for item in per_variant
                if item[0] == kind and item[4].get("inn_warning")
            }
            if len(inn_values) > 1:
                inn_warning = "Варианты OCR расходятся по ИНН"
            elif inn_warnings:
                inn_warning = (
                    next(iter(inn_warnings)) if len(inn_warnings) == 1
                    else "Варианты OCR содержат неоднозначные данные ИНН"
                )
            elif agreed_inn:
                inn_warning = None
            elif any(item[4].get("inn_present") for item in per_variant if item[0] == kind):
                inn_warning = "ИНН указан, но его значение не распознано"
            else:
                inn_warning = "ИНН не указан"
            card.inn = _field(
                agreed_inn,
                inn_warning,
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
                    warning = next(iter(warnings)) if warnings else None
                    return _field(agreed, warning, location=location)
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
            if any(item[4].get("section_truncated") for item in per_variant if item[0] == kind):
                boundary_warning = (
                    "Раздел должника длиннее 800 символов; проверьте остальные реквизиты по оригиналу"
                )
                for name in (
                    "entity_kind", "legal_name" if kind == "legal_entity" else "debtor_name",
                    "inn", "ogrn", "ogrnip", "kpp", "address", "address_kind", "short_name",
                ):
                    field = getattr(card, name)
                    field.warning = (
                        f"{field.warning}; {boundary_warning}" if field.warning
                        else boundary_warning
                    )
            if kind == "legal_entity" and not card.ogrn.value and not card.ogrn.warning:
                card.ogrn.warning = "ОГРН не извлечён; поле можно проверить вручную"
            if kind == "sole_proprietor" and not card.ogrnip.value and not card.ogrnip.warning:
                card.ogrnip.warning = "ОГРНИП не извлечён; поле можно проверить вручную"
    else:
        warning = "Тип или наименование должника не установлены однозначно; проверьте документ"
        card.entity_kind = _field(None, warning, "role_ambiguous")
        for name in ("legal_name", "short_name", "debtor_name", "inn", "ogrn", "ogrnip", "kpp", "address"):
            setattr(card, name, _field(None, warning, "role_ambiguous"))
        # A repeated checksum-valid identifier can support one review-only
        # occurrence despite noisy legal-name readings. Never attach an ID
        # from a different occurrence to the chosen name.
        candidates = [item for item in per_variant if item[0] and item[1]]
        kinds = {item[0] for item in candidates}
        all_ids = {item[2] for item in candidates if item[2]}
        agreed_inn = next(iter(all_ids)) if len(all_ids) == 1 else None
        cohort = [
            item for item in candidates
            if item[2] == agreed_inn
            and all(item[4]["spans"].get(key) for key in (
                "entity_kind", "legal_name" if item[0] == "legal_entity" else "debtor_name", "inn",
            ))
        ] if agreed_inn else []
        if (
            len(cohort) >= 2 and len(kinds) == 1 and not multi_candidate_variant
        ):
            kind = next(iter(kinds))
            name_counts = Counter(
                str(item[4].get("identity_name") or item[1]).casefold()
                for item in cohort
            )
            chosen = max(cohort, key=lambda item: (
                name_counts[str(item[4].get("identity_name") or item[1]).casefold()],
                not item[4].get("section_truncated"),
                sum(bool(item[4].get(key)) for key in ("ogrn", "ogrnip", "kpp", "address")),
            ))
            target = "legal_name" if kind == "legal_entity" else "debtor_name"
            role_warning = "Роль и ИНН повторяются, но наименование расходится между OCR; проверьте оригинал"
            card.entity_kind = _field(
                kind, role_warning, ENTITY_CANDIDATE_SOURCE,
                chosen[4]["spans"].get("entity_kind"),
            )
            setattr(card, target, _field(
                chosen[1], role_warning, ENTITY_CANDIDATE_SOURCE,
                chosen[4]["spans"].get(target),
            ))
            card.inn = _field(
                agreed_inn, "ИНН согласуется между проходами OCR; проверьте принадлежность по оригиналу",
                ENTITY_CANDIDATE_SOURCE, chosen[4]["spans"].get("inn"),
            )
    participant_kinds = set()
    for variant_index, text in enumerate(variants[:5]):
        for section, start, truncated, role, role_start, role_end, name_end in _participant_sections(text)[:16]:
            candidate = _section_candidate(section, start, truncated, variant_index, text, name_end,
                                           generic_legal=not bool(LEGAL_PREFIX.match(section)))
            if not candidate:
                continue
            kind, name, inn, legal_form, extra = candidate
            if not name:
                continue
            fields = {}
            target = "legal_name" if kind == "legal_entity" else "debtor_name"
            values = {"entity_kind": kind, target: name, "legal_form": legal_form, "inn": inn,
                      **{key: extra.get(key) for key in ("short_name", "ogrn", "ogrnip", "kpp", "address", "address_kind")}}
            for attr, value in values.items():
                location = extra["spans"].get(attr)
                if value and location:
                    field = _field(value, "Участник дела; роль должника не установлена", "entity_participant:v1", location)
                    fields[attr] = {key: getattr(field, key) for key in field.__dataclass_fields__}
            if target not in fields:
                continue
            participant_kinds.add(kind)
            card.review_suggestions.append({"kind": "entity_participant", "role": role, "review_only": True,
                "warning": "Участник дела; выберите явно. Участие не подтверждает роль должника",
                "copyable_fields": list(fields), "fields": fields,
                "role_location": {"source_variant": variant_index, "source_start": role_start,
                    "source_end": role_end, "source_page": _source_page_at(text, role_start),
                    "source_excerpt": text[role_start:role_end]}})
    if not card.entity_kind.value and len(participant_kinds) == 1:
        kind = next(iter(participant_kinds))
        sample = next(item for item in card.review_suggestions if item.get("kind") == "entity_participant")
        loc = sample["fields"]["entity_kind"]
        card.entity_kind = _field(kind, "Тип участника дела; должник не установлен", ENTITY_CANDIDATE_SOURCE,
            (loc["source_variant"], loc["source_start"], loc["source_end"], loc["source_excerpt"], loc["source_page"]))
    return card
