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


def looks_like_genitive_fio(raw: str) -> bool:
    parts = raw.split()
    if len(parts) < 2:
        return False
    morph = _analyzer()
    for part in parts[:3]:
        for parse in morph.parse(part):
            if "gent" in parse.tag and any(g in parse.tag for g in NAME_GRAMMEMES):
                return True
    return False
