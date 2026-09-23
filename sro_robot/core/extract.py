"""Извлечение полей карточки должника из распознанного текста.

OCR даёт несколько вариантов текста одного документа. Каждый вариант парсится
отдельно, а итоговое значение поля выбирается голосованием с приоритетом
значений, прошедших контрольную сумму.
"""

from __future__ import annotations

import re
from collections import Counter
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


def find_fio(text: str) -> Optional[str]:
    patterns = (
        rf"рассмотрев\s+заявление\s+({WORD}\s+{WORD}\s+{WORD})",
        rf"заявление\s+({WORD}\s+{WORD}\s+{WORD})\s*\(?\s*\d",
        rf"должник[а-я]*\s*[-—:]?\s*({WORD}\s+{WORD}\s+{WORD})",
        rf"({WORD}\s+{WORD}\s+{WORD})\s*\(\s*\d{{2}}[.,]\d{{2}}[.,]\d{{4}}",
        rf"в\s+отношении\s+({WORD}\s+{WORD}\s+{WORD})",
        rf"заявление\s+({WORD}\s+{WORD}\s+{WORD})",
    )
    for pattern in patterns:
        m = re.search(pattern, text)
        if not m:
            continue
        candidate = re.sub(r"\s+", " ", m.group(1)).strip()
        # Отсекаем совпадения вида «Арбитражного Процессуального Кодекса».
        if any(stop in candidate.lower() for stop in ("суд", "кодекс", "закон", "ассоциаци")):
            continue
        return candidate
    return None


def find_inn(text: str) -> Optional[str]:
    for m in re.finditer(r"ИНН[:\s]*([\d\s]{10,17})", text):
        d = validators.digits(m.group(1))
        for length in (12, 10):
            if len(d) >= length and validators.valid_inn(d[:length]):
                return d[:length]
        if d:
            return d[:12] if len(d) >= 12 else d
    return None


def find_snils(text: str) -> Optional[str]:
    m = re.search(r"СНИЛС[:\s]*([\d\s\-]{11,20})", text)
    if not m:
        m = re.search(r"\b(\d{3}\s?-\s?\d{3}\s?-\s?\d{3}\s?[\s-]\s?\d{2})\b", text)
    if not m:
        return None
    d = validators.digits(m.group(1))
    return validators.format_snils(d[:11]) if len(d) >= 11 else None


def find_birth_date(text: str) -> Optional[str]:
    m = re.search(r"(\d{2})[.,](\d{2})[.,](\d{4})\s*г\.?\s*[рp]", text)
    if m:
        return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
    return None


def find_birth_place(text: str) -> Optional[str]:
    m = re.search(r"место\s+рождения[:\s]*(.+?)(?=,?\s*(?:ИНН|СНИЛС|адрес)|\n\s*\n|$)", text, re.S | re.I)
    if not m:
        return None
    place = re.sub(r"\s+", " ", m.group(1)).strip(" ,.;:")
    # «ИНН» и «СНИЛС» Tesseract пишет как «ИНИ», «СНИЛ С» — режем по корню и по длинным числам.
    place = re.split(r"\s*(?:ИН\w?|СНИЛ\w?|адрес|\d{6,})", place, flags=re.I)[0]
    return place.strip(" ,.;:") or None


def find_address(text: str) -> tuple[Optional[str], Optional[str]]:
    """Возвращает адрес и его тип: регистрации или фактический."""
    registration = re.search(
        r"адрес(?:а)?\s+регистрации[:\s]*(.+?)(?=\)\s*о\s+призна|о\s+призна|\n\s*\n|$)",
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


def find_court(text: str) -> Optional[str]:
    m = re.search(r"(Арбитражный\s+суд\s+[А-ЯЁа-яё]+(?:\s+(?:области|края|округа|Республики)\w*)?)", text)
    return re.sub(r"\s+", " ", m.group(1)) if m else None


def find_doc_type(text: str) -> Optional[str]:
    head = text[:1500].upper()
    for marker, name in DOC_TYPES.items():
        if marker in head:
            return name
    return None


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


def parse(variants: list[str]) -> DebtorCard:
    variants = [_clean(v) for v in variants if v and v.strip()]
    if not variants:
        raise ValueError("Нет текста для разбора")

    card = DebtorCard(raw_text=max(variants, key=len))

    card.doc_type = _vote(variants, find_doc_type)
    card.doc_date = _vote(variants, find_doc_date)
    card.case_number = _vote(variants, find_case_number)
    card.court = _vote(variants, find_court)
    card.inn = _vote(variants, find_inn, validators.valid_inn)
    card.snils = _vote(variants, find_snils, validators.valid_snils)
    card.birth_date = _vote(variants, find_birth_date)
    card.birth_place = _vote(variants, find_birth_place)
    card.fio_raw = _vote(variants, find_fio)
    card.address = _vote(variants, lambda t: find_address(t)[0], score=_address_score)
    card.address_kind = _vote(variants, lambda t: find_address(t)[1])

    if card.fio_raw.value:
        last, first, middle = morph.normalize_fio(card.fio_raw.value)
        conf = card.fio_raw.confidence
        card.last_name = Field(last, conf, "morph", card.fio_raw.warning)
        card.first_name = Field(first, conf, "morph")
        card.middle_name = Field(middle, conf, "morph")

    if not card.inn.value and card.case_number.value:
        card.inn.warning = "ИНН не найден — в карточку уйдёт номер дела"

    return card


def parse_file(path, dpi: int = 300) -> DebtorCard:
    from .ocr import recognize

    return parse(recognize(path, dpi=dpi))
