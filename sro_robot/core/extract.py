"""Извлечение полей карточки должника из распознанного текста.

OCR даёт несколько вариантов текста одного документа. Каждый вариант парсится
отдельно, а итоговое значение поля выбирается голосованием с приоритетом
значений, прошедших контрольную сумму.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterator, Optional

from . import morph, person_attributes, validators
from .address import STREET_RE, UNIT_NUMBER_PATTERN
from .person_attributes import detect_person_attributes
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

# Match whole Cyrillic tokens. Orthographic hyphens join parts; an uncertain
# dash must not expose the remainder as another person's surname.
WORD = (
    r"(?<![\w‐‑‒–—−-])[А-ЯЁ][а-яёА-ЯЁ]+"
    r"(?:[-‐‑][а-яёА-ЯЁ]+)*(?![\w‐‑‒–—−-])"
)
DEBTOR_LABEL = re.compile(r"\bдолжник(?:а|у|ом|е|и|ов|ами|ах)?\b\s*[:—-]?", re.I)
IN_RELATION = re.compile(r"\bв\s+отношении\s+(?:гражданина|гражданки)\s+", re.I)
SELF_BANKRUPTCY = re.compile(
    rf"\bзаявлени[ея]\s+(?:гражданина\s+|гражданки\s+)?"
    rf"(?P<name>{WORD}\s+{WORD}\s+{WORD})\s+"
    r"о\s+признании\s+себя\s+"
    r"(?:несостоятельн(?:ым|ой)\s*\(\s*)?банкротом",
    re.I,
)
ROLE_BOUNDARY = re.compile(
    r"\b(?:должник\w*|заявител\w*|кредитор\w*|представител\w*|"
    r"(?:финансов\w*|арбитражн\w*|конкурсн\w*)\s+управляющ\w*|"
    r"управляющ\w*|истец\w*|ответчик\w*|взыскател\w*|"
    r"заинтересованн\w*\s+лиц\w*)\b",
    re.I,
)
_SIGNATORY_BOUNDARY = re.compile(
    r"(?:^|(?<=\f))[ \t]*(?:судья\b|сведения[ \t]+об[ \t]+электронной[ \t]+подписи\b|"
    r"(?:документ[ \t]+)?подписан[ао]?[ \t]+(?:усиленной[ \t]+квалифицированной[ \t]+)?"
    r"электронной[ \t]+подписью\b|владелец[ \t]+сертификата\b)",
    re.I | re.M,
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
# Name components need whitespace or an explicit former-surname parenthesis.
NAME_TRIPLE = re.compile(rf"({WORD})(?:\s*\([^)]*\)\s*|\s+)({WORD})\s+({WORD})")
NAME_PAIR = re.compile(rf"({WORD})[ \t\r\n]+({WORD})")
_LABELED_TWO_PART_PERSON = re.compile(
    r"\b(?i:граждан(?:ин(?:а|у|ом|е)?|к(?:а|и|е|у|ой))|"
    r"должник(?:а|у|ом|е)?|заявител(?:ь|я|ю|ем|е))\b[ \t]*[:—-]?[ \t]*"
    rf"(?P<name>{WORD}[ \t\r\n]{{1,3}}{WORD})"
    r"(?=[ \t]*(?:[,;(:.]|\r?$|\f))",
    re.M,
)
ROLE_SOURCE = "debtor_role:v1"
APPLICANT_ROLE_SOURCE = "applicant_role:v1"
_APPLICANT_LABEL = re.compile(r"(?im)^[ \t]*заявитель[ \t]*[:—-][ \t]*")
_APPLICANT_NON_IDENTITY_PREFIX = re.compile(
    r"^(?:по\s+делу|просит|обратил\w*|не\s+явил\w*|судья|секретарь|"
    r"председательствующ\w*|помощник\s+судьи)\b",
    re.I,
)
_PETITIONING_COMPANY = re.compile(
    r"\bпо\s+заявлению\s+"
    r"(?P<name>(?:ООО|ПАО|АО|ЗАО|ОАО)\s*"
    r"(?:«[^»\n]{2,120}»|\"[^\"\n]{2,120}\"))"
    r"(?=\s*(?:\(|,|;|\n|о\s+признании|$))",
    re.I,
)
_APPLICANT_STOP = re.compile(
    r"\b(?:ИНН|ОГРН(?:ИП)?|КПП|СНИЛС|адрес|должник|кредитор|"
    r"представитель|управляющий)\b\s*[:—-]?",
    re.I,
)
APPLICATION_NAME = re.compile(
    rf"\bрассмотрев\s+"
    r"(?:в\s+(?:открытом|закрытом)\s+судебном\s+заседании\s+)?"
    r"(?:без\s+участия\s+представителей\s+сторон\s*,?\s*)?"
    rf"заявлени[ея]\s+(?:гражданина\s+|гражданки\s+)?"
    rf"(?P<name>{WORD}(?:\s*\([^)]*\))?\s+{WORD}\s+{WORD})\b",
    re.I,
)
INSOLVENCY_CASE_NAME = re.compile(
    rf"\bдел\w*\s+о\s+несостоятельности(?:\s*\(\s*банкротств\w*\s*\))?\s+"
    rf"(?P<name>{WORD}(?:\s*\([^)]*\))?\s+{WORD}\s+{WORD})\b",
    re.I,
)
BANKRUPTCY_PERSON_NAME = re.compile(
    rf"\bо\s+(?:банкротстве|признании)\s+(?:гражданина|гражданки)\s+"
    rf"(?P<name>{WORD}(?:\s*\([^)]*\))?\s+{WORD}\s+{WORD})\b",
    re.I,
)
REVERSE_PERSON_DEBTOR_NAME = re.compile(
    rf"(?P<name>{WORD}\s+{WORD}\s+{WORD})\s*"
    r"\(\s*(?:далее\s*[-—]\s*)?должник\s*\)",
    re.I,
)
_REVERSE_PERSON_DEBTOR_SUFFIX = re.compile(
    r"\(\s*(?:далее\s*[-—]\s*)?должник\s*\)", re.I,
)


def _reverse_person_debtor_matches(text: str) -> Iterator[re.Match[str]]:
    # The suffix is necessary for every full match. Avoid the unanchored
    # three-word scan when absent; otherwise retain exact matches and offsets.
    if _REVERSE_PERSON_DEBTOR_SUFFIX.search(text) is None:
        return iter(())
    return REVERSE_PERSON_DEBTOR_NAME.finditer(text)


ROLE_CANDIDATE_WARNING = (
    "ФИО и реквизиты найдены в тексте документа; связь с должником проверьте по оригиналу"
)
_PERSON_CONTEXT = re.compile(
    r"(?:гражданин\w*|гражданк\w*|заявител\w*|должник\w*|физическ\w*\s+лиц\w*)",
    re.I,
)
_PERSON_BIOGRAPHY_BOUNDARY = re.compile(
    r"(?:^|(?<=[.;]))[ \t]*(?i:гражданин\w*|гражданк\w*|физическ\w*\s+лиц\w*)"
    r"\b[ \t]*(?:[:—-][ \t]*)?(?=\w)",
    re.M,
)
_OTHER_PERSON_CONTEXT = re.compile(
    r"(?:судь\w*|представител\w*|управляющ\w*|кредитор\w*|истец\w*|"
    r"ответчик\w*|взыскател\w*)",
    re.I,
)
_PATRONYMIC_SHAPE = re.compile(r"(?i:.*(?:ович|евич|ьевич|ич|овна|евна|ьевна|ична|"
                                r"овича|евича|ьевича|ича|овны|евны|ьевны|ичны|"
                                r"овичу|евичу|ичу|овной|евной|ьевной|ичной|"
                                r"овне|евне|ьевне|овну|евну|ьевну|"
                                r"овичем|евичем|ичем|овиче|евиче))$")
_BIRTHPLACE_SPAN = re.compile(r"\bместо\s+рождения\s*[:—-]?\s*[^,;\n]{2,100}", re.I)
logger = logging.getLogger(__name__)
_QUOTE_PAIRS = {"«": "»", '"': '"', "„": "“", "“": "”", "‘": "’"}
_QUOTE_MARK = re.compile('[«»"„“”‘’]')


def _advance_quote(stack: list[str], mark: str) -> None:
    # The curly double mark closes a low-opening quote before it can open an
    # English-style quote. Different nested quote styles retain their pairing.
    if stack and mark == stack[-1]:
        stack.pop()
    elif mark in _QUOTE_PAIRS:
        stack.append(_QUOTE_PAIRS[mark])
    elif not stack:
        # An unmatched closing mark leaves this page's ownership uncertain.
        stack.append("")


def _outside_quotes(text: str, position: int) -> bool:
    prefix = text[:position].rsplit("\f", 1)[-1]
    stack: list[str] = []
    for match in _QUOTE_MARK.finditer(prefix):
        _advance_quote(stack, match[0])
    return not stack


@dataclass(frozen=True)
class _RoleEvidence:
    fio: str | None = None
    sections: tuple[str, ...] = ()
    section_maps: tuple[tuple[int, ...], ...] = ()
    direct_sections: tuple[str, ...] = ()
    fio_location: tuple[int, int] | None = None
    warning: str | None = None
    nominative: bool = False


def _role_identity_key(name: str) -> tuple[str, ...]:
    parts = tuple(name.split())
    # Only a grammatically valid triplet may unify an oblique occurrence with
    # its nominative form. Unknown or damaged names retain exact separation.
    normalized = morph.person_triplet(parts) if len(parts) == 3 else None
    return tuple(part.casefold() for part in (normalized or parts))


_BLANK_BREAK = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)*")
_PERSONAL_FIELD_LINE = re.compile(
    r"^(?:ИНН\b|СНИЛС\b|адрес(?:а)?\b|проживающ\w*\s+по\s+адресу\b)",
    re.I,
)


def _page_window_end(text: str, start: int, end: int) -> int:
    page_end = text.find("\f", start, end)
    return end if page_end < 0 else page_end


def _personal_section_end(text: str, start: int, end: int, *, after_name: bool = False) -> int:
    end = _page_window_end(text, start, end)
    for boundary in (ROLE_BOUNDARY, _SIGNATORY_BOUNDARY):
        if marker := boundary.search(text, start, end):
            end = marker.start()
    if next_person := _LABELED_TWO_PART_PERSON.search(text, start, end):
        end = next_person.start()
    if after_name and (next_person := _PERSON_BIOGRAPHY_BOUNDARY.search(text, start, end)):
        # A malformed next name still ends this occurrence's field ownership.
        end = next_person.start()
    return end


def _bounded_personal_continuation(
    candidate: str,
) -> tuple[str, str, str | None, tuple[int, ...]]:
    """Cross formatting blanks only into immediate debtor-field lines.

    The direct pre-blank section is retained separately: a field found only
    after a blank cannot establish its own debtor provenance for automation.
    """
    block_spans = []
    cursor = 0
    for separator in _BLANK_BREAK.finditer(candidate):
        if len(block_spans) == 4:
            break
        block_spans.append((cursor, separator.start()))
        cursor = separator.end()
    block_spans.append((cursor, len(candidate)))
    direct_start, direct_end = block_spans[0]
    direct = candidate[direct_start:direct_end]
    extended = direct
    source_map = list(range(direct_start, direct_end))

    def nonempty_lines(block_start: int, block_end: int) -> list[tuple[str, int, int]]:
        found = []
        line_start = block_start
        for raw_line in candidate[block_start:block_end].splitlines(keepends=True):
            line = raw_line.rstrip("\r\n")
            stripped = line.strip()
            if stripped:
                leading = len(line) - len(line.lstrip())
                found.append((stripped, line_start + leading, line_start + leading + len(stripped)))
            line_start += len(raw_line)
        return found

    for block_start, block_end in block_spans[1:4]:
        line_spans = nonempty_lines(block_start, block_end)
        if not line_spans:
            break
        lines = [line for line, _start, _end in line_spans]
        wrapped_address = (
            bool(_ADDRESS_WRAPPED_STREET.match(lines[0]))
            and bool(_ADDRESS_LABEL.search(extended))
            and not _ADDRESS_HOUSE.search(extended)
        )
        if not _PERSONAL_FIELD_LINE.match(lines[0]) and not wrapped_address:
            break
        selected: list[tuple[str, int, int]] = []
        for (line, line_start, line_end) in line_spans:
            if ROLE_BOUNDARY.search(line) or NAME_TRIPLE.search(line):
                return extended, direct, "Другое лицо или роль после полей должника", tuple(source_map)
            address_wrap = False
            if selected and (
                wrapped_address or any(_ADDRESS_LABEL.match(previous[0]) for previous in selected)
            ):
                address_wrap = bool(
                    _ADDRESS_CONTINUATION.match(line)
                    or _ADDRESS_WRAPPED_STREET.match(line)
                    or (selected[-1][0].endswith(("ул.", "улица")) and _ADDRESS_HOUSE.search(line))
                )
            elif wrapped_address:
                address_wrap = bool(_ADDRESS_WRAPPED_STREET.match(line))
            if _PERSONAL_FIELD_LINE.match(line) or address_wrap:
                selected.append((line, line_start, line_end))
            else:
                break
        extended += "\n" + "\n".join(line for line, _start, _end in selected)
        source_map.append(selected[0][1])
        for index, (_line, line_start, line_end) in enumerate(selected):
            if index:
                source_map.append(line_start)
            source_map.extend(range(line_start, line_end))
        if len(selected) != len(lines):
            break
    return extended, direct, None, tuple(source_map)


def _role_evidence(text: str) -> _RoleEvidence:
    """Find one named person in explicit debtor context, never an applicant fallback."""
    # start, first position eligible for a later role boundary, nominative,
    # name-only, decisive role position. A bankruptcy subject names the debtor,
    # but narrative after that name cannot establish ownership of an identifier.
    anchors: list[tuple[int, int, bool, bool, int]] = []
    for marker in DEBTOR_LABEL.finditer(text):
        if "\f" in marker[0]:
            continue
        prefix = text[max(0, marker.start() - 80) : marker.start()].rsplit("\f", 1)[-1]
        if re.search(
            r"(?:представител\w*|управляющ\w*|кредитор\w*)"
            r"(?:\s+[а-яё]+){0,2}[\s-]+$",
            prefix,
            re.I,
        ):
            continue
        anchors.append((marker.end(), marker.end(), marker.group().strip(" :—-").casefold() == "должник", False, marker.start()))
    anchors.extend((marker.end(), marker.end(), False, False, marker.start())
                   for marker in IN_RELATION.finditer(text) if "\f" not in marker[0])
    anchors.extend((marker.start("name"), marker.start("name"), False, False, marker.start())
                   for marker in SELF_BANKRUPTCY.finditer(text) if "\f" not in marker[0])
    for marker in _reverse_person_debtor_matches(text):
        if "\f" in marker[0]:
            continue
        prefix = re.split(r"[\n\f.;,]", text[max(0, marker.start() - 100):marker.start()])[-1]
        if _OTHER_PERSON_CONTEXT.search(prefix):
            continue
        role_start = marker.start() + marker[0].casefold().rfind("должник")
        anchors.append((marker.start("name"), marker.end(), True, False, role_start))
    for marker in BANKRUPTCY_PERSON_NAME.finditer(text):
        # "О признании гражданина X" alone can describe a claim whose
        # disposition is elsewhere. Require the bankruptcy predicate.
        if "\f" in marker[0]:
            continue
        if "банкротстве" not in marker.group().casefold() and not re.match(
            r"\s+(?:несостоятельным\s*\(\s*)?банкротом\b",
            text[marker.end():_page_window_end(text, marker.end(), marker.end() + 80)], re.I,
        ):
            continue
        anchors.append((marker.start("name"), marker.end(), False, True, marker.start()))
    if not anchors:
        return _RoleEvidence(warning="Не установлена явная роль должника-физлица")

    named_sections: list[tuple[str, str, tuple[int, ...], str, tuple[int, int] | None, bool]] = []
    for start, search_from, nominative_label, name_only, role_start in sorted(set(anchors)):
        # Literal quoted text can name a person without establishing a current
        # debtor role. Check the role, not quotation inside the person's name.
        if not _outside_quotes(text, role_start):
            continue
        end = search_from if name_only else _personal_section_end(text, search_from, min(len(text), start + 500))
        section, direct, boundary_warning, relative_map = _bounded_personal_continuation(text[start:end])
        if boundary_warning:
            return _RoleEvidence(warning=boundary_warning)
        if NON_PERSON_PREFIX.match(section):
            return _RoleEvidence(warning="Указан должник другого типа")
        without_prefix = PERSON_PREFIX.sub("", section, count=1)
        first = NAME_TRIPLE.match(without_prefix.lstrip(" :—-\t\n"))
        if first is None:
            # Court acts often mention an unnamed "должник/должнику" before a
            # later explicit debtor card. That generic mention cannot defeat
            # the directly labelled identity found later in the same text.
            # A capitalized but damaged identity after a debtor label is
            # different: it may be a second debtor and must block promotion.
            if re.match(WORD, without_prefix.lstrip(" :—-\t\n")):
                return _RoleEvidence(warning="ФИО должника неполное или повреждено")
            continue
        name = " ".join(first.groups())
        if any(stop in name.lower() for stop in ("суд", "кодекс", "закон", "ассоциаци")):
            return _RoleEvidence(warning="Неоднозначное ФИО должника")
        # Another person can appear after the debtor's INN or address without
        # a fresh role label. Do not attach those fields to the first name.
        other_names = {" ".join(match.groups()) for match in NAME_TRIPLE.finditer(without_prefix)}
        if other_names - {name}:
            return _RoleEvidence(warning="Несколько лиц в секции должника")
        direct_name_start = direct.find(first.group(0))
        name_location = (
            (start + direct_name_start, start + direct_name_start + len(first.group(0)))
            if direct_name_start >= 0 else None
        )
        named_sections.append((
            name, section, tuple(start + offset for offset in relative_map), direct, name_location,
            nominative_label and "(" not in first.group(0),
        ))

    if not named_sections:
        return _RoleEvidence(warning="ФИО должника не связано с ролью")
    if len({_role_identity_key(item[0]) for item in named_sections}) != 1:
        return _RoleEvidence(warning="Несколько разных должников")
    selected = next((item for item in named_sections if item[5]), named_sections[0])
    return _RoleEvidence(
        selected[0],
        tuple(item[1] for item in named_sections),
        section_maps=tuple(item[2] for item in named_sections),
        direct_sections=tuple(item[3] for item in named_sections),
        fio_location=next((item[4] for item in named_sections if item[4] is not None), None),
        nominative=selected[5],
    )


@dataclass(frozen=True)
class _ApplicationCandidate:
    name: str
    name_location: tuple[int, int]
    anchor_kind: str = "application"
    inn: str | None = None
    snils: str | None = None
    address: str | None = None
    address_kind: str | None = None
    address_partial: bool = False
    birth_date: str | None = None
    birth_place: str | None = None
    inn_location: tuple[int, int] | None = None
    snils_location: tuple[int, int] | None = None
    address_location: tuple[int, int] | None = None
    birth_date_location: tuple[int, int] | None = None
    birth_place_location: tuple[int, int] | None = None
    role_location: tuple[int, int] | None = None
    normalized_name: tuple[str, str, str] | None = None


_DECISION_HEADING = re.compile(r"(?:^|(?<=\f))[ \t]*р[ \t]*е[ \t]*ш[ \t]*и[ \t]*л[ао]?[ \t]*:[ \t]*\r?$", re.I | re.M)
_DECISION_TITLE = re.compile(
    r"(?:^|(?<=\f))[ \t]*р[ \t]*е[ \t]*ш[ \t]*е[ \t]*н[ \t]*и[ \t]*е[ \t]*\r?$",
    re.I | re.M,
)
_OTHER_ACT_TITLE = re.compile(
    r"(?:^|(?<=\f))[ \t]*(?:(?P<determination>о[ \t]*п[ \t]*р[ \t]*е[ \t]*д[ \t]*е[ \t]*л[ \t]*е[ \t]*н[ \t]*и[ \t]*е)|"
    r"п[ \t]*о[ \t]*с[ \t]*т[ \t]*а[ \t]*н[ \t]*о[ \t]*в[ \t]*л[ \t]*е[ \t]*н[ \t]*и[ \t]*е)[ \t]*\r?$",
    re.I | re.M,
)
_OPERATIVE_NAME = rf"{WORD}(?:[ \t]*\({WORD}\))?[ \t\r\n]+{WORD}[ \t\r\n]+{WORD}"
_REGISTRATION_RESIDENCE_LABEL = r"\bадрес[ \t]+регистрации[ \t]*(?P<adjunct>\([ \t]*проживания[ \t]*\))(?=[ \t]*:)"
_REGISTRATION_RESIDENCE_ADJUNCT = re.compile(_REGISTRATION_RESIDENCE_LABEL, re.I)
_OPERATIVE_BIOGRAPHY = rf"(?:[^()\f«»\"“”]|\([ \t]*(?:ИНН|СНИЛС)[ \t]*\)|{_REGISTRATION_RESIDENCE_LABEL}){{1,500}}"
_OPERATIVE_APPLICATION_RULING = re.compile(
    rf"[ \t\r\n]*(?:\d{{1,2}}[.)][ \t]*)?заявление\s+"
    rf"(?:(?P<person_kind>гражданки|гражданина)\s+)?(?P<name>{_OPERATIVE_NAME})\s+"
    rf"(?:\((?P<bio>{_OPERATIVE_BIOGRAPHY})\)[ \t\r\n]+)?"
    r"о\s+признании\s+(?P<pronoun>его|е[её]|себя)\s+несостоятельн(?P<ending>ым|ой)\s*"
    r"\(\s*банкротом\s*\)\s+(?:признать\s+обоснованным|удовлетворить)[ \t]*\.[ \t]*\r?\n",
    re.I,
)
_OPERATIVE_NUMBERED_APPLICATION = re.compile(
    r"[ \t\r\n]*1\.[ \t]*заявление[ \t]+удовлетворить[ \t]*\.[ \t]*\r?\n(?=[ \t]*2\.[ \t]*признать\b)", re.I,
)
_OPERATIVE_PERSON = re.compile(
    rf"[ \t\r\n]*(?:\d{{1,2}}[.)][ \t]*)?(?P<verb>признать)[ \t\r\n]+"
    rf"(?:гражданин[ау][ \t\r\n]+)?"
    rf"(?P<name>{_OPERATIVE_NAME})"
    rf"[ \t\r\n]*\((?P<bio>{_OPERATIVE_BIOGRAPHY})\)"
    r"[ \t\r\n]+несостоятельн(?P<ending>ым|ой)[ \t\r\n]*"
    r"\([ \t\r\n]*банкротом[ \t\r\n]*\)[ \t]*",
    re.I,
)
_OTHER_OPERATIVE_PERSON = re.compile(r"\bпризнать\b", re.I)
_OPERATIVE_END = re.compile(r"(?:[.;]|\f|\Z|\r?\n(?![ \t\r\n]*и\b))", re.I)
_PROCEDURE_CONTINUATION = re.compile(
    rf"[ \t\r\n]*и\s+ввести\s+(?:в\s+отношении\s+(?:граждан(?:ина|ки)|(?P<pronoun>не[её]|него))\s+)?"
    rf"процедуру\s+реализации\s+имущества"
    rf"(?:\s+(?:гражданина|гражданки)(?:\s+(?P<duration>сроком\s+на\s+"
    r"(?:\d{1,2}|один|два|три|четыре|пять|шесть|семь|восемь|девять|десять|одиннадцать|двенадцать)"
    r"\s+месяц(?:а|ев)?)(?=[ \t]*(?:[,—–-]|до\b|[.;]|\r?\n|\f|\Z))"
    rf"|\s+(?P<name>{_OPERATIVE_NAME}))|\s+сроком\s+до\s+"
    rf"(?P<until_date>\d{{2}}\.\d{{2}}\.\d{{4}}|(?P<until_day>\d{{1,2}})[ \t]+"
    rf"(?P<until_month>{MONTHS_RE})[ \t]+(?P<until_year>\d{{4}})[ \t]+года\b))?[ \t]*",
    re.I,
)
_PROCEDURE_UNTIL = re.compile(r"(?:[,—–-]\s*)?до\b", re.I)
_FOREIGN_BIOGRAPHY_PERSON = re.compile(
    r"\b(?:супруг\w*|родител\w*|опекун\w*|нотариус\w*|доверенност\w*|соза[её]мщик\w*)\b",
    re.I,
)
_OPERATIVE_FIELD_SEPARATOR = r"(?:^|[,;])\s*"
_OPERATIVE_INN_LABEL = re.compile(
    _OPERATIVE_FIELD_SEPARATOR + r"(?:\(\s*ИНН\s*\)|ИНН)\s*[:№–—-]?\s*$", re.I,
)
_OPERATIVE_SNILS_LABEL = re.compile(
    _OPERATIVE_FIELD_SEPARATOR
    + r"(?:страховой\s+номер\s+индивидуального\s+лицевого\s+сч[её]та\s+)?"
    r"(?:\(\s*СНИЛС\s*\)|СНИЛС)\s*[:–—-]?\s*№?\s*$", re.I,
)
_OPERATIVE_BIRTH_DATE_LABEL = re.compile(
    _OPERATIVE_FIELD_SEPARATOR + r"дата\s+рождения\s*[:—-]?\s*$", re.I,
)
_OPERATIVE_RESIDENCE_LABEL = (
    r"(?:фактический\s+адрес(?:\s+проживания)?|адрес\s+проживания|"
    r"место\s+(?:жительства|проживания)|проживающ\w+\s+по\s+адресу)"
)
_OPERATIVE_ADDRESS_LABEL = re.compile(
    _OPERATIVE_FIELD_SEPARATOR
    + rf"(?:(?P<registration>адрес\s+регистрации|зарегистрирован(?:а)?\s+по\s+адресу)|"
    rf"{_OPERATIVE_RESIDENCE_LABEL}|адрес)"
    r"\s*:\s*", re.I,
)
_PARENTHESIZED_PERSONAL_LABEL = re.compile(r"\(([ \t]*(?:ИНН|СНИЛС)[ \t]*)\)", re.I)
_OPERATIVE_ADDRESS_END = re.compile(
    r"\s*(?:[.;]?\s*\Z|[,;]\s*"
    r"(?:(?:страховой\s+номер\s+индивидуального\s+лицевого\s+сч[её]та\s+)?"
    r"(?:\(\s*СНИЛС\s*\)|СНИЛС)|(?:\(\s*ИНН\s*\)|ИНН))"
    r"\s*[:–—-]?\s*№?\s*\d)",
    re.I,
)
_OPERATIVE_POSTAL_PREFIX = re.compile(r"\d{6}\b\s*,?\s*")
_RURAL_PLACE_NAME = r"(?-i:[А-ЯЁ][А-ЯЁа-яё]+(?:[-–][А-ЯЁа-яё]+)*)"
_POSTAL_LOCALITY = rf"{_RURAL_PLACE_NAME}(?:[ \t\r\n]+{_RURAL_PLACE_NAME}){{0,2}}"
_POSTAL_AREA_TYPE = (
    r"(?:область|обл\.?|край|республика|респ\.?|район|р-(?:н|он)\.?|округ|улус|"
    r"с\.?п\.|сельсовет|сельское\s+поселение)"
)
_POSTAL_RURAL_TYPE = r"(?:(?:д\.|дер\.|с\.|х\.)\s*|(?:деревня|с|село|хутор|аул)\s+)"
_OPERATIVE_ADMIN_COMPONENT = re.compile(
    rf"(?:россия|рф|российская\s+федерация|"
    rf"{_POSTAL_LOCALITY}\s+(?:(?:муниципальн\w+|автономн\w+|городск\w+)\s+)?{_POSTAL_AREA_TYPE}|"
    rf"{_POSTAL_AREA_TYPE}\s+{_POSTAL_LOCALITY})",
    re.I,
)
_POSTAL_RURAL_LOCALITY = re.compile(
    rf"{_POSTAL_RURAL_TYPE}{_POSTAL_LOCALITY}", re.I,
)
_POSTAL_CITY_TYPE = r"(?:(?:г\.|гор\.)\s*|город(?:-(?:курорт|герой))?\s+)"
_POSTAL_CITY = re.compile(rf"{_POSTAL_CITY_TYPE}{_POSTAL_LOCALITY}", re.I)
_POSTAL_TERRITORY_TYPE = r"(?:тер\.\s*|территория\s+)(?:СНТ|ТСН|ДНТ|ОНТ|ДНП)\s+(?:ст\.?\s+)?"
_POSTAL_TERRITORY = re.compile(rf"{_POSTAL_TERRITORY_TYPE}{_POSTAL_LOCALITY}", re.I)
_POSTAL_SETTLEMENT = re.compile(
    rf"(?:{_POSTAL_RURAL_TYPE}|{_POSTAL_CITY_TYPE}|{_POSTAL_TERRITORY_TYPE}|(?:пгт\.|р\.?п\.|п\.|пос\.|ст\.)\s*|"
    rf"(?:пгт|рп|пос[её]лок)\s+){_POSTAL_LOCALITY}", re.I,
)
_POSTAL_UNMARKED_LOCALITY = re.compile(_POSTAL_LOCALITY)
_POSTAL_STREET_MARKER = rf"(?:{STREET_RE.pattern}|\b(?:пл\.|площадь\b|пр-д\b|б-р\b))"
_POSTAL_STREET_WORD = rf"(?:{_RURAL_PLACE_NAME}|(?-i:[А-ЯЁ]\.)|\d{{1,4}}(?:-[а-яё]{{1,3}})?)"
_POSTAL_STREET_NAME = rf"(?:(?:им\.|имени)\s+)?{_POSTAL_STREET_WORD}(?:\s+(?:лет\s+)?{_POSTAL_STREET_WORD}){{0,4}}"
_POSTAL_STREET = re.compile(
    rf"(?:{_POSTAL_STREET_MARKER}(?:(?<=\.)\s*|\s+){_POSTAL_STREET_NAME}|"
    rf"{_POSTAL_STREET_NAME}\s+{_POSTAL_STREET_MARKER})", re.I,
)
_OPERATIVE_BUILDING = re.compile(
    rf"(?<![\w.])(?:д(?:\.[ \t]*\.?)?|дом|домовл\.?|домовладение|двлд\.?|вл\.|владение|"
    rf"уч(?:асток|-к\.?)?|общ\.|общежитие)\s*№?\s*{UNIT_NUMBER_PATTERN}", re.I,
)
_OPERATIVE_CORPUS = re.compile(rf"\s*,?\s*(?:к\.|к\s+|кор\.)\s*№?\s*{UNIT_NUMBER_PATTERN}", re.I)
_OPERATIVE_ROOM = re.compile(rf"\s*,\s*комн(?:ата)?\.?\s*№?\s*{UNIT_NUMBER_PATTERN}", re.I)
_DETACHED_HOUSE_LETTER = re.compile(r"[ \t]+[А-ЯЁA-Z](?=[ \t]*(?:[,;]|\Z))")
_OPERATIVE_LITERA = re.compile(r"\s*,?\s*лит(?:ера|\.)\s*[А-ЯЁа-яёA-Za-z](?!\w)", re.I)
_TEMPORARY_REGISTRATION = re.compile(r"адрес\s+регистрации\s+по\s+месту\s+пребывания\s*:", re.I)


def _operative_postal_address(biography: str) -> tuple[str, str, tuple[int, int], bool] | None:
    """Read a single explicitly typed address in the debtor's operative biography."""
    labels = list(_OPERATIVE_ADDRESS_LABEL.finditer(biography))
    if len(labels) != 1:
        return None
    label = labels[0]
    extra = [m for m in _ADDRESS_LABEL.finditer(biography) if not label.start() <= m.start() < label.end()]
    limit = len(biography)
    if extra:
        temporary = extra[0].start()
        if (len(extra) != 1 or not label["registration"] or temporary <= label.end()
                or not _TEMPORARY_REGISTRATION.match(biography, temporary)
                or not biography[:temporary].rstrip().endswith((',', ';'))):
            return None
        limit = len(biography[:temporary].rstrip(' ,;\t\r\n'))
    start = label.end()
    stop = _OPERATIVE_ADDRESS_END.search(biography, start, limit)
    end = stop.start() if stop else limit
    if not 0 < end - start <= 260:
        return None
    raw = biography[start:end]
    house = _OPERATIVE_BUILDING.search(raw)
    prefix = raw[:house.start()].rstrip().removesuffix(",") if house else raw
    prefix = _OPERATIVE_POSTAL_PREFIX.sub("", prefix, count=1)
    parts = [component.strip() for component in prefix.split(",")]
    street = bool(parts and _POSTAL_STREET.fullmatch(parts[-1]))
    if not parts or any(ROLE_BOUNDARY.search(part) or _REVIEW_ADDRESS_STOP.search(part) for part in parts):
        return None
    if street:
        parts.pop()
    if not parts:
        return None
    locality = [part for part in parts if _POSTAL_SETTLEMENT.fullmatch(part)]
    if len(locality) > 1:
        if len(locality) != 2:
            return None
        parent, child = locality
        if not (_POSTAL_CITY.fullmatch(parent) and not _POSTAL_CITY.fullmatch(child)
                or not _POSTAL_TERRITORY.fullmatch(parent) and _POSTAL_TERRITORY.fullmatch(child)):
            return None
    unknown = [part for part in parts if not (
        _OPERATIVE_ADMIN_COMPONENT.fullmatch(part) or _POSTAL_SETTLEMENT.fullmatch(part)
    )]
    unmarked_rural_part = False
    if unknown:
        if len(unknown) != 1 or len(parts) < 2 or unknown[0] != parts[-1]:
            return None
        # Retain a literal unmarked locality before a street, or one name
        # between an explicit rural settlement and a numbered building.
        unmarked_locality = street and not locality and _POSTAL_UNMARKED_LOCALITY.fullmatch(parts[-1])
        unmarked_rural_part = bool(house and not street and _POSTAL_RURAL_LOCALITY.fullmatch(parts[-2])
                                   and re.fullmatch(_RURAL_PLACE_NAME, parts[-1]))
        if not (unmarked_locality or unmarked_rural_part):
            return None
    elif not locality or (house and not street and not (
        _POSTAL_RURAL_LOCALITY.fullmatch(parts[-1]) or _POSTAL_TERRITORY.fullmatch(parts[-1])
    )):
        return None
    kind = "registration" if label["registration"] else "actual"
    address = " ".join(raw.split())
    if not house:
        if street:
            return None
        return address, kind, (start, end), True
    current = house.end()
    if letter := _DETACHED_HOUSE_LETTER.match(raw, current):
        current = letter.end()
    seen_units = set()
    while unit := (_ADDRESS_UNIT.match(raw, current) or _OPERATIVE_CORPUS.match(raw, current)
                   or _OPERATIVE_ROOM.match(raw, current) or _OPERATIVE_LITERA.match(raw, current)):
        if unit.re is _OPERATIVE_CORPUS:
            unit_kind = "ко"  # Same duplicate key as "корпус" and "корп.".
        else:
            unit_kind = unit["label"][:2].casefold() if unit.re is _ADDRESS_UNIT else unit.re
        if unit_kind in seen_units:
            return None
        seen_units.add(unit_kind)
        current = unit.end()
    if raw[current:].strip():
        return None
    return address, kind, (start, end), unmarked_rural_part


def _inside_operative_quote(prefix: str) -> bool:
    if prefix.rfind("«") > prefix.rfind("»") or prefix.rfind("“") > prefix.rfind("”"):
        return True
    if prefix.count('"') % 2 == 0:
        return False
    quote = prefix.rfind('"')
    opening_position = quote == 0 or prefix[quote - 1].isspace() or prefix[quote - 1] in ":("
    return opening_position and not re.match(r"[ \t]*[,.;)]", prefix[quote + 1:])


def _operative_person_candidate(text: str) -> _ApplicationCandidate | None:
    """Read the biography enclosed in a direct bankruptcy disposition."""
    title = _DECISION_TITLE.search(text, 0, 2000)
    if not title or _OTHER_ACT_TITLE.search(text, 0, 2000):
        return None
    headings = _DECISION_HEADING.finditer(text)
    heading = next(headings, None)
    if heading is None or heading.start() < title.end() or next(headings, None) is not None:
        return None
    if _inside_operative_quote(text[:heading.start()]):
        return None
    ruling = _OPERATIVE_APPLICATION_RULING.match(text, heading.end(), heading.end() + 900)
    numbered = _OPERATIVE_NUMBERED_APPLICATION.match(text, heading.end(), heading.end() + 100) if not ruling else None
    clause_start = ruling.end() if ruling else numbered.end() if numbered else heading.end()
    match = _OPERATIVE_PERSON.match(text, clause_start, clause_start + 900)
    if not match or _OTHER_OPERATIVE_PERSON.search(text, match.end()):
        return None
    biography = match.group("bio")
    for section in (biography, ruling['bio'] if ruling else None):
        if section and (ROLE_BOUNDARY.search(section) or _OTHER_OPERATIVE_PERSON.search(section)
                or _FOREIGN_BIOGRAPHY_PERSON.search(section)
                or any(_PATRONYMIC_SHAPE.fullmatch(other.group(3)) for other in NAME_TRIPLE.finditer(section))):
            return None
    name = NAME_TRIPLE.fullmatch(match.group("name"))
    if name is None:
        return None
    normalized_name = morph.person_triplet(name.groups())
    if ruling:
        applicant = NAME_TRIPLE.fullmatch(ruling.group("name"))
        pronoun = ruling["pronoun"].casefold()
        ending = ruling["ending"].casefold()
        if (not normalized_name or not applicant or morph.person_triplet(applicant.groups()) != normalized_name
                or ending != match['ending'].casefold()
                or (pronoun != "себя" and (pronoun == "его") != (ending == "ым"))
                or (ruling['person_kind'] and (ruling['person_kind'].casefold() == 'гражданина') != (ending == 'ым'))):
            return None
    # Treat parentheses around the label as spacing while preserving offsets
    # in the original biography. Only the exact registration-label adjunct is
    # additionally masked; other nested parentheticals remain disallowed.
    attributes = _PARENTHESIZED_PERSONAL_LABEL.sub(r" \1 ", biography)
    attributes = _REGISTRATION_RESIDENCE_ADJUNCT.sub(
        lambda item: item[0].replace(item['adjunct'], ' ' * len(item['adjunct']), 1), attributes,
    )
    fields = detect_person_attributes(attributes, 0, len(attributes))
    bio_start = match.start("bio")

    def absolute(location):
        return (bio_start + location[0], bio_start + location[1]) if location else None

    inn_location = fields.inn_location
    if inn_location and not _OPERATIVE_INN_LABEL.search(biography, 0, inn_location[0]):
        inn_location = None
    snils_location = fields.snils_location
    if snils_location and not _OPERATIVE_SNILS_LABEL.search(biography, 0, snils_location[0]):
        snils_location = None
    if ruling and ruling['bio']:
        prelude = ruling['bio']
        prelude_attributes = _PARENTHESIZED_PERSONAL_LABEL.sub(r" \1 ", prelude)
        prelude_attributes = _REGISTRATION_RESIDENCE_ADJUNCT.sub(
            lambda item: item[0].replace(item['adjunct'], ' ' * len(item['adjunct']), 1), prelude_attributes,
        )
        prelude_attributes = prelude_attributes.replace('–', '-').replace('—', '-')
        # An explicit label supplies the left boundary, including "ИНН:123…".
        identity_inn = re.compile(person_attributes._INN.pattern.removeprefix(r'(?<![\d/:-])(?<!\d\.)'),
                                 person_attributes._INN.flags)
        for attr, label, patterns in (
            ('inn', _OPERATIVE_INN_LABEL, ((identity_inn, 0),)),
            ('snils', _OPERATIVE_SNILS_LABEL,
             ((person_attributes._SNILS, 0), (person_attributes._LABELED_SNILS, 1))),
            ('birth_date', _OPERATIVE_BIRTH_DATE_LABEL, ((person_attributes._BIRTH_DATE, 1),)),
        ):
            identity_label = re.compile(label.pattern.replace(_OPERATIVE_FIELD_SEPARATOR, r'(?:^|[,;\r\n])\s*', 1), label.flags)
            owned = []
            for section in (prelude_attributes, attributes.replace('–', '-').replace('—', '-')):
                values = set()
                for pattern, group in patterns:
                    for candidate in pattern.finditer(section):
                        if not identity_label.search(section, 0, candidate.start(group)):
                            continue
                        raw = candidate[group]
                        if attr == 'birth_date':
                            value = tuple(int(part) for part in re.split(r'[./-]', raw))
                            try:
                                date(*reversed(value))
                            except ValueError:
                                continue
                        else:
                            if not (validators.valid_inn(raw) if attr == 'inn' else validators.valid_snils(raw)):
                                continue
                            value = validators.digits(raw)
                        values.add(value)
                if len(values) > 1:
                    return None
                owned.append(values)
            if all(owned) and owned[0] != owned[1]:
                return None
    if normalized_name is None and "(" in match.group("name") and inn_location and snils_location:
        normalized_name = morph.person_triplet(name.groups(), allow_literal_surname=True)
    if normalized_name is None:
        return None
    if not _OPERATIVE_END.match(text, match.end()):
        procedure = _PROCEDURE_CONTINUATION.match(text, match.end(), match.end() + 240)
        if not procedure or not (
            _OPERATIVE_END.match(text, procedure.end())
            or (procedure["duration"] and _PROCEDURE_UNTIL.match(text, procedure.end()))
        ):
            return None
        if procedure['pronoun'] and ((procedure['pronoun'].casefold() == 'него') != (match['ending'].casefold() == 'ым')):
            return None
        if procedure['until_date']:
            try:
                if procedure['until_day']:
                    date(int(procedure['until_year']), MONTHS[procedure['until_month'].lower()], int(procedure['until_day']))
                else:
                    date(*reversed([int(part) for part in procedure['until_date'].split('.')]))
            except ValueError:
                return None
        if procedure["name"]:
            subject = NAME_TRIPLE.fullmatch(procedure["name"])
            if subject is None:
                return None
            same_spelling = tuple(part.casefold() for part in subject.groups()) == tuple(
                part.casefold() for part in name.groups()
            )
            if not same_spelling and morph.person_triplet(subject.groups()) != normalized_name:
                return None
    address = fields.address
    address_kind = fields.address_kind
    address_partial = fields.address_partial
    address_location = fields.address_location if address_kind else None
    if address_location and not any(
        label.end() == address_location[0] for label in _OPERATIVE_ADDRESS_LABEL.finditer(attributes)
    ):
        address_location = None
    if address_location and not _OPERATIVE_ADDRESS_END.match(biography, address_location[1]):
        address_partial = True
    if "address" not in fields.ambiguous_fields:
        if postal := _operative_postal_address(attributes):
            address, address_kind, address_location, address_partial = postal
        elif address_location and _OPERATIVE_POSTAL_PREFIX.match(biography, address_location[0]):
            address_partial = True
    return _ApplicationCandidate(
        name=" ".join(name.groups()), name_location=match.span("name"),
        anchor_kind="operative_person", role_location=(match.start("verb"), match.end()),
        inn=fields.inn if inn_location else None, inn_location=absolute(inn_location),
        snils=fields.snils if snils_location else None, snils_location=absolute(snils_location),
        address=address if address_location else None,
        address_kind=address_kind if address_location else None,
        address_partial=address_partial, address_location=absolute(address_location),
        birth_date=fields.birth_date, birth_date_location=absolute(fields.birth_date_location),
        birth_place=fields.birth_place, birth_place_location=absolute(fields.birth_place_location),
        normalized_name=normalized_name,
    )


def _application_candidates(text: str) -> tuple[list[_ApplicationCandidate], bool]:
    candidates: list[_ApplicationCandidate] = []
    for anchor_kind, pattern in (
        ("application", APPLICATION_NAME),
        ("insolvency_case", INSOLVENCY_CASE_NAME),
        ("bankruptcy_person", BANKRUPTCY_PERSON_NAME),
        ("reverse_person_debtor", REVERSE_PERSON_DEBTOR_NAME),
    ):
        matches = (
            _reverse_person_debtor_matches(text)
            if pattern is REVERSE_PERSON_DEBTOR_NAME else pattern.finditer(text)
        )
        for match in matches:
            candidate = _application_candidate(text, match, anchor_kind=anchor_kind)
            if candidate is not None:
                candidates.append(candidate)
    pairs = []
    pair_scan_truncated = len(text) > 40_000
    for match in _LABELED_TWO_PART_PERSON.finditer(text, 0, min(len(text), 40_000)):
        if candidate := _application_candidate(text, match, anchor_kind="two_part_person"):
            pairs.append(candidate)
            if len(pairs) > 8:
                pair_scan_truncated = True
                break
    ordered = sorted(candidates + pairs, key=lambda item: item.name_location)
    if candidates:
        return ordered[:8], len(ordered) > 8 or pair_scan_truncated
    morphological, truncated = _morphological_person_candidates(text)
    if morphological or pairs:
        ordered = sorted(morphological + pairs, key=lambda item: item.name_location)
        return ordered[:8], truncated or pair_scan_truncated or len(ordered) > 8
    blocks, block_truncated = _identifier_block_person_candidates(text)
    return blocks, truncated or block_truncated


def _identifier_block_person_candidates(text: str) -> tuple[list[_ApplicationCandidate], bool]:
    """Expose one occurrence's FIO and valid personal ID for employee review.

    This fallback is deliberately unbound to debtor or applicant. It helps when
    OCR damages a surname enough for morphology to reject it, but a nearby
    checksum-valid personal INN and a person/context label remain legible.
    """
    scan_end = min(len(text), 40_000)
    truncated = len(text) > scan_end
    identifiers = []
    for match in re.compile(r"\bИНН[ \t\r\n]*[:№-]?[ \t\r\n]*([\d \t\r\n]{10,17})", re.I).finditer(text, 0, scan_end):
        value = validators.digits(match.group(1))
        if valid_person_inn(value):
            identifiers.append((match, value))
    candidates: list[_ApplicationCandidate] = []
    tokens = list(re.compile(WORD).finditer(text, 0, scan_end))
    triples: dict[tuple[int, int], tuple[str, str, str]] = {}
    boundary_name_spans: set[tuple[int, int]] = set()
    for first, second, third in zip(tokens, tokens[1:], tokens[2:]):
        if (
            "\f" in text[first.end():third.start()]
            or not text[first.end():second.start()].isspace()
            or not text[second.end():third.start()].isspace()
        ):
            continue
        span = (first.start(), third.end())
        boundary_name_spans.add(span)
        if _PATRONYMIC_SHAPE.fullmatch(third.group()):
            triples[span] = (first.group(), second.group(), third.group())
    # Parenthetical former surnames are common in personal data blocks; the
    # plain token pass above intentionally cannot span their punctuation.
    parenthetical = [match for match in NAME_TRIPLE.finditer(text, 0, scan_end) if "\f" not in match[0]]
    for match in parenthetical:
        if _PATRONYMIC_SHAPE.fullmatch(match.group(3)):
            triples.setdefault(match.span(), match.groups())
    # A second name can have a damaged patronymic and still delimit the first
    # person's fields, even though it cannot itself become a candidate.
    name_spans = tuple(boundary_name_spans | {match.span() for match in parenthetical})

    def same_person_block(name_start: int, name_end: int, identifier: re.Match[str]) -> bool:
        between_start = min(name_end, identifier.end())
        between_end = max(name_start, identifier.start())
        if between_start >= between_end:
            return True
        if _personal_section_end(text, between_start, between_end) < between_end:
            return False
        if _OTHER_PERSON_CONTEXT.search(text, between_start, between_end):
            return False
        return not any(
            (other_start, other_end) != (name_start, name_end)
            and between_start <= other_start < other_end <= between_end
            for other_start, other_end in name_spans
        )

    for (name_start, name_end), parts in sorted(triples.items()):
        if any(len(part) > 40 for part in parts):
            continue
        prefix = re.split(r"[\n\f.;,]", text[max(0, name_start - 100):name_start])[-1]
        if _OTHER_PERSON_CONTEXT.search(prefix):
            continue
        adjacent = [
            (match, value) for match, value in identifiers
            if abs(name_start - match.start()) <= 250
            and same_person_block(name_start, name_end, match)
        ]
        if len({value for _match, value in adjacent}) != 1:
            continue
        if not _outside_quotes(text, name_start):
            continue
        nearest, inn = min(adjacent, key=lambda item: abs(name_start - item[0].start()))
        first_position = min(name_start, nearest.start())
        block_start = max(first_position - 250, text.rfind("\f", max(0, first_position - 250), first_position) + 1)
        last_position = max(name_end, nearest.end())
        block_end = _page_window_end(text, last_position, min(len(text), last_position + 250))
        context = text[block_start:block_end]
        if not re.search(r"\b(?:СНИЛС|должник\w*|гражданин\w*)\b|\bо\s+признании\b", context, re.I):
            continue
        end = _personal_section_end(text, name_end, min(scan_end, name_end + 500), after_name=True)
        next_name = NAME_TRIPLE.search(text, name_end, end)
        if next_name:
            end = next_name.start()
        attributes = detect_person_attributes(text, name_end, end)
        candidates.append(_ApplicationCandidate(
            name=" ".join(parts),
            name_location=(name_start, name_end),
            anchor_kind="identifier_block",
            inn=inn,
            snils=attributes.snils,
            address=attributes.address,
            address_kind=attributes.address_kind,
            address_partial=attributes.address_partial,
            birth_date=attributes.birth_date,
            birth_place=attributes.birth_place,
            inn_location=nearest.span(1),
            snils_location=attributes.snils_location,
            address_location=attributes.address_location,
            birth_date_location=attributes.birth_date_location,
            birth_place_location=attributes.birth_place_location,
        ))
        if len(candidates) > 8:
            truncated = True
            break
    return candidates[:8], truncated


def _morphological_person_candidates(text: str) -> tuple[list[_ApplicationCandidate], bool]:
    """Find bounded, review-only persons when exact application wording is absent.

    Morphology validates name roles and agreement; nearby personal evidence or
    an explicit person context is still required. Every occurrence keeps its
    own fields and offsets. The scan and candidate count are bounded for OCR.
    """
    scan_end = min(len(text), 40_000)
    found: list[tuple[int, int, tuple[str, str, str], str]] = []
    truncated = len(text) > scan_end
    tokens = list(re.compile(WORD).finditer(text, 0, scan_end))
    birthplace_spans = [item.span() for item in _BIRTHPLACE_SPAN.finditer(text, 0, scan_end)]
    for first, second, third in zip(tokens, tokens[1:], tokens[2:]):
        if any(start <= first.start() < end for start, end in birthplace_spans):
            continue
        separator_a = text[first.end():second.start()]
        separator_b = text[second.end():third.start()]
        if (not separator_a.isspace() or not separator_b.isspace() or "\f" in separator_a + separator_b
                or "\n\n" in separator_a + separator_b
                or len(separator_a) > 3 or len(separator_b) > 3):
            continue
        parts = (first.group(), second.group(), third.group())
        if any(len(part) > 40 for part in parts):
            continue
        if not (_PATRONYMIC_SHAPE.fullmatch(parts[2]) or _PATRONYMIC_SHAPE.fullmatch(parts[1])):
            continue
        if len(found) >= 64:
            truncated = True
            break
        normalized = morph.person_triplet(parts)
        order = "surname_first"
        if normalized is None:
            normalized = morph.person_triplet((parts[2], parts[0], parts[1]))
            order = "surname_last"
        if normalized is not None:
            found.append((first.start(), third.end(), parts, order))

    candidates: list[_ApplicationCandidate] = []
    for position, (name_start, name_end, parts, order) in enumerate(found):
        if not _outside_quotes(text, name_start):
            continue
        # A preceding occupational role belongs to this occurrence, not to a
        # prior person. Keep the check within the current clause or OCR line.
        prefix = text[max(0, name_start - 100):name_start]
        clause_prefix = re.split(r"[\n\f.;,]", prefix)[-1]
        if _OTHER_PERSON_CONTEXT.search(clause_prefix):
            continue
        if re.search(r"\b(?:место\s+рождения|урожен\w*|суд\w*|город\w*)\s*[:—-]?\s*$", clause_prefix, re.I):
            continue
        end = _personal_section_end(text, name_end, min(len(text), name_end + 500), after_name=True)
        next_occupation = _OTHER_PERSON_CONTEXT.search(text, name_end, end)
        if next_occupation:
            end = min(end, next_occupation.start())
        if position + 1 < len(found):
            end = min(end, found[position + 1][0])
        # A second capitalized triple with an unfamiliar surname may still be
        # another person. Stop before it even if morphology cannot qualify it.
        for possible_next in NAME_TRIPLE.finditer(text, name_end, end):
            if not any(start <= possible_next.start() < stop for start, stop in birthplace_spans):
                end = possible_next.start()
                break
        # The field detector receives only this person's bounded continuation.
        attributes = detect_person_attributes(text, name_end, end)
        has_attribute = any((
            attributes.inn, attributes.snils, attributes.address,
            attributes.birth_date, attributes.birth_place,
        ))
        has_context = bool(_PERSON_CONTEXT.search(clause_prefix))
        if not has_attribute and not has_context:
            continue
        name = " ".join(parts if order == "surname_first" else (parts[2], parts[0], parts[1]))
        candidate = _ApplicationCandidate(
            name=name,
            name_location=(name_start, name_end),
            anchor_kind="morphological",
            inn=attributes.inn,
            snils=attributes.snils,
            address=attributes.address,
            address_kind=attributes.address_kind,
            address_partial=attributes.address_partial,
            birth_date=attributes.birth_date,
            birth_place=attributes.birth_place,
            inn_location=attributes.inn_location,
            snils_location=attributes.snils_location,
            address_location=attributes.address_location,
            birth_date_location=attributes.birth_date_location,
            birth_place_location=attributes.birth_place_location,
        )
        candidates.append(candidate)
        if len(candidates) > 8:
            truncated = True
            break
    return candidates[:8], truncated


def _application_candidate(
    text: str, match: re.Match[str] | None = None, *, anchor_kind: str = "application",
) -> _ApplicationCandidate | None:
    """Find one review-only name and nearby labeled fields in an application.

    Application authorship is not proof of debtor identity. Callers must keep
    these values behind the employee review path; this source is deliberately
    distinct from ROLE_SOURCE, which is the only source accepted for automatic
    internal debtor-card preparation.
    """
    if match is None:
        matches, truncated = _application_candidates(text)
        return matches[0] if not truncated and len(matches) == 1 else None
    if "\f" in match[0]:
        return None
    name_match = NAME_TRIPLE.fullmatch(match.group("name"))
    normalized_pair = None
    if name_match is None and anchor_kind == "two_part_person":
        name_match = NAME_PAIR.fullmatch(match.group("name"))
        normalized_pair = morph.person_pair(name_match.groups()) if name_match else None
        # A soft OCR line break does not close "representative of debtor".
        # Hard clauses, pages and paragraphs still bound the role modifier.
        prefix = re.split(r"[\f.;,]|\n[ \t]*\n", text[max(0, match.start() - 100):match.start()])[-1]
        if (normalized_pair is None or _OTHER_PERSON_CONTEXT.search(prefix)
                or not _outside_quotes(text, match.start())):
            return None
    if name_match is None:
        return None
    raw_name = match.group("name")
    previous_surname = re.search(r"\(([^)]*)\)", raw_name)
    if (
        len(raw_name) > 180
        or any(len(part) > 40 for part in name_match.groups())
        or (previous_surname and len(previous_surname.group(1)) > 40)
    ):
        return None
    name = " ".join(name_match.groups())
    end = _personal_section_end(text, match.end(), min(len(text), match.end() + 500), after_name=True)
    if anchor_kind == "two_part_person":
        # Quoted narrative cannot establish this literal pair's attributes.
        if quotation := _QUOTE_MARK.search(text, match.end(), end):
            end = quotation.start()
    section = text[match.end():end]
    narrative = re.search(
        r"(?:[.,;]\s+|^)(?=(?:суд\w*\s+установ|судья\b|установил\b|"
        r"в\s+соответствии\b|руководствуясь\b|судебн\w*\s+заседан|"
        r"рассмотрев\b|позднее\b))",
        section,
        re.I,
    )
    if narrative:
        section = section[:narrative.start()]
    # A labeled birthplace may look like three capitalized name tokens.
    # Keep its bounded comma-delimited span; another person outside it still
    # closes the candidate before any subsequent identifiers can be attached.
    birthplace = re.search(r"\bместо\s+рождения\s*[:—-]?\s*[^,;]{2,100}", section, re.I)
    next_name = next((
        item for item in NAME_TRIPLE.finditer(section)
        if not (birthplace and birthplace.start() <= item.start() and item.end() <= birthplace.end())
    ), None)
    if next_name:
        section = section[:next_name.start()]

    inn_matches = {
        validators.digits(item.group(1)): item
        for item in re.finditer(r"ИНН[:\s]*([\d\s]{10,17})", section)
    }
    valid_inns = [value for value in inn_matches if valid_person_inn(value)]
    # Keep one unique 12-digit reading for the editable review form even when
    # its checksum is damaged. The form marks it uncertain; authoritative
    # extraction and automatic card preparation still require validation.
    inn = (
        valid_inns[0] if len(valid_inns) == 1
        else next(iter(inn_matches))
        if len(inn_matches) == 1 and len(next(iter(inn_matches))) == 12
        else None
    )
    inn_match = inn_matches.get(inn) if inn else None

    snils_matches = {
        validators.digits(item.group(1)): item
        for item in re.finditer(r"СНИЛС[:\s]*([\d\s\-]{11,20})", section)
    }
    valid_snils = [value for value in snils_matches if validators.valid_snils(value)]
    # As with INN, preserve a unique checksum-damaged reading as a review-only
    # candidate; do not promote it to the authoritative card fields here.
    snils_digits = (
        valid_snils[0] if len(valid_snils) == 1
        else next(iter(snils_matches))
        if len(snils_matches) == 1 and len(next(iter(snils_matches))) == 11
        else None
    )
    snils_match = snils_matches.get(snils_digits) if snils_digits else None
    snils = validators.format_snils(snils_digits) if snils_digits else None

    address, address_kind, address_warning = _scoped_address(section)
    if address_warning and not address:
        address, address_kind, fallback_span = _review_address_fragment(section)
    else:
        fallback_span = None
    address_match = None
    if address:
        address_match = _scoped_address_match(section, address)
        if address_match:
            completed = _parenthetical_wrapped_room(section, address, address_match)
            if completed:
                address = completed
                address_warning = None
                address_match = _scoped_address_match(section, address)

    birth_date_match = re.search(
        r"(?:\bдата\s+рождения\s*[:—-]?\s*(?P<label_date>\d{2}[./-]\d{2}[./-]\d{4})"
        r"|\(\s*(?P<birthmark_date>\d{2}[./-]\d{2}[./-]\d{4})\s*[гg]\s*\.?\s*[рp](?![а-яёa-z])\s*\.?\s*\)?)",
        section,
        re.I,
    )
    birth_place_match = re.search(
        r"\bместо\s+рождения\s*[:—-]?\s*([^,;\n]{2,100})",
        section,
        re.I,
    )

    return _ApplicationCandidate(
        name=name,
        name_location=(match.start("name"), match.end("name")),
        anchor_kind=anchor_kind,
        # A two-part source establishes neither a patronymic nor a safe
        # nominative transformation. Keep both literal components for review.
        normalized_name=normalized_pair,
        inn=inn,
        snils=snils,
        address=address,
        address_kind=address_kind,
        address_partial=fallback_span is not None or bool(address_warning),
        birth_date=(birth_date_match.group("label_date") or birth_date_match.group("birthmark_date")) if birth_date_match else None,
        birth_place=birth_place_match.group(1).strip() if birth_place_match else None,
        inn_location=(match.end() + inn_match.start(1), match.end() + inn_match.end(1)) if inn_match else None,
        snils_location=(match.end() + snils_match.start(1), match.end() + snils_match.end(1)) if snils_match else None,
        address_location=(match.end() + address_match.start(), match.end() + address_match.end()) if address_match else (
            (match.end() + fallback_span[0], match.end() + fallback_span[1]) if fallback_span else None
        ),
        birth_date_location=(
            match.end() + birth_date_match.start("label_date" if birth_date_match.group("label_date") else "birthmark_date"),
            match.end() + birth_date_match.end("label_date" if birth_date_match.group("label_date") else "birthmark_date"),
        ) if birth_date_match else None,
        birth_place_location=(match.end() + birth_place_match.start(1), match.end() + birth_place_match.end(1)) if birth_place_match else None,
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

# One evidenced case-family grammar for selection, source spans and conflicts.
CASE_NUMBER_TOKEN = r"(?:[АA]\s?\d{1,2}|СИП)\s?[-–—]\s?\d{1,7}\s?/\s?\d{4}"
# Preserve the existing compact-only unlabelled fallback; labelled occurrences
# and the all-occurrence integrity scan retain optional single whitespace.
CASE_NUMBER_COMPACT_TOKEN = CASE_NUMBER_TOKEN.replace(r"\s?", "")


def find_case_number(text: str) -> Optional[str]:
    patterns = (
        rf"[Дд]ело\s*[№N]?\s*({CASE_NUMBER_TOKEN})\b",
        rf"[№N]\s*({CASE_NUMBER_TOKEN})\b",
        rf"\b({CASE_NUMBER_COMPACT_TOKEN})\b",
    )
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return validators.normalize_case_number(m.group(1))
    return None


def find_doc_date(text: str) -> Optional[str]:
    chosen = _chosen_doc_date(text)
    return chosen[0] if chosen else None


def _chosen_doc_date(text: str) -> tuple[str, int, int] | None:
    full_text = _decision_full_text_date(text)
    if full_text:
        return full_text
    candidates = _date_candidates(text)
    if not candidates:
        return None
    heading = re.search(r"\b(?:ОПРЕДЕЛЕНИЕ|РЕШЕНИЕ|ПОСТАНОВЛЕНИЕ)\b", text[:600], re.I)
    if heading is not None:
        boundary = ROLE_BOUNDARY.search(text, heading.end())
        end = min(heading.end() + 250, boundary.start() if boundary else len(text))
        anchored = [item for item in candidates if heading.end() <= item[1] < end]
        if anchored:
            # Only the bounded act caption displaces the old fallback. A
            # numeric address/contact date before the heading is not evidence.
            return anchored[0]
    # Keep the existing uncertain suggestion outside an established caption;
    # metadata review still holds ambiguous/reference/participant dates.
    return min(candidates, key=lambda item: (
        not bool(re.search(r"[а-яё]", text[item[1]:item[2]])), item[1],
    ))


def _case_candidates(text: str) -> set[str]:
    return {value for value, _start, _end in _case_candidate_locations(text)}


def _case_candidate_locations(text: str) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        (validators.normalize_case_number(match.group(0)), match.start(), match.end())
        for match in re.finditer(
            rf"\b{CASE_NUMBER_TOKEN}\b", text
        )
    )


def _date_candidates(text: str) -> list[tuple[str, int, int]]:
    header = _header(text)
    found: list[tuple[str, int, int]] = []
    for match in re.finditer(rf"(\d{{1,2}})\s+({MONTHS_RE})\s+(\d{{4}})", header):
        found.append((f"{int(match.group(1)):02d}.{MONTHS[match.group(2)]:02d}.{match.group(3)}", match.start(), match.end()))
    for match in re.finditer(r"\b(\d{2})[.\-/](\d{2})[.\-/](\d{4})\b", header):
        found.append((f"{match.group(1)}.{match.group(2)}.{match.group(3)}", match.start(), match.end()))
    return sorted(found, key=lambda item: item[1])


_CAPTION_DATE = rf"(?:\d{{2}}[./-]\d{{2}}[./-]\d{{4}}|\d{{1,2}}[ \t]+(?:{MONTHS_RE})[ \t]+\d{{4}})"
_CAPTION_DATE_END = r"(?:[ \t]+(?:г\.?|года))?[ \t]*\.?[ \t]*\r?\n"
_DECISION_DATE_PAIR = re.compile(
    r"(?:[ \t]*\r?\n)*"
    rf"[ \t]*резолютивная[ \t]+часть[ \t]+решения[ \t]+(?:оглашена|объявлена)[ \t]+"
    rf"(?P<announced>{_CAPTION_DATE}){_CAPTION_DATE_END}"
    r"(?:[ \t]*\r?\n)*"
    r"[ \t]*(?:полный[ \t]+текст[ \t]+решения[ \t]+изготовлен|"
    r"решение[ \t]+изготовлено[ \t]+в[ \t]+полном[ \t]+объ[её]ме)[ \t]+"
    rf"(?P<full>{_CAPTION_DATE}){_CAPTION_DATE_END}", re.I,
)
_DECISION_CASE_LINE = re.compile(
    rf"(?im)^[ \t]*(?:(?P<column_quote>«)?(?P<column_day>\d{{1,2}})(?(column_quote)»)"
    rf"[ \t]+(?P<column_month>{MONTHS_RE})[ \t]+(?P<column_year>\d{{4}})[ \t]+(?:года|г\.?)[ \t]+)?"
    rf"дело[ \t]+[№N][ \t]*(?P<number>{CASE_NUMBER_TOKEN})[ \t]*\r?\n",
)
_CAPTION_ACT_NAME = r"(?:решени[ея]|определени[ея]|постановлени[ея]|судебн[а-яё]*[ \t]+акт[а-яё]*)"
_REFERENCED_DECISION_TITLE = re.compile(
    rf"\b(?:(?:ранее|предыдущ[а-яё]+|обжалуем[а-яё]+|цитир[а-яё]+)\b"
    rf"[^\r\n\f:]{{0,120}}\b{_CAPTION_ACT_NAME}"
    rf"|(?:текст|цитата|выдержка|выписка)[ \t]+(?:из[ \t]+)?{_CAPTION_ACT_NAME}"
    r"|суд[ \t]+(?:цитирует|приводит)"
    r"|(?:решением|определением|постановлением)[ \t]+(?:арбитражного[ \t]+)?суда"
    r"[^\r\n\f:]{0,120}\b(?:постановлено|решено|определено))"
    r"[^\r\n\f:]{0,120}:[ \t]*\r?\n(?:[ \t]*\r?\n)*"
    r"(?:[ \t]*арбитражный[ \t]+суд[^\r\n\f]{0,160}\r?\n)?[ \t\r\n]*\Z",
    re.I,
)


def _decision_full_text_date(text: str, expected_case: str | None = None) -> tuple[str, int, int] | None:
    """Read exact decision-caption statements across formatting-only blanks."""
    title = _DECISION_TITLE.search(text, 0, 650)
    if not title or _OTHER_ACT_TITLE.search(text, 0, 650) or not _outside_quotes(text, title.start()):
        return None
    # A colon-ended reference introduction scopes the following reproduced
    # caption, even when OCR has omitted its surrounding quotation marks.
    prefix_start = max(text.rfind("\f", 0, title.start()) + 1, title.start() - 270)
    if _REFERENCED_DECISION_TITLE.search(text[prefix_start:title.start()]):
        return None
    caption_limit = _page_window_end(text, title.end(), min(len(text), title.end() + 650))
    case = _DECISION_CASE_LINE.search(text, title.end(), min(title.end() + 250, caption_limit))
    if not case or (
        expected_case and validators.normalize_case_number(case['number']) != validators.normalize_case_number(expected_case)
    ):
        return None
    hearing = re.search(r"\bрассмотрев\b", text[case.end():caption_limit], re.I)
    if not hearing or ROLE_BOUNDARY.search(text, title.end(), case.start()):
        return None
    caption_end = case.end() + hearing.start()
    pair = _DECISION_DATE_PAIR.match(text, case.end(), caption_end)
    if not pair or len(_case_candidates(text[title.end():caption_end])) != 1:
        return None
    dates = _date_candidates(text)
    selected = [next((item for item in dates if item[1:] == pair.span(name)), None)
                for name in ('announced', 'full')]
    allowed_spans = {pair.span('announced'), pair.span('full')}
    if case['column_day']:
        # A dated case row may repeat the full-text date in a separate span.
        # Its value is checked against the declared full-text date below.
        allowed_spans.add((case.start('column_day'), case.end('column_year')))
    if any(item is None for item in selected) or any(
        title.end() <= start < caption_end and (start, end) not in allowed_spans
        for _value, start, end in dates
    ):
        return None
    try:
        parsed = [date(*reversed([int(part) for part in item[0].split('.')])) for item in selected]
        if case['column_day'] and date(int(case['column_year']), MONTHS[case['column_month'].lower()],
                                       int(case['column_day'])) != parsed[1]:
            return None
    except ValueError:
        return None
    return selected[1] if parsed[0] <= parsed[1] else None


def _metadata_warnings(card: DebtorCard, variants: list[str], *, date_variants: list[str] | None = None) -> None:
    """Flag reference collisions and dates without an act heading for review."""
    cases = [_case_candidates(text) for text in variants]
    if card.case_number.value and (
        any(len(values) != 1 for values in cases)
        or any(values != {card.case_number.value} for values in cases)
    ):
        card.case_number.warning = "Номер дела расходится между страницами или вариантами OCR"

    date_variants = date_variants if date_variants is not None else variants
    dates = [_date_candidates(text) for text in date_variants]
    chosen = card.doc_date.value
    if not chosen:
        return
    try:
        day, month, year = (int(part) for part in chosen.split("."))
        date(year, month, day)
    except ValueError:
        card.doc_date.warning = "Недопустимая дата документа"
        return
    for text, candidates in zip(date_variants, dates):
        full_text = _decision_full_text_date(text)
        if full_text and full_text[0] == chosen:
            continue
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


def valid_person_inn(value: str) -> bool:
    return len(value) == 12 and validators.valid_inn(value)


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


def _court_field(variants: list[str]) -> Field:
    """Select the same literal court and use its occurrences for source and consistency."""
    candidates = [_court_candidates(text) for text in variants]
    field = _vote([items[0][0] if items else "" for items in candidates], lambda name: name)
    if not field.value:
        return field
    expected = {field.value.casefold()}
    field.warning = (
        None if all({name.casefold() for name, _, _ in items} == expected for items in candidates)
        else "Название суда расходится между страницами или вариантами OCR"
    )
    for variant_index, items in enumerate(candidates):
        location = next(
            (candidate for candidate in items if candidate[0] == field.value),
            None,
        )
        if location is not None:
            _name, start, end = location
            _set_field_location(field, variants[variant_index], variant_index, start, end)
            break
    return field


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
        _set_field_location(field, text, variant_index, start, end)
        return


def _set_field_location(field: Field, text: str, variant_index: int, start: int, end: int) -> None:
    field.source_variant = variant_index
    field.source_page = _source_page_at(text, start)
    field.source_start = start
    field.source_end = end
    field.source_excerpt = text[max(0, start - 50):min(len(text), end + 50)]


def _source_page_at(text: str, offset: int) -> int | None:
    """Return a one-based page only when OCR retained explicit page breaks."""
    return text.count("\f", 0, offset) + 1 if "\f" in text else None


def _attach_role_name_source(
    fields: tuple[Field, ...], variants: list[str], evidence: list[_RoleEvidence],
) -> None:
    selected_raw = fields[0].value
    if not selected_raw:
        return
    for variant_index, item in enumerate(evidence):
        if item.fio_location is None:
            continue
        text = variants[variant_index]
        start, end = item.fio_location
        if text[start:end] != selected_raw:
            continue
        for field in fields:
            if field.value:
                _set_field_location(field, text, variant_index, start, end)
        return


def _attach_role_identifier_source(
    field: Field,
    variants: list[str],
    evidence: list[_RoleEvidence],
    pattern: re.Pattern[str],
    normalize: Callable[[str], str],
) -> None:
    if not field.value:
        return
    expected = normalize(field.value)
    for variant_index, item in enumerate(evidence):
        for section, source_map in zip(item.sections, item.section_maps):
            for match in pattern.finditer(section):
                raw_value = match.group(1)
                trimmed_value = raw_value.strip()
                if normalize(trimmed_value) == expected:
                    leading = len(raw_value) - len(raw_value.lstrip())
                    start_index = match.start(1) + leading
                    end_index = start_index + len(trimmed_value)
                    _set_field_location(
                        field,
                        variants[variant_index],
                        variant_index,
                        source_map[start_index],
                        source_map[end_index - 1] + 1,
                    )
                    return


def _attach_role_text_source(
    field: Field,
    variants: list[str],
    evidence: list[_RoleEvidence],
) -> None:
    """Locate normalized free text only inside direct, role-bound OCR sections."""
    if not field.value:
        return
    for variant_index, item in enumerate(evidence):
        for section, source_map in zip(item.sections, item.section_maps):
            match = _scoped_address_match(section, field.value)
            if match:
                start = source_map[match.start()]
                end = source_map[match.end() - 1] + 1
                _set_field_location(
                    field,
                    variants[variant_index],
                    variant_index,
                    start,
                    end,
                )
                return


_DOC_TYPE_CAPTION = re.compile(
    r"(?:^|(?<=\f))[ \t]*(?P<title>"
    + "|".join(r"[ \t]*".join(marker) for marker in DOC_TYPES)
    + r")[ \t]*\r?(?=$|\f)",
    re.I | re.M,
)


def _chosen_doc_type(text: str) -> tuple[str, int, int] | None:
    """Prefer an exact caption on the first nonempty physical page."""
    limit = min(len(text), 1500)
    page_start = 0
    for page in text[:limit].split("\f"):
        if page.strip():
            break
        page_start += len(page) + 1
    page_end = _page_window_end(text, page_start, limit)
    for match in _DOC_TYPE_CAPTION.finditer(text, page_start, page_end):
        # An artificial 1500-character boundary cannot end a real title line.
        if (match.end() == page_end < len(text)
                and not text.startswith(("\n", "\r\n", "\f"), page_end)):
            continue
        if not _outside_quotes(text, match.start()):
            continue
        prefix_start = max(page_start, match.start() - 270)
        if _REFERENCED_DECISION_TITLE.search(text[prefix_start:match.start()]):
            continue
        marker = re.sub(r"[ \t]", "", match["title"]).upper()
        return DOC_TYPES[marker], *match.span("title")

    # Retain the old uncertain earliest-word suggestion when no caption is
    # qualified. Body mentions and quoted references do not gain caption rank.
    head = text[:1500].upper()
    matches = [
        (position, name, len(marker))
        for marker, name in DOC_TYPES.items()
        if (position := head.find(marker)) >= 0
    ]
    if not matches:
        return None
    position, name, length = min(matches)
    return name, position, position + length


def _locate_doc_type(text: str, expected: str) -> tuple[int, int] | None:
    chosen = _chosen_doc_type(text)
    return (chosen[1], chosen[2]) if chosen and chosen[0] == expected else None


def _locate_doc_date(text: str, expected: str) -> tuple[int, int] | None:
    chosen = _chosen_doc_date(text)
    return (chosen[1], chosen[2]) if chosen and chosen[0] == expected else None


def _locate_case_number(text: str, expected: str) -> tuple[int, int] | None:
    return next(
        ((start, end) for value, start, end in _case_candidate_locations(text) if value == expected),
        None,
    )


def find_doc_type(text: str) -> Optional[str]:
    chosen = _chosen_doc_type(text)
    return chosen[0] if chosen else None


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
    r"(адрес(?:\s+проживания)?|проживающ\w+\s+по\s+адресу)\s*:\s*)",
    re.I,
)
_REVIEW_ADDRESS_STOP = re.compile(
    r"\b(?:место\s+рождения|дата\s+рождения|инн|снилс|должник\w*|заявител\w*)\b",
    re.I,
)
_COMPETING_ADDRESS_LABEL = re.compile(
    r"\b(?:место\s+(?:проживания|жительства)|адрес\s+для\s+корреспонденции|"
    r"почтовый\s+адрес|фактическ\w*\s+адрес|фактически\s+прожива\w*)\s*[:—-]",
    re.I,
)
_ADDRESS_CONTACT_FIELD = re.compile(
    r"\b(?:телефон|тел\.)[ \t]*[:—-][ \t]*(?:\+[ \t]*)?\d", re.I,
)
_ADDRESS_HOUSE = re.compile(r"\b(?:д\.?|дом|домовладение)\s*№?\s*\d+[а-яёa-z]?(?:[/\-]\d+[а-яёa-z]?)?\b", re.I)
_ADDRESS_UNIT_MARKER = (
    r"(?:корп(?:ус)?\.?|стр(?:оение)?\.?|кв(?:артира)?\.?(?:[ \t]*:)?|пом(?:ещение)?\.?|офис)"
)
_ADDRESS_UNIT = re.compile(
    rf"\s*,?\s*(?P<label>{_ADDRESS_UNIT_MARKER})\s*№?\s*{UNIT_NUMBER_PATTERN}",
    re.I,
)
_ADDRESS_PENDING_UNIT = re.compile(rf"\s*,?\s*{_ADDRESS_UNIT_MARKER}\s*№?\s*$", re.I)
_ADDRESS_PENDING_APARTMENT = re.compile(r"\bкв(?:артира)?\.?(?:[ \t]*:)?\s*№?\s*$", re.I)
_PHONE_FIELD = re.compile(r"(?:телефон|тел\.)[ \t]*[:—-]?[ \t]*(?:\+[ \t]*)?\d", re.I)
_ADDRESS_UNIT_START = re.compile(rf"\s*,?\s*{_ADDRESS_UNIT_MARKER}(?![а-яёa-z])", re.I)
_ADDRESS_WRAPPED_NUMBER = re.compile(UNIT_NUMBER_PATTERN, re.I)
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
_PARENTHETICAL_WRAPPED_ROOM = re.compile(
    r"[ \t]*,?[ \t]*(?P<label>кв(?:артира)?\.?(?:[ \t]*:)?)[ \t]*\r?\n"
    rf"[ \t]*(?P<number>{UNIT_NUMBER_PATTERN})",
    re.I,
)


def _scoped_address_match(section: str, address: str) -> re.Match[str] | None:
    """Bind the value to its selected label, never an earlier equal birthplace."""
    labels = list(_ADDRESS_LABEL.finditer(section))
    if len(labels) != 1:
        return None
    start = labels[0].end()
    pattern = re.compile(r"\s+".join(re.escape(part) for part in address.split()), re.I)
    # Normal and partial suggestions trim different decoration characters.
    # Search only that leading decoration, never later substantive text.
    while start < len(section):
        match = pattern.match(section, start)
        if match:
            return match
        if section[start] not in " \t\r\n,.;:)(—-":
            break
        start += 1
    return None


def _parenthetical_wrapped_room(
    section: str, address: str, address_match: re.Match[str],
) -> str | None:
    """Complete a review-only room marker wrapped before a biography's closing parenthesis."""
    house = _ADDRESS_HOUSE.search(address)
    if house is None or address[house.end():].strip(" ,."):
        return None
    tail = section[address_match.end():]
    unit = _PARENTHETICAL_WRAPPED_ROOM.match(tail)
    if unit is None:
        return None
    close_index = address_match.end() + unit.end("number")
    from .address import closes_personal_data_parenthesis
    if not closes_personal_data_parenthesis(section, close_index):
        return None
    return _tidy_address(section[address_match.start():close_index])


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
    if _COMPETING_ADDRESS_LABEL.search(section, label.end()):
        return None, None, "Несколько адресов в секции должника; проверьте вручную"
    kind = "registration" if label.group(1) else "actual"
    tail = section[label.end():]
    # A phone field cannot bridge a locality-only proposal to a later house.
    # Keep the existing wrapped-unit termination checks after an owned house.
    contact = _ADDRESS_CONTACT_FIELD.search(tail)
    house = _ADDRESS_HOUSE.search(tail)
    if contact and (house is None or contact.start() < house.start()):
        tail = tail[:contact.start()]
    lines = tail.splitlines()
    if not lines:
        return None, None, "Граница адреса должника не установлена"
    chunks = [lines[0].strip()]
    incomplete_unit = False
    for index, line in enumerate(lines[1:4], 1):
        line = line.strip()
        if not line:
            break
        wrapped_street = _ADDRESS_WRAPPED_STREET.match(line)
        if _ADDRESS_HOUSE.search(" ".join(chunks)):
            # An isolated number can complete a wrapped unit only at the end
            # of the bounded section or immediately before another field.
            # Numbered prose ("1. Обязать...") never meets this shape.
            if _ADDRESS_PENDING_UNIT.search(chunks[-1]) and _ADDRESS_WRAPPED_NUMBER.fullmatch(line):
                following = "\n".join(lines[index + 1:]).strip()
                if (not following or _REVIEW_ADDRESS_STOP.match(following)
                        or (_ADDRESS_PENDING_APARTMENT.search(chunks[-1]) and _PHONE_FIELD.match(following))):
                    chunks.append(line)
                    continue
            if not _ADDRESS_UNIT.match(line):
                if _ADDRESS_PENDING_UNIT.fullmatch(line) and index < 3:
                    next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
                    following = "\n".join(lines[index + 2:]).strip()
                    if _ADDRESS_WRAPPED_NUMBER.fullmatch(next_line) and (
                        not following or _REVIEW_ADDRESS_STOP.match(following)
                        or (_ADDRESS_PENDING_APARTMENT.search(line) and _PHONE_FIELD.match(following))
                    ):
                        chunks.append(line)
                        continue
                if _ADDRESS_UNIT_START.match(line):
                    incomplete_unit = True
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
    if incomplete_unit or _ADDRESS_UNIT_START.match(raw[end:]):
        return address, kind, "Номер помещения или корпуса в адресе не установлен"
    return address, kind, None


def _review_address_fragment(section: str) -> tuple[str | None, str | None, tuple[int, int] | None]:
    """Return a short labeled address fragment for employee review only."""
    labels = list(_ADDRESS_LABEL.finditer(section))
    if len(labels) != 1:
        return None, None, None
    label = labels[0]
    if _COMPETING_ADDRESS_LABEL.search(section, label.end()):
        return None, None, None
    tail = section[label.end():]
    stop = _REVIEW_ADDRESS_STOP.search(tail)
    end = stop.start() if stop else len(tail)
    if contact := _ADDRESS_CONTACT_FIELD.search(tail, 0, end):
        end = contact.start()
    # A sentence boundary can also close an address fragment. Keep the
    # fragment bounded and never use this path for role-bound card fields.
    punctuation = re.search(r"[;\n]", tail[:end])
    if punctuation:
        end = min(end, punctuation.start())
    end = min(end, 250)
    raw = tail[:end].strip(" \t,;:—-")
    raw = re.sub(r"\s+", " ", raw)
    if len(raw) < 3:
        return None, None, None
    left = len(tail[:end]) - len(tail[:end].lstrip(" \t,;:—-"))
    return raw, "registration" if label.group(1) else "actual", (label.end() + left, label.end() + left + len(raw))


_PRACTITIONER_TYPE = r"(?i:(?:финансовым|конкурсным|временным|внешним|арбитражным)[ \t\r\n]+управляющим)"
_PRACTITIONER_NAME = rf"{WORD}[ \t\r\n]+{WORD}[ \t\r\n]+{WORD}"
_PRACTITIONER_ID_BLOCK = re.compile(
    rf"(?im:(?:^|(?<=\f))[ \t]*(?:\d{{1,2}}[.)][ \t]*)?утвердить[ \t\r\n]+)(?:"
    rf"{_PRACTITIONER_TYPE}[ \t\r\n]+{_PRACTITIONER_NAME}[ \t\r\n]*\((?P<after_role>[^()\f]{{1,320}})\)"
    rf"|{_PRACTITIONER_NAME}[ \t\r\n]*\((?P<before_role>[^()\f]{{1,320}})\)[ \t\r\n]+{_PRACTITIONER_TYPE}\b)",
)
_PRACTITIONER_ID_WARNING = "Принадлежность реквизита противоречива: он указан у назначенного управляющего"


def _practitioner_identifiers(variants: list[str]) -> dict[str, set[str]]:
    """An explicit named appointment can contradict a debtor identifier elsewhere in the act."""
    known = {"inn": set(), "snils": set()}
    for reading in variants:
        for match in _PRACTITIONER_ID_BLOCK.finditer(reading):
            if _inside_operative_quote(reading[:match.start()]):
                continue
            bio = (match["after_role"] or match["before_role"]).upper()
            for key, finder in (("inn", _section_inn), ("snils", _section_snils)):
                value, warning = finder(bio)
                if value and not warning:
                    known[key].add(validators.digits(value))
    return known


def _role_field(
    evidence: list[_RoleEvidence],
    finder: Callable[[str], tuple[str | None, str | None]],
    *,
    recoverable_warnings: tuple[str, ...] = (),
) -> Field:
    """Vote only within the single agreed debtor identity, rejecting contradictions."""
    per_variant: list[str | None] = []
    invalid_warnings: list[str] = []
    for item in evidence:
        values: set[str] = set()
        for section in item.sections:
            value, warning = finder(section)
            if warning:
                if warning not in recoverable_warnings:
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


def _collect_application_candidate(
    card: DebtorCard, variants: list[str], *, suppress_morphological: bool = True,
    practitioner_ids: dict[str, set[str]],
) -> bool:
    # Only dedupe an identical anchored occurrence at the same offsets in
    # another OCR variant. Never merge field values across separate anchors.
    occurrence_map: dict[tuple, tuple[int, _ApplicationCandidate, int]] = {}
    if len(variants) > 4:
        card.review_suggestions_complete = False
    # The original-photo pass is the fifth variant for image OCR. Keep its
    # candidates visible for employee review. More than four variants still
    # marks the bounded candidate scan incomplete; every suggestion remains
    # uncertain and outside authoritative card fields.
    for index, text in enumerate(variants[:5]):
        variant_candidates, truncated = _application_candidates(text)
        if truncated:
            card.review_suggestions_complete = False
        explicit_role_person = bool(_role_evidence(text).fio) if any(
            item.anchor_kind == "morphological" for item in variant_candidates
        ) else False
        for candidate in variant_candidates:
            if candidate.anchor_kind == "morphological" and explicit_role_person and suppress_morphological:
                continue
            signature = (
                candidate.anchor_kind,
                candidate.name_location,
                candidate.name,
                candidate.inn,
                candidate.snils,
                candidate.address,
                candidate.address_kind,
                candidate.address_partial,
                candidate.birth_date,
                candidate.birth_place,
                candidate.inn_location,
                candidate.snils_location,
                candidate.address_location,
                candidate.birth_date_location,
                candidate.birth_place_location,
            )
            if signature in occurrence_map:
                prior_index, prior_candidate, count = occurrence_map[signature]
                occurrence_map[signature] = (prior_index, prior_candidate, count + 1)
            else:
                occurrence_map[signature] = (index, candidate, 1)
    if not occurrence_map:
        return False

    if len(occurrence_map) > 8:
        card.review_suggestions_complete = False
    for source_index, source_candidate, confirmations in list(occurrence_map.values())[:8]:
        confidence = round(0.35 + 0.4 * (confirmations / min(len(variants), 5)), 2)
        card.review_suggestions.append(_person_review_suggestion(
            variants, source_index, source_candidate, confidence, practitioner_ids=practitioner_ids,
        ))
    return True


def _person_review_suggestion(
    variants: list[str], source_index: int, source_candidate: _ApplicationCandidate, confidence: float,
    *, practitioner_ids: dict[str, set[str]],
) -> dict:
    suggestion_fields: dict[str, dict] = {}

    def add(attr: str, value: str | None, index: int, location: tuple[int, int] | None) -> None:
        if not value or location is None:
            return
        source = variants[index]
        start, end = location
        suggestion_fields[attr] = {
            "value": value,
            "confidence": confidence,
            "source_variant": index,
            "source_start": start,
            "source_end": end,
            "source_excerpt": source[max(0, start - 45):min(len(source), end + 45)][:180],
        }

    add("fio", source_candidate.name, source_index, source_candidate.name_location)
    normalized_name = source_candidate.normalized_name or (
        morph.person_triplet(tuple(source_candidate.name.split()))
        if source_candidate.anchor_kind == "morphological" else None
    ) or morph.normalize_fio(source_candidate.name)
    for attr, value in zip(
        ("last_name", "first_name", "middle_name"),
        normalized_name,
    ):
        add(attr, value, source_index, source_candidate.name_location)

    for attr, location_attr in (
        ("inn", "inn_location"),
        ("snils", "snils_location"),
        ("address", "address_location"),
        ("address_kind", "address_location"),
        ("birth_date", "birth_date_location"),
        ("birth_place", "birth_place_location"),
    ):
        value = getattr(source_candidate, attr)
        if attr in practitioner_ids and value and validators.digits(value) in practitioner_ids[attr]:
            continue
        add(attr, value, source_index, getattr(source_candidate, location_attr))

    suggestion = {
        "kind": "operative_person" if source_candidate.role_location else "application_person",
        "warning": (
            "Двухкомпонентное имя сохранено буквально; проверьте порядок и падеж по оригиналу. "
            + ROLE_CANDIDATE_WARNING
            if source_candidate.anchor_kind == "two_part_person" else ROLE_CANDIDATE_WARNING
        ),
        "copyable_fields": [attr for attr in (
            "last_name", "first_name", "middle_name", "inn", "snils",
            *([] if source_candidate.address_partial else ["address", "address_kind"]),
        ) if attr in suggestion_fields],
        "fields": suggestion_fields,
    }
    if source_candidate.role_location:
        start, end = source_candidate.role_location
        for field in suggestion_fields.values():
            field["source_page"] = _source_page_at(variants[source_index], field["source_start"])
        suggestion.update(role="debtor", role_location={
            "source_variant": source_index, "source_start": start, "source_end": end,
            "source_page": _source_page_at(variants[source_index], start),
        })
    return suggestion


def add_review_candidates(card, variants: list[str]) -> None:
    """Add evidence for the isolated reviewer without changing office extraction."""
    if not variants or len(variants) > 5:
        return
    candidates = [_operative_person_candidate(text) for text in variants]
    # A reading without a unique operative clause cannot be ignored in favor
    # of another reading. Preserve the existing ambiguous review candidates.
    if all(candidate is not None for candidate in candidates):
        practitioner_ids = _practitioner_identifiers(variants)
        card.review_suggestions[:0] = [
            _person_review_suggestion(variants, index, candidate, .6, practitioner_ids=practitioner_ids)
            for index, candidate in enumerate(candidates) if candidate is not None
        ]
    else:
        from .entity_extract import isolated_review_candidates
        card.review_suggestions[:0] = isolated_review_candidates(variants)



def extract_applicant_evidence(variants: list[str], case_number: Field) -> dict | None:
    """Return one explicitly labelled applicant for human-reviewed assignment use.

    Application authorship, a bare occurrence of ``заявитель``, and an
    unlabelled person name cannot establish this role. A matching case number
    must be present in the same OCR variant as the labelled party.
    """
    if not case_number.value or case_number.warning:
        return None
    observed: dict[str, tuple[int, int, int, str]] = {}
    petition_confirmations: dict[str, set[int]] = {}
    labelled: set[str] = set()
    unresolved_label = False
    for index, text in enumerate(variants):
        if find_case_number(text) != case_number.value:
            continue
        for label in _APPLICANT_LABEL.finditer(text):
            if label.end() >= len(text):
                unresolved_label = True
                continue
            # An empty label cannot borrow the next OCR line or page.
            tail = text[label.end(): min(len(text), label.end() + 200)]
            line = tail.splitlines()[0].split(";", 1)[0].strip(" \t,;:.—-")
            stop = _APPLICANT_STOP.search(line)
            if stop:
                line = line[:stop.start()].rstrip(" \t,;:.—-")
            # A role label with an empty, overly long, or obviously procedural
            # continuation is not a usable party identity.
            if not 5 <= len(line) <= 160 or len(re.findall(r"[А-ЯЁа-яё]+", line)) < 2:
                unresolved_label = True
                continue
            if _APPLICANT_NON_IDENTITY_PREFIX.match(line):
                unresolved_label = True
                continue
            # The stripped identity can be shorter than the line after a field
            # label. Locate its exact source span rather than guessing offsets.
            actual = text.find(line, label.end(), min(len(text), label.end() + 200))
            if actual < 0:
                unresolved_label = True
                continue
            normalized = re.sub(r"\s+", " ", line).casefold()
            observed.setdefault(normalized, (index, actual, actual + len(line), line))
            labelled.add(normalized)
        for petition in _PETITIONING_COMPANY.finditer(text):
            if "\f" in petition[0]:
                continue
            name = re.sub(r"\s+", " ", petition.group("name")).strip()
            normalized = name.casefold()
            observed.setdefault(
                normalized,
                (index, petition.start("name"), petition.end("name"), name),
            )
            petition_confirmations.setdefault(normalized, set()).add(index)
    if unresolved_label or len(observed) != 1:
        return None
    normalized = next(iter(observed))
    if normalized not in labelled and len(petition_confirmations.get(normalized, ())) < 2:
        return None
    index, start, end, name = next(iter(observed.values()))
    return {
        "role_source": APPLICANT_ROLE_SOURCE,
        "name": name,
        "case_number": case_number.value,
        "source_variant": index,
        "source_start": start,
        "source_end": end,
        "source_page": _source_page_at(variants[index], start),
    }


def parse(variants: list[str]) -> DebtorCard:
    original_variants = [v for v in variants if v and v.strip()]
    variants = [_clean(v) for v in original_variants]
    if not variants:
        raise ValueError("Нет текста для разбора")

    card = DebtorCard(raw_text=max(variants, key=len))
    # Caption layout is source evidence; whitespace cleanup must not shift the
    # selected date's offsets in the original OCR reading.
    date_variants = [original if _decision_full_text_date(original) else cleaned
                     for original, cleaned in zip(original_variants, variants)]

    card.doc_type = _vote(variants, find_doc_type)
    card.doc_date = _vote(date_variants, find_doc_date)
    card.case_number = _vote(variants, find_case_number)
    card.court = _court_field(variants)
    _attach_field_source(card.doc_type, variants, _locate_doc_type)
    _attach_field_source(card.doc_date, date_variants, _locate_doc_date)
    _attach_field_source(card.case_number, variants, _locate_case_number)
    _metadata_warnings(card, variants, date_variants=date_variants)
    card.applicant_evidence = extract_applicant_evidence(variants, card.case_number)
    role_evidence = [_role_evidence(text) for text in variants]
    practitioner_ids = _practitioner_identifiers(variants)
    names = {_role_identity_key(item.fio) for item in role_evidence if item.fio}
    ambiguity = next(
        (
            item.warning for item in role_evidence
            if item.warning and item.warning not in (
                "Не установлена явная роль должника-физлица",
                "ФИО должника не связано с ролью",
            )
        ),
        None,
    )
    # Retain review-only candidates even if another OCR variant found an
    # explicit but conflicting debtor role. The human review form can show the
    # strongest candidate with a warning; authoritative fields stay gated below.
    has_review_candidate = _collect_application_candidate(
        card, variants,
        suppress_morphological=len(names) == 1 and not ambiguity,
        practitioner_ids=practitioner_ids,
    )
    candidate_applied = (
        not names
        and all(item.warning in (
            "Не установлена явная роль должника-физлица",
            "ФИО должника не связано с ролью",
        ) for item in role_evidence)
        and has_review_candidate
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
        preferred = next((item for item in named if item.nominative), named[0])
        fio = preferred.fio
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
        card.inn = _role_field(
            role_evidence, _section_inn,
            recoverable_warnings=(
                "Есть повреждённые варианты распознавания ИНН",
                "ИНН должника-физлица неоднозначен или не прошёл проверку",
            ),
        )
        card.snils = _role_field(role_evidence, _section_snils)
        for attr in ("inn", "snils"):
            field = getattr(card, attr)
            if field.value and validators.digits(field.value) in practitioner_ids[attr]:
                setattr(card, attr, Field(source="role_ambiguous", warning=_PRACTITIONER_ID_WARNING))
        # Birth date and birthplace remain excluded from authoritative card
        # fields; review-only candidate hints are kept in separate metadata.
        recoverable_address_warnings = (
            "Граница адреса должника не установлена",
            "Адрес должника упомянут без распознаваемой границы",
            "Номер помещения или корпуса в адресе не установлен",
        )
        card.address = _role_field(
            role_evidence, _section_address,
            recoverable_warnings=recoverable_address_warnings,
        )
        card.address_kind = _role_field(
            role_evidence, _section_address_kind,
            recoverable_warnings=recoverable_address_warnings,
        )

    if card.fio_raw.value and not candidate_applied:
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

    _attach_role_name_source(
        (card.fio_raw, card.last_name, card.first_name, card.middle_name),
        variants,
        role_evidence,
    )
    _attach_role_identifier_source(
        card.inn, variants, role_evidence,
        re.compile(r"ИНН[:\s]*([\d\s]{10,17})"), validators.digits,
    )
    _attach_role_identifier_source(
        card.snils, variants, role_evidence,
        re.compile(r"СНИЛС[:\s]*([\d\s\-]{11,20})"), validators.digits,
    )
    _attach_role_text_source(card.address, variants, role_evidence)

    if not card.inn.value and card.case_number.value and not card.inn.warning:
        card.inn.warning = "ИНН не найден; проверьте исходный документ"

    return card


def _field_recovery_choice(variants: list[str]):
    """Select one alternate reading for an observable defect, not an absent fact."""
    from .entity_extract import has_explicit_entity_marker, parse_entity
    text = "\n".join(variants)
    entity = any(has_explicit_entity_marker(v) for v in variants)
    card = parse_entity(variants) if entity else parse(variants)
    for label, name in ((r"\bИНН\b", "inn"), (r"\bСНИЛС\b", "snils")):
        field = getattr(card, name, None)
        if (re.search(label, text, re.I) and field is not None and not field.value
                and field.warning != _PRACTITIONER_ID_WARNING):
            return (6, name == "inn", "labelled_identifier_not_extracted")
    for apartment in re.finditer(r"\b(?:квартира|кв|офис)(?!\w)\.?[ \t]*[:№-]?[ \t]*([^\n]*)", text, re.I):
        if not apartment.group(1)[:1].isdigit():
            return (6, True, "apartment_token_without_number")
    identity = getattr(card, "fio_raw", None) or getattr(card, "legal_name", None)
    if identity and identity.value and not card.address.value and re.search(
        r"мест[оауе]\s+жительства|адрес(?:у|а)?\s+(?:регистрации|проживания)", text, re.I,
    ):
        return (6, False, "labelled_address_not_extracted")
    if not card.case_number.value and re.search(r"\bдел[оа]\b\s*(?:№|N)", text, re.I):
        return (4, False, "case_header_not_extracted")
    if not card.doc_date.value and re.search(r"ОПРЕДЕЛЕНИЕ|РЕШЕНИЕ|ПОСТАНОВЛЕНИЕ", text):
        return (4, False, "act_date_not_extracted")
    if not entity and not card.fio_raw.value and re.search(
        r"\b(?:должник(?:а|ом)?|гражданин(?:а|у)?|гражданк[аи])\b", text, re.I,
    ):
        return (11, False, "person_role_without_name")
    return None


def _retain_ocr_review_gate(card, budget) -> None:
    """Fewer readings must not manufacture unanimous multi-pass authority.

    Values, source spans, alternatives and checksum checks remain intact. Only
    critical OCR-derived fields lose any automatic promotion implied by fewer
    voters. Fully searchable PDFs retain their existing policy. Mixed PDFs do
    too only when every populated field is bound to a usable text page;
    OCR/unknown-origin values keep the card-wide review and preview gate.
    """
    if not budget.ocr_pages:
        return
    from .models import Field
    from .entity_extract import ENTITY_ROLE_SOURCE, ENTITY_CANDIDATE_SOURCE
    critical = {"fio_raw", "last_name", "first_name", "middle_name", "inn", "snils",
                "address", "address_kind", "legal_name", "short_name", "legal_form",
                "entity_kind", "debtor_name", "ogrn", "ogrnip", "kpp"}
    populated = [item for item in vars(card).values()
                 if isinstance(item, Field) and item.value]
    text_pages = getattr(budget, "text_layer_pages", set()) - budget.ocr_pages
    # Do not penalize a searchable debtor page merely because an unrelated
    # attachment needed OCR. All populated values must have proven text-layer
    # provenance: unknown or mixed-origin cards retain the conservative gate.
    # This card-wide condition also protects explicit-role preview admission.
    if populated and all(type(item.source_page) is int and item.source_page in text_pages
                         for item in populated):
        return
    for name in critical:
        field = getattr(card, name, None)
        if not isinstance(field, Field) or not field.value:
            continue
        if not field.warning:
            field.warning = "Сверьте значение с оригиналом"
        if field.source == ROLE_SOURCE:
            field.source = "role_partial"
        elif field.source == ENTITY_ROLE_SOURCE:
            field.source = ENTITY_CANDIDATE_SOURCE


def parse_variants_for_review(variants: list[str]):
    """Select a primary review shape without losing other participant evidence.

    Civil plaintiffs/applicants cannot displace existing person candidates.
    Entity-primary results retain those exact person occurrences for selection;
    fields from different people or organizations are never merged.
    """
    from .entity_extract import has_explicit_entity_marker, has_entity_participant, parse_entity
    person = parse(variants)
    strict_entity = any(has_explicit_entity_marker(text) for text in variants)
    if not strict_entity and not any(has_entity_participant(text) for text in variants):
        return person
    entity = parse_entity(variants)
    people = [item for item in person.review_suggestions if item.get("kind") == "application_person"]
    parties = [item for item in entity.review_suggestions if item.get("kind") == "entity_participant"]
    established_person = bool(person.fio_raw.value and person.fio_raw.source in {ROLE_SOURCE, "role_partial"})
    only_nondefendant_companies = bool(parties) and all(item.get("role") in {"plaintiff", "applicant"} for item in parties)
    if not strict_entity and (established_person or (people and only_nondefendant_companies)):
        person.review_suggestions.extend(deepcopy(parties))
        person.review_suggestions_complete &= entity.review_suggestions_complete
        return person
    # Put preserved person occurrences before bounded UI candidate inventories
    # can hide them. Main entity fields keep their independent source evidence.
    entity.review_suggestions = deepcopy(people) + entity.review_suggestions
    entity.review_suggestions_complete &= person.review_suggestions_complete
    return entity


def parse_file(
    path,
    dpi: int = 300,
    *,
    max_pdf_pages: int = 80,
    max_image_pixels: int = 12_000_000,
    max_tiff_frames: int = 80,
    budget=None,
) -> DebtorCard:
    path = Path(path)
    from .ocr import (
        OcrBudget,
        recognize,
        recover_role_bound_inn,
    )

    budget = budget or OcrBudget()
    from .source_locations import SourceLocationCollector
    locations = SourceLocationCollector() if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".heic", ".pdf"} else None
    location_options = {"source_location_collector": locations} if locations is not None else {}
    variants = recognize(
        path,
        dpi=dpi,
        max_pdf_pages=max_pdf_pages,
        max_image_pixels=max_image_pixels,
        max_tiff_frames=max_tiff_frames,
        budget=budget,
        recovery_decider=_field_recovery_choice,
        **location_options,
    )
    # Business debtors use a separate, conservative parser. Do not run the
    # person-name normalizer over a company or an entrepreneur name.
    from .entity_extract import EntityDebtorCard
    card = parse_variants_for_review(variants)
    if isinstance(card, EntityDebtorCard):
        _cross_check_low_resolution_date(
            path, card, budget, max_pdf_pages, max_image_pixels,
        )
        if locations is not None:
            locations.bind_card(card)
        _retain_ocr_review_gate(card, budget)
        return card
    if (
        card.fio_raw.value
        and not card.inn.value
        and card.inn.source == "role_ambiguous"
        and card.inn.warning
        and card.inn.warning != _PRACTITIONER_ID_WARNING
        and any(re.search(r"\bИНН\b", variant, re.I) for variant in variants)
    ):
        try:
            recovery = recover_role_bound_inn(
                path, variants, dpi=dpi, budget=budget,
                max_image_pixels=max_image_pixels,
                max_tiff_frames=max_tiff_frames,
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
        path, card, budget, max_pdf_pages, max_image_pixels,
    )
    if locations is not None:
        locations.bind_card(card)
    _retain_ocr_review_gate(card, budget)
    return card


def _cross_check_low_resolution_date(
    path: Path,
    card: DebtorCard,
    budget,
    max_pdf_pages: int,
    max_image_pixels: int,
) -> None:
    """Offer alternate header evidence without letting it bypass manual review."""
    if (
        path.suffix.lower() != ".pdf"
        or 1 not in budget.ocr_pages
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
    # The secondary reading has no TSV coordinates in the primary OCR variants.
    card.doc_date = Field(
        value=lower_resolution.value,
        source="ocr_scale_crosscheck",
        confidence=min(card.doc_date.confidence, lower_resolution.confidence, 0.69),
        warning="Распознавание даты расходится при другом масштабе; проверьте заголовок",
        source_page=1,
        source_excerpt=lower_resolution.source_excerpt,
    )
