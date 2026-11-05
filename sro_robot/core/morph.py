"""Приведение ФИО из родительного падежа в именительный.

В определении должник упомянут как «заявление Бурцева Егора Анатольевича»,
а в карточку СРО нужно «Бурцев Егор Анатольевич».
"""

from __future__ import annotations

import functools
from typing import Optional

import pymorphy3

NAME_GRAMMEMES = ("Surn", "Name", "Patr")

# Отчества склоняются по жёстким правилам, морфология тут не нужна.
PATRONYMIC_ENDINGS = {
    "овича": "ович",
    "евича": "евич",
    "ьевича": "ьевич",
    "ича": "ич",
    "овны": "овна",
    "евны": "евна",
    "ьевны": "ьевна",
    "ичны": "ична",
    "ovna": "ovna",
}


@functools.lru_cache(maxsize=1)
def _analyzer() -> pymorphy3.MorphAnalyzer:
    return pymorphy3.MorphAnalyzer()


def _titlecase(word: str) -> str:
    return word[:1].upper() + word[1:].lower() if word else word


def normalize_word(word: str, grammeme: Optional[str] = None) -> str:
    """Ставит одно слово в именительный падеж единственного числа."""
    if not word:
        return word

    morph = _analyzer()
    parses = morph.parse(word)

    preferred = [p for p in parses if grammeme and grammeme in p.tag]
    if not preferred:
        preferred = [p for p in parses if any(g in p.tag for g in NAME_GRAMMEMES)]
    if not preferred:
        preferred = parses

    for parse in preferred:
        if "nomn" in parse.tag and "sing" in parse.tag:
            return _titlecase(parse.word)
        inflected = parse.inflect({"nomn", "sing"})
        if inflected:
            return _titlecase(inflected.word)

    return _titlecase(word)


def normalize_patronymic(word: str) -> str:
    low = word.lower()
    for genitive, nominative in PATRONYMIC_ENDINGS.items():
        if low.endswith(genitive):
            return _titlecase(low[: -len(genitive)] + nominative)
    return normalize_word(word, "Patr")


def normalize_fio(raw: str) -> tuple[str, str, str]:
    """«Бурцева Егора Анатольевича» -> («Бурцев», «Егор», «Анатольевич»)."""
    parts = [p for p in raw.replace("\u00a0", " ").split() if p]
    if not parts:
        return "", "", ""

    last = normalize_word(parts[0], "Surn")
    first = normalize_word(parts[1], "Name") if len(parts) > 1 else ""
    middle = normalize_patronymic(parts[2]) if len(parts) > 2 else ""
    return last, first, middle


@functools.lru_cache(maxsize=2048)
def person_triplet(parts: tuple[str, str, str]) -> tuple[str, str, str] | None:
    """Recognize an ordered surname, given name and patronymic for review.

    The given name and patronymic must agree in gender and grammatical case.
    A surname parse is selected using that gender: a feminine oblique surname
    must not silently become its masculine dictionary lemma.
    """
    surname, given, patronymic = parts
    analyzer = _analyzer()
    name_parses = [p for p in analyzer.parse(given) if "Name" in p.tag and "sing" in p.tag]
    patronymic_parses = [p for p in analyzer.parse(patronymic) if "Patr" in p.tag and "sing" in p.tag]
    if not name_parses or not patronymic_parses:
        return None
    pairs = [
        (name, patr) for name in name_parses for patr in patronymic_parses
        if name.tag.gender == patr.tag.gender
        and name.tag.case == patr.tag.case
    ]
    if not pairs:
        return None
    name, patr = max(pairs, key=lambda pair: pair[0].score * pair[1].score)
    surname_parses = [
        p for p in analyzer.parse(surname)
        if "Surn" in p.tag and "sing" in p.tag
        and p.tag.gender == name.tag.gender
    ]
    if not surname_parses:
        return None
    # Prefer agreement in case where morphology can identify it. Name spelling
    # may itself be ambiguous across nominative/genitive (e.g. female surnames).
    matching = [p for p in surname_parses if p.tag.case == name.tag.case]
    selected = max(matching or surname_parses, key=lambda p: p.score)
    nominative = [p.inflect({"nomn", "sing"}) for p in (selected, name, patr)]
    if any(value is None for value in nominative):
        return None
    return tuple(_titlecase(value.word) for value in nominative)


def guess_sex(middle_name: str, first_name: str = "") -> str:
    """Возвращает значение для списка «Пол» в СРО: «M», «Ж» или пустую строку."""
    mid = (middle_name or "").lower()
    if mid.endswith(("ович", "евич", "ич")):
        return "M"
    if mid.endswith(("овна", "евна", "ична", "чна")):
        return "Ж"

    if first_name:
        for parse in _analyzer().parse(first_name):
            if "Name" in parse.tag:
                if "masc" in parse.tag:
                    return "M"
                if "femn" in parse.tag:
                    return "Ж"
    return ""
