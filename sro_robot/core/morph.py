"""Приведение ФИО из родительного падежа в именительный.

В определении должник упомянут как «заявление Бурцева Егора Анатольевича»,
а в карточку СРО нужно «Бурцев Егор Анатольевич».
"""

from __future__ import annotations

import functools
from typing import Optional

import pymorphy3

NAME_GRAMMEMES = ("Surn", "Name", "Patr")
_ORTHOGRAPHIC_HYPHENS = str.maketrans("‐‑", "--")

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
    word = word.translate(_ORTHOGRAPHIC_HYPHENS)
    return "-".join(part[:1].upper() + part[1:].lower() for part in word.split("-"))


def normalize_word(word: str, grammeme: Optional[str] = None) -> str:
    """Ставит одно слово в именительный падеж единственного числа."""
    if not word:
        return word
    word = word.translate(_ORTHOGRAPHIC_HYPHENS)

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
    if len(parts) == 3 and (agreed := person_triplet(tuple(parts))):
        return agreed

    last = normalize_word(parts[0], "Surn")
    first = normalize_word(parts[1], "Name") if len(parts) > 1 else ""
    middle = normalize_patronymic(parts[2]) if len(parts) > 2 else ""
    return last, first, middle


@functools.lru_cache(maxsize=2048)
def person_pair(parts: tuple[str, str]) -> tuple[str, str, str] | None:
    """Keep a labeled surname/name pair literal when its given name is known.

    Two components do not establish grammatical gender/case agreement. Avoid
    inventing a patronymic or choosing a nominative surname from this evidence.
    """
    if any(p.is_known and "Name" in p.tag for p in _analyzer().parse(parts[1])):
        return *parts, ""
    return None


@functools.lru_cache(maxsize=2048)
def person_triplet(
    parts: tuple[str, str, str], *, allow_literal_surname: bool = False,
) -> tuple[str, str, str] | None:
    """Recognize an ordered surname, given name and patronymic for review.

    The given name and patronymic must agree in gender and grammatical case.
    A surname parse is selected using that gender: a feminine oblique surname
    must not silently become its masculine dictionary lemma. A known
    indeclinable surname without grammatical gender can agree with either.
    """
    surname, given, patronymic = (part.translate(_ORTHOGRAPHIC_HYPHENS) for part in parts)
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
        and (p.tag.gender == name.tag.gender
             or (p.tag.gender is None and "Fixd" in p.tag and p.is_known))
    ]
    if not surname_parses:
        if allow_literal_surname:
            given_nominative = name.inflect({"nomn", "sing"})
            patronymic_nominative = patr.inflect({"nomn", "sing"})
            if given_nominative and patronymic_nominative:
                return _titlecase(surname), _titlecase(given_nominative.word), _titlecase(patronymic_nominative.word)
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
