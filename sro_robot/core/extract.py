"""Извлечение полей карточки должника из распознанного текста.

OCR даёт несколько вариантов текста одного документа. Каждый вариант парсится
отдельно, а итоговое значение поля выбирается голосованием с приоритетом
значений, прошедших контрольную сумму.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from . import morph, validators
from .models import DebtorCard, Field

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
MONTHS_RE = "|".join(MONTHS)

DOC_TYPES = {
    "ОПРЕДЕЛЕНИЕ": "определение",
    "РЕШЕНИЕ": "решение",
    "ЗАЯВЛЕНИЕ": "заявление",
    "ПОСТАНОВЛЕНИЕ": "постановление",
    "УВЕДОМЛЕНИЕ": "уведомление",
}

WORD = r"[А-ЯЁ][а-яёА-ЯЁ\-]+"
DEBTOR_LABEL = re.compile(r"\bдолжник(?:а|у|ом|е|и|ов|ами|ах)?\b\s*[:—-]?", re.I)
IN_RELATION = re.compile(r"\bв\s+отношении\s+(?:гражданина|гражданки)\s+", re.I)
SELF_BANKRUPTCY = re.compile(
    rf"\bзаявлени[ея]\s+(?:гражданина\s+|гражданки\s+)?"
    rf"(?P<name>{WORD}\s+{WORD}\s+{WORD})\s+"
    r"о\s+признании\s+(?:себя|его|е[её])\s+"
    r"(?:несостоятельным\s*\(\s*)?банкротом",
    re.I,
)
ROLE_BOUNDARY = re.compile(
    r"\b(?:должник\w*|заявител\w*|кредитор\w*|представител\w*|"
    r"(?:финансов\w*|арбитражн\w*|конкурсн\w*)\s+управляющ\w*|"
    r"управляющ\w*|истец\w*|ответчик\w*|взыскател\w*|"
    r"заинтересованн\w*\s+лиц\w*)\b",
    re.I,
)
NON_PERSON_PREFIX = re.compile(
    r"^\s*(?:ООО|АО|ПАО|ЗАО|ОАО|ИП\b|индивидуальн\w*\s+предпринимател\w*|"
    r"(?:федеральн\w*\s+)?государственн\w*\s+унитарн\w*\s+предприят\w*|"
    r"муниципальн\w*\s+унитарн\w*\s+предприят\w*|"
    r"юридическ\w*\s+лиц\w*|организаци\w*)\b",
    re.I,
)
PERSON_PREFIX = re.compile(
    r"^\s*(?:(?:гражданин\w*|гражданк\w*|физическ\w*\s+лиц\w*)\s+)+",
    re.I,
)
NAME_TRIPLE = re.compile(rf"({WORD})\s*(?:\([^)]*\)\s*)?({WORD})\s+({WORD})")
ROLE_SOURCE = "debtor_role:v1"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RoleEvidence:
    fio: str | None = None
    sections: tuple[str, ...] = ()
    direct_sections: tuple[str, ...] = ()
    warning: str | None = None
    nominative: bool = False


_BLANK_BREAK = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)*")
_PERSONAL_FIELD_LINE = re.compile(
    r"^(?:ИНН\b|СНИЛС\b|адрес(?:а)?\b|проживающ\w*\s+по\s+адресу\b)",
    re.I,
)


def _bounded_personal_continuation(candidate: str) -> tuple[str, str, str | None]:
    """Cross formatting blanks only into immediate debtor-field lines.

    The direct pre-blank section is retained separately: a field found only
    after a blank cannot establish its own debtor provenance for automation.
    """
    blocks = _BLANK_BREAK.split(candidate, maxsplit=4)
    direct = blocks[0]
    extended = direct
    for block in blocks[1:4]:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            break
        wrapped_address = (
            bool(_ADDRESS_WRAPPED_STREET.match(lines[0]))
            and bool(_ADDRESS_LABEL.search(extended))
            and not _ADDRESS_HOUSE.search(extended)
        )
        if not _PERSONAL_FIELD_LINE.match(lines[0]) and not wrapped_address:
            break
        selected: list[str] = []
        for line in lines:
            if ROLE_BOUNDARY.search(line) or NAME_TRIPLE.search(line):
                return extended, direct, "Другое лицо или роль после полей должника"
            address_wrap = False
            if selected and (
                wrapped_address or any(_ADDRESS_LABEL.match(previous) for previous in selected)
            ):
                address_wrap = bool(
                    _ADDRESS_CONTINUATION.match(line)
                    or _ADDRESS_WRAPPED_STREET.match(line)
                    or (selected[-1].endswith(("ул.", "улица")) and _ADDRESS_HOUSE.search(line))
                )
            elif wrapped_address:
                address_wrap = bool(_ADDRESS_WRAPPED_STREET.match(line))
            if _PERSONAL_FIELD_LINE.match(line) or address_wrap:
                selected.append(line)
            else:
                break
        extended += "\n" + "\n".join(selected)
        if len(selected) != len(lines):
            break
    return extended, direct, None


def _role_evidence(text: str) -> _RoleEvidence:
    """Find one named person in explicit debtor context, never an applicant fallback."""
    anchors: list[tuple[int, bool]] = []
    for marker in DEBTOR_LABEL.finditer(text):
        prefix = text[max(0, marker.start() - 80) : marker.start()]
        if re.search(
            r"(?:представител\w*|управляющ\w*|кредитор\w*)"
            r"(?:\s+[а-яё]+){0,2}[\s-]+$",
            prefix,
            re.I,
        ):
            continue
        anchors.append((marker.end(), marker.group().strip(" :—-").casefold() == "должник"))
    anchors.extend((marker.end(), False) for marker in IN_RELATION.finditer(text))
    anchors.extend((marker.start("name"), False) for marker in SELF_BANKRUPTCY.finditer(text))
    if not anchors:
        return _RoleEvidence(warning="Не установлена явная роль должника-физлица")

    named_sections: list[tuple[str, str, str, bool]] = []
    for start, nominative_label in sorted(set(anchors)):
        next_role = ROLE_BOUNDARY.search(text, start)
        end = min(len(text), start + 500, next_role.start() if next_role else len(text))
        section, direct, boundary_warning = _bounded_personal_continuation(text[start:end])
        if boundary_warning:
            return _RoleEvidence(warning=boundary_warning)
        if NON_PERSON_PREFIX.match(section):
            return _RoleEvidence(warning="Указан должник другого типа")
        without_prefix = PERSON_PREFIX.sub("", section, count=1)
        first = NAME_TRIPLE.match(without_prefix.lstrip(" :—-\t\n"))
        if first is None:
            return _RoleEvidence(warning="ФИО должника не связано с ролью")
        name = " ".join(first.groups())
        if any(stop in name.lower() for stop in ("суд", "кодекс", "закон", "ассоциаци")):
            return _RoleEvidence(warning="Неоднозначное ФИО должника")
        # Another person can appear after the debtor's INN or address without
        # a fresh role label. Do not attach those fields to the first name.
        other_names = {" ".join(match.groups()) for match in NAME_TRIPLE.finditer(without_prefix)}
        if other_names - {name}:
            return _RoleEvidence(warning="Несколько лиц в секции должника")
        named_sections.append((name, section, direct, nominative_label and "(" not in first.group(0)))

    if len({name.casefold() for name, _section, _direct, _nominative in named_sections}) != 1:
        return _RoleEvidence(warning="Несколько разных должников")
    return _RoleEvidence(
        named_sections[0][0],
        tuple(section for _name, section, _direct, _nominative in named_sections),
        direct_sections=tuple(direct for _name, _section, direct, _nominative in named_sections),
        nominative=all(nominative for _name, _section, _direct, nominative in named_sections),
    )


def _clean(text: str) -> str:
    text = text.replace("\u00a0", " ")
    return re.sub(r"[ \t]+", " ", text)


def _header(text: str) -> str:
    """Шапка документа до резолютивной части: там стоит дата самого документа."""
    for marker in ("УСТАНОВИЛ", "ОПРЕДЕЛИЛ", "РЕШИЛ", "ПОСТАНОВИЛ"):
        idx = text.find(marker)
        if idx > 200:
            return text[:idx]
    return text[:1800]


# --- отдельные поля ---------------------------------------------------------

def find_case_number(text: str) -> Optional[str]:
    patterns = (
        r"[Дд]ело\s*[№N]?\s*([АA]\s?\d{1,2}\s?[-–—]\s?\d{3,7}\s?/\s?\d{4})",
        r"[№N]\s*([АA]\s?\d{1,2}\s?[-–—]\s?\d{3,7}\s?/\s?\d{4})",
        r"\b([АA]\d{1,2}[-–—]\d{3,7}/\d{4})\b",
    )
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return validators.normalize_case_number(m.group(1))
    return None


def find_doc_date(text: str) -> Optional[str]:
    header = _header(text)
    m = re.search(rf"(\d{{1,2}})\s+({MONTHS_RE})\s+(\d{{4}})", header)
    if m:
        day, month, year = int(m.group(1)), MONTHS[m.group(2)], m.group(3)
        return f"{day:02d}.{month:02d}.{year}"
    m = re.search(r"\b(\d{2})[.\-/](\d{2})[.\-/](\d{4})\b", header)
    if m:
        return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
    return None


def _case_candidates(text: str) -> set[str]:
    return {value for value, _start, _end in _case_candidate_locations(text)}


def _case_candidate_locations(text: str) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        (validators.normalize_case_number(match.group(0)), match.start(), match.end())
        for match in re.finditer(
            r"\b[АA]\s?\d{1,2}\s?[-–—]\s?\d{3,7}\s?/\s?\d{4}\b", text
        )
    )


def _date_candidates(text: str) -> list[tuple[str, int, int]]:
    header = _header(text)
    found: list[tuple[str, int]] = []
    for match in re.finditer(rf"(\d{{1,2}})\s+({MONTHS_RE})\s+(\d{{4}})", header):
        found.append((f"{int(match.group(1)):02d}.{MONTHS[match.group(2)]:02d}.{match.group(3)}", match.start(), match.end()))
    for match in re.finditer(r"\b(\d{2})[.\-/](\d{2})[.\-/](\d{4})\b", header):
        found.append((f"{match.group(1)}.{match.group(2)}.{match.group(3)}", match.start(), match.end()))
    return found


def _metadata_warnings(card: DebtorCard, variants: list[str]) -> None:
    """Flag reference collisions and dates without an act heading for review."""
    cases = [_case_candidates(text) for text in variants]
    if card.case_number.value and (
        any(len(values) != 1 for values in cases)
        or any(values != {card.case_number.value} for values in cases)
    ):
        card.case_number.warning = "Номер дела расходится между страницами или вариантами OCR"

    courts = [{name for name, _start, _end in _court_candidates(text)} for text in variants]
    if card.court.value and (
        any(len(values) != 1 for values in courts)
        or any(values != {card.court.value} for values in courts)
    ):
        card.court.warning = "Название суда расходится между страницами или вариантами OCR"

    dates = [_date_candidates(text) for text in variants]
    chosen = card.doc_date.value
    if not chosen:
        return
    try:
        day, month, year = (int(part) for part in chosen.split("."))
        date(year, month, day)
    except ValueError:
        card.doc_date.warning = "Недопустимая дата документа"
        return
    for text, candidates in zip(variants, dates):
        heading = re.search(r"\b(?:ОПРЕДЕЛЕНИЕ|РЕШЕНИЕ|ПОСТАНОВЛЕНИЕ)\b", text[:600], re.I)
        date_offset = candidates[0][1] if len(candidates) == 1 else -1
        preceding = text[max(0, date_offset - 80) : date_offset] if date_offset >= 0 else ""
        referenced_act = bool(re.search(
            r"\b(?:ранее|предыдущ\w*|обжалуем\w*)\b.{0,40}"
            r"\b(?:решение|определение|постановление|судебн\w*\s+акт)\b",
            preceding,
            re.I | re.S,
        ))
        role_before_date = bool(
            heading is not None
            and date_offset >= 0
            and ROLE_BOUNDARY.search(text[heading.end() : date_offset])
        )
        if (
            len(candidates) != 1
            or candidates[0][0] != chosen
            or heading is None
            or abs(candidates[0][1] - heading.start()) > 250
            or role_before_date
            or referenced_act
            or re.search(r"дата\s+рождения|заседани\w*\s+назначено", preceding, re.I)
        ):
            card.doc_date.warning = "Дата судебного акта неоднозначна; проверьте заголовок"
            return


def find_fio(text: str) -> Optional[str]:
    return _role_evidence(_clean(text)).fio


def find_inn(text: str) -> Optional[str]:
    evidence = _role_evidence(_clean(text))
    if not evidence.fio:
        return None
    values = {
        validators.digits(match.group(1))
        for section in evidence.sections
        for match in re.finditer(r"ИНН[:\s]*([\d\s]{10,17})", section)
    }
    return next(iter(values)) if len(values) == 1 and valid_person_inn(next(iter(values))) else None


def valid_person_inn(value: str) -> bool:
    return len(value) == 12 and validators.valid_inn(value)


def find_snils(text: str) -> Optional[str]:
    evidence = _role_evidence(_clean(text))
    if not evidence.fio:
        return None
    values = {
        validators.digits(match.group(1))
        for section in evidence.sections
        for match in re.finditer(r"СНИЛС[:\s]*([\d\s\-]{11,20})", section)
    }
    if len(values) != 1:
        return None
    value = next(iter(values))
    return validators.format_snils(value) if validators.valid_snils(value) else None


def find_address(text: str) -> tuple[Optional[str], Optional[str]]:
    """Возвращает адрес и его тип: регистрации или фактический."""
    registration = re.search(
        r"адрес(?:а)?\s+регистрации[:\s]*(.+?)"
        r"(?=,?\s*место\s+рождения|\)\s*о\s+призна|о\s+призна|\n\s*\n|$)",
        text,
        re.S | re.I,
    )
    if registration:
        return _tidy_address(registration.group(1)), "registration"

    generic = re.search(
        r"(?:адрес|проживающ\w+\s+по\s+адресу)[:\s]*(\d{6},?\s*.+?)"
        r"(?=\)\s*о\s+призна|о\s+призна|\n\s*\n|$)",
        text,
        re.S | re.I,
    )
    if generic:
        return _tidy_address(generic.group(1)), "actual"
    return None, None


# Слова, которые OCR приклеивает к адресу из следующей фразы документа.
# Корни обрезаны, потому что Tesseract искажает окончания: «призиации», «банкротом».
ADDRESS_TAIL = re.compile(
    r"\s*(?:[оОа0]?\s*призна|[оОа0]?\s*призиа|несостоятел|несостоятсл|банкрот|баикрот"
    r"|УСТАНОВИЛ|заявление\s+подано)",
    re.I,
)


def _tidy_address(raw: str) -> str:
    addr = re.sub(r"\s+", " ", raw).strip(" ,.;:)(")
    addr = ADDRESS_TAIL.split(addr)[0]
    return addr.strip(" ,.;:)(")


def _court_caption(match: re.Match[str]) -> tuple[str, int, int]:
    name = match.group(0)
    # Court captions may include a city, republic, or multiword federal
    # district. Stop before case/date/judge details when they share the line.
    name = re.split(
        r"\s+(?=(?:дело\b|по\s+делу\b|№|в\s+составе\b|судь[яе]\b|"
        r"ОПРЕДЕЛЕНИЕ\b|РЕШЕНИЕ\b|ПОСТАНОВЛЕНИЕ\b|"
        rf"\d{{1,2}}\s+(?:{MONTHS_RE})\s+\d{{4}}\b|"
        r"\d{2}[.\-/]\d{2}[.\-/]\d{4}\b))",
        name,
        maxsplit=1,
        flags=re.I,
    )[0]
    trimmed = name.strip(" :—-.")
    leading = len(name) - len(name.lstrip(" :—- ."))
    start = match.start() + leading
    end = start + len(trimmed)
    return re.sub(r"\s+", " ", trimmed), start, end


def _court_candidates(text: str) -> tuple[tuple[str, int, int], ...]:
    candidates: dict[str, tuple[str, int, int]] = {}
    for match in re.finditer(r"\bАрбитражный\s+суд\s+([^\n,;]{1,180})", text, re.I):
        caption = _court_caption(match)
        if caption[0]:
            candidates.setdefault(caption[0], caption)
    return tuple(candidates.values())


def find_court(text: str) -> Optional[str]:
    candidates = _court_candidates(text)
    return candidates[0][0] if candidates else None


def _attach_court_source(field: Field, variants: list[str]) -> None:
    if not field.value:
        return
    for variant_index, text in enumerate(variants):
        location = next(
            (candidate for candidate in _court_candidates(text) if candidate[0] == field.value),
            None,
        )
        if location is None:
            continue
        _name, start, end = location
        field.source_variant = variant_index
        field.source_start = start
        field.source_end = end
        field.source_excerpt = text[max(0, start - 50):min(len(text), end + 50)]
        return


def _attach_field_source(
    field: Field,
    variants: list[str],
    locator: Callable[[str, str], tuple[int, int] | None],
) -> None:
    if not field.value:
        return
    for variant_index, text in enumerate(variants):
        location = locator(text, field.value)
        if location is None:
            continue
        start, end = location
        field.source_variant = variant_index
        field.source_start = start
        field.source_end = end
        field.source_excerpt = text[max(0, start - 50):min(len(text), end + 50)]
        return


def _locate_doc_type(text: str, expected: str) -> tuple[int, int] | None:
    head = text[:1500].upper()
    matches = [
        (position, name, len(marker))
        for marker, name in DOC_TYPES.items()
        if (position := head.find(marker)) >= 0
    ]
    if not matches:
        return None
    position, name, length = min(matches)
    return (position, position + length) if name == expected else None


def _locate_doc_date(text: str, expected: str) -> tuple[int, int] | None:
    return next(
        ((start, end) for value, start, end in _date_candidates(text) if value == expected),
        None,
    )


def _locate_case_number(text: str, expected: str) -> tuple[int, int] | None:
    return next(
        ((start, end) for value, start, end in _case_candidate_locations(text) if value == expected),
        None,
    )


def find_doc_type(text: str) -> Optional[str]:
    head = text[:1500].upper()
    markers = ((head.find(marker), name) for marker, name in DOC_TYPES.items())
    candidates = [(position, name) for position, name in markers if position >= 0]
    return min(candidates)[1] if candidates else None


# --- голосование ------------------------------------------------------------

def _address_score(value: str) -> tuple:
    """Чистый адрес начинается индексом, кончается номером и не тащит хвостов."""
    has_index = bool(re.match(r"^\d{6}\b", value))
    ends_clean = bool(re.search(r"\d\s*$", value))
    no_garbage = not re.search(r"призна|призиа|банкрот|несостоятел", value, re.I)
    # Короткий вариант при прочих равных почти всегда точнее.
    return (no_garbage, has_index, ends_clean, -len(value))


def _vote(
    variants: list[str],
    finder: Callable[[str], Optional[str]],
    is_valid: Optional[Callable[[str], bool]] = None,
    score: Optional[Callable[[str], tuple]] = None,
) -> Field:
    found = [v for v in (finder(t) for t in variants) if v]
    if not found:
        return Field(source="not_found", warning="поле не найдено")

    counter = Counter(found)

    if score is not None:
        best = max(counter, key=lambda v: (score(v), counter[v]))
        agreement = counter[best] / len(variants)
        warning = None if agreement >= 0.5 else "варианты OCR разошлись, проверьте вручную"
        return Field(best, round(0.4 + 0.5 * agreement, 2), "ocr+heuristic", warning)

    valid = [v for v in counter if is_valid(v)] if is_valid else []

    if valid:
        best = max(valid, key=lambda v: counter[v])
        agreement = counter[best] / len(variants)
        return Field(best, min(1.0, 0.7 + 0.3 * agreement), "ocr+checksum")

    best, hits = counter.most_common(1)[0]
    confidence = 0.35 + 0.4 * (hits / len(variants))
    warning = None
    if is_valid:
        warning = "не прошло проверку контрольной суммы"
    elif hits == 1 and len(variants) > 1:
        warning = "варианты OCR разошлись, проверьте вручную"
    return Field(best, round(confidence, 2), "ocr", warning)


def _section_inn(section: str) -> tuple[str | None, str | None]:
    values = {
        validators.digits(match.group(1))
        for match in re.finditer(r"ИНН[:\s]*([\d\s]{10,17})", section)
    }
    if not values:
        return None, None
    valid_values = {value for value in values if valid_person_inn(value)}
    if len(valid_values) > 1:
        return None, "Несколько корректных ИНН в секции должника"
    if len(valid_values) == 1:
        candidate = next(iter(valid_values))
        if len(values) > 1:
            return candidate, "Есть повреждённые варианты распознавания ИНН"
        return candidate, None
    if len(values) != 1 or not valid_person_inn(next(iter(values))):
        return None, "ИНН должника-физлица неоднозначен или не прошёл проверку"
    return next(iter(values)), None


def _section_snils(section: str) -> tuple[str | None, str | None]:
    values = {
        validators.digits(match.group(1))
        for match in re.finditer(r"СНИЛС[:\s]*([\d\s\-]{11,20})", section)
    }
    if not values:
        return None, None
    if len(values) != 1 or not validators.valid_snils(next(iter(values))):
        return None, "СНИЛС должника неоднозначен или не прошёл проверку"
    return validators.format_snils(next(iter(values))), None


def _section_address(section: str) -> tuple[str | None, str | None]:
    address, _kind, warning = _scoped_address(section)
    return address, warning


def _section_address_kind(section: str) -> tuple[str | None, str | None]:
    _address, kind, warning = _scoped_address(section)
    return kind, warning


_ADDRESS_LABEL = re.compile(
    r"\b(?:(адрес(?:а)?\s+регистрации)\b\s*:?[ \t]*|"
    r"(адрес|проживающ\w+\s+по\s+адресу)\s*:\s*)",
    re.I,
)
_ADDRESS_HOUSE = re.compile(r"\b(?:д\.?|дом|домовладение)\s*№?\s*\d+[а-яёa-z]?(?:[/\-]\d+[а-яёa-z]?)?\b", re.I)
_ADDRESS_UNIT = re.compile(
    r"\s*,?\s*(?:корп(?:ус)?\.?|стр(?:оение)?\.?|кв(?:артира)?\.?|"
    r"пом(?:ещение)?\.?|офис)\s*№?\s*\d+[а-яёa-z]?(?:[/\-]\d+)?\b",
    re.I,
)
_ADDRESS_CONTINUATION = re.compile(
    r"^(?:\d{6}\b|(?:г\.|город|ул\.|улица|пр\.?|проспект|пер\.|переулок|"
    r"ш\.|шоссе|обл\.?|область|р-н|район|пос\.?|поселок|с\.|село|"
    r"д\.|дом|корп\.?|стр\.?|кв\.|квартира)(?=\s|$))",
    re.I,
)
_ADDRESS_WRAPPED_STREET = re.compile(
    r"^(?P<fragment>[а-яё]{1,3}),\s+(?P<street>(?:ул\.|улица)\s+.+)$",
    re.I,
)


def _scoped_address(section: str) -> tuple[str | None, str | None, str | None]:
    """Accept only a delimited debtor address; prose after it is not evidence."""
    labels = list(_ADDRESS_LABEL.finditer(section))
    if not labels:
        if re.search(r"\b(?:адрес\w*|проживающ\w*)\b", section, re.I):
            return None, None, "Адрес должника упомянут без распознаваемой границы"
        return None, None, None
    if len(labels) != 1:
        return None, None, "Несколько адресов в секции должника; проверьте вручную"
    label = labels[0]
    kind = "registration" if label.group(1) else "actual"
    lines = section[label.end():].splitlines()
    if not lines:
        return None, None, "Граница адреса должника не установлена"
    chunks = [lines[0].strip()]
    for line in lines[1:4]:
        line = line.strip()
        if not line:
            break
        wrapped_street = _ADDRESS_WRAPPED_STREET.match(line)
        if _ADDRESS_HOUSE.search(" ".join(chunks)):
            if not _ADDRESS_UNIT.match(line):
                break
        elif not (
            _ADDRESS_CONTINUATION.match(line)
            or (wrapped_street and _ADDRESS_HOUSE.search(wrapped_street.group("street"))
                and re.search(r"[а-яё]$", chunks[-1], re.I))
            or (chunks[-1].endswith(("ул.", "улица"))
                and _ADDRESS_HOUSE.search(line))
        ):
            break
        if wrapped_street:
            chunks[-1] += wrapped_street.group("fragment") + ","
            chunks.append(wrapped_street.group("street"))
        else:
            chunks.append(line)
    raw = " ".join(chunks)
    house = _ADDRESS_HOUSE.search(raw)
    if not house or house.start() > 250:
        return None, None, "Граница адреса должника не установлена"
    end = house.end()
    while unit := _ADDRESS_UNIT.match(raw, end):
        end = unit.end()
    address = _tidy_address(raw[:end])
    if not address:
        return None, None, "Граница адреса должника не установлена"
    return address, kind, None


def _role_field(
    evidence: list[_RoleEvidence],
    finder: Callable[[str], tuple[str | None, str | None]],
    *,
    retain_valid_candidate: bool = False,
) -> Field:
    """Vote only within the single agreed debtor identity, rejecting contradictions."""
    per_variant: list[str | None] = []
    invalid_warnings: list[str] = []
    for item in evidence:
        values: set[str] = set()
        for section in item.sections:
            value, warning = finder(section)
            if warning:
                if not retain_valid_candidate:
                    return Field(source="role_ambiguous", warning=warning)
                if warning not in (
                    "Есть повреждённые варианты распознавания ИНН",
                    "ИНН должника-физлица неоднозначен или не прошёл проверку",
                ):
                    return Field(source="role_ambiguous", warning=warning)
                invalid_warnings.append(warning)
            if value:
                values.add(value)
        if len(values) > 1:
            return Field(source="role_ambiguous", warning="Разные значения в секциях должника")
        per_variant.append(next(iter(values)) if values else None)

    observed = {value for value in per_variant if value}
    if not observed:
        if invalid_warnings:
            return Field(source="role_ambiguous", warning=invalid_warnings[0])
        return Field(source="not_found")
    if len(observed) > 1:
        return Field(source="role_ambiguous", warning="Варианты OCR расходятся по полю должника")
    value = next(iter(observed))
    if invalid_warnings:
        return Field(
            value,
            round(0.35 + 0.4 * (sum(candidate == value for candidate in per_variant) / len(evidence)), 2),
            "role_partial",
            "Есть повреждённые варианты распознавания; проверьте реквизит по оригиналу",
        )
    agreement = sum(candidate == value for candidate in per_variant) / len(evidence)
    direct_confirmation = any(
        finder(section)[0] == value
        for item in evidence for section in item.direct_sections
    )
    complete = (
        agreement == 1.0 and direct_confirmation
        and all(item.fio and not item.warning for item in evidence)
    )
    return Field(
        value,
        round(0.35 + 0.4 * agreement, 2),
        ROLE_SOURCE if complete else "role_partial",
        None if complete else (
            "Поле после разрыва секции не подтверждено без разрыва; проверьте принадлежность"
            if agreement == 1.0 else "Поле должника подтверждено не всеми вариантами OCR"
        ),
    )


def parse(variants: list[str]) -> DebtorCard:
    variants = [_clean(v) for v in variants if v and v.strip()]
    if not variants:
        raise ValueError("Нет текста для разбора")

    card = DebtorCard(raw_text=max(variants, key=len))

    card.doc_type = _vote(variants, find_doc_type)
    card.doc_date = _vote(variants, find_doc_date)
    card.case_number = _vote(variants, find_case_number)
    card.court = _vote(variants, find_court)
    _attach_field_source(card.doc_type, variants, _locate_doc_type)
    _attach_field_source(card.doc_date, variants, _locate_doc_date)
    _attach_field_source(card.case_number, variants, _locate_case_number)
    _attach_court_source(card.court, variants)
    _metadata_warnings(card, variants)
    role_evidence = [_role_evidence(text) for text in variants]
    names = {item.fio.casefold() for item in role_evidence if item.fio}
    ambiguity = next(
        (
            item.warning for item in role_evidence
            if item.warning and item.warning != "Не установлена явная роль должника-физлица"
        ),
        None,
    )
    if len(names) != 1 or ambiguity:
        warning = ambiguity or (
            "Варианты OCR называют разных должников" if names else "Не установлена явная роль должника-физлица"
        )
        card.fio_raw = Field(source="role_ambiguous", warning=warning)
        for name in ("inn", "snils", "address", "address_kind"):
            setattr(card, name, Field(source="role_ambiguous", warning=warning))
    else:
        named = [item for item in role_evidence if item.fio]
        fio = named[0].fio
        agreement = len(named) / len(role_evidence)
        complete = agreement == 1.0 and all(not item.warning for item in role_evidence)
        card.fio_raw = Field(
            fio,
            round(0.35 + 0.4 * agreement, 2),
            ROLE_SOURCE if complete else "role_partial",
            None if complete else "ФИО должника подтверждено не всеми вариантами OCR",
        )
        # A lone checksum-valid INN remains a manual-review suggestion when
        # another OCR pass mangles the token; it cannot meet the automatic card
        # gate because its source is role_partial and carries a warning.
        card.inn = _role_field(role_evidence, _section_inn, retain_valid_candidate=True)
        card.snils = _role_field(role_evidence, _section_snils)
        # Birth date and birth place stay out of the review card. Address parsing
        # still stops at the birth-place label so that phrase is not absorbed.
        card.address = _role_field(role_evidence, _section_address)
        card.address_kind = _role_field(role_evidence, _section_address_kind)

    if card.fio_raw.value:
        raw_parts = tuple(card.fio_raw.value.split())
        normalized = morph.normalize_fio(card.fio_raw.value)
        explicit_nominative = all(item.nominative for item in named)
        # The word "Должник" does not prove the following name is nominative:
        # "Должник Сидоровой Анны Сергеевны" occurs in source documents.
        oblique_given_name = normalized[1:] != raw_parts[1:]
        masculine_patronymic = raw_parts[2].endswith(("ович", "евич", "ьич"))
        masculine_genitive_surname = masculine_patronymic and raw_parts[0].endswith(("ова", "ева"))
        if explicit_nominative and not oblique_given_name and not masculine_genitive_surname:
            last, first, middle = raw_parts
        else:
            last, first, middle = normalized
        conf = card.fio_raw.confidence
        name_warning = card.fio_raw.warning
        if oblique_given_name or masculine_genitive_surname or (not explicit_nominative and normalized != raw_parts):
            name_warning = "Склонённое ФИО нормализовано; проверьте написание"
        name_source = ROLE_SOURCE if not name_warning and explicit_nominative else "morph"
        card.last_name = Field(last, conf, name_source, name_warning)
        card.first_name = Field(first, conf, name_source, name_warning)
        card.middle_name = Field(middle, conf, name_source, name_warning)

    if not card.inn.value and card.case_number.value and not card.inn.warning:
        card.inn.warning = "ИНН не найден — в карточку уйдёт номер дела"

    return card


def parse_file(
    path,
    dpi: int = 300,
    *,
    max_pdf_pages: int = 80,
    max_image_pixels: int = 12_000_000,
    budget=None,
) -> DebtorCard:
    path = Path(path)
    from .ocr import (
        OcrBudget,
        recognize,
        recover_role_bound_inn,
    )

    budget = budget or OcrBudget()
    variants = recognize(
        path,
        dpi=dpi,
        max_pdf_pages=max_pdf_pages,
        max_image_pixels=max_image_pixels,
        budget=budget,
    )
    # Business debtors use a separate, conservative parser. Do not run the
    # person-name normalizer over a company or an entrepreneur name.
    from .entity_extract import has_explicit_entity_marker, parse_entity
    if any(has_explicit_entity_marker(variant) for variant in variants):
        card = parse_entity(variants)
        _cross_check_low_resolution_date(
            path, card, variants, budget, max_pdf_pages, max_image_pixels,
        )
        return card
    card = parse(variants)
    if (
        card.fio_raw.value
        and not card.inn.value
        and card.inn.source == "role_ambiguous"
        and card.inn.warning
        and any(re.search(r"\bИНН\b", variant, re.I) for variant in variants)
    ):
        try:
            recovery = recover_role_bound_inn(
                path, variants, dpi=dpi, budget=budget,
                max_image_pixels=max_image_pixels,
            )
        except Exception as exc:
            # Optional recovery must not turn a usable OCR result into a failed task.
            logger.warning(
                "Optional debtor INN recovery failed; original OCR retained error_type=%s",
                type(exc).__name__,
            )
            recovery = []
        if recovery:
            card = parse([*variants, *recovery])
        if path.suffix.lower() == ".pdf" and dpi > 200 and not card.inn.value:
            # A damaged token can remain checksum-invalid at the normal and
            # high-resolution recovery passes. A smaller, still bounded raster
            # can avoid duplicated glyphs on sparse scans; it stays partial
            # evidence and therefore cannot bypass the employee review gate.
            try:
                lower_dpi_recovery = recover_role_bound_inn(
                    path, variants, dpi=200, budget=budget,
                    max_image_pixels=max_image_pixels,
                )
            except Exception as exc:
                logger.warning(
                    "Lower-resolution debtor INN recovery failed; original OCR retained error_type=%s",
                    type(exc).__name__,
                )
                lower_dpi_recovery = []
            if lower_dpi_recovery:
                candidate = parse([*variants, *recovery, *lower_dpi_recovery])
                if candidate.inn.value:
                    card = candidate
    _cross_check_low_resolution_date(
        path, card, variants, budget, max_pdf_pages, max_image_pixels,
    )
    return card


def _cross_check_low_resolution_date(
    path: Path,
    card: DebtorCard,
    variants: list[str],
    budget,
    max_pdf_pages: int,
    max_image_pixels: int,
) -> None:
    """Offer alternate header evidence without letting it bypass manual review."""
    if (
        path.suffix.lower() != ".pdf"
        or len(variants) < 2
        or not card.doc_date.value
        or card.doc_date.warning
    ):
        return
    from .ocr import recover_low_resolution_header

    try:
        variants = recover_low_resolution_header(
            path,
            budget=budget,
            max_pdf_pages=max_pdf_pages,
            max_image_pixels=max_image_pixels,
        )
    except Exception as exc:
        logger.warning(
            "Low-resolution date cross-check failed; original OCR retained error_type=%s",
            type(exc).__name__,
        )
        return
    if not variants:
        return
    lower_resolution = parse(variants).doc_date
    if (
        not lower_resolution.value
        or lower_resolution.warning
        or lower_resolution.value == card.doc_date.value
    ):
        return
    card.doc_date.value = lower_resolution.value
    card.doc_date.source = "ocr_scale_crosscheck"
    card.doc_date.confidence = min(card.doc_date.confidence, 0.69)
    card.doc_date.warning = "Распознавание даты расходится при другом масштабе; проверьте заголовок"
