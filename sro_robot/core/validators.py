"""Контрольные суммы ИНН и СНИЛС.

Главная польза не в проверке самих документов, а в отлове ошибок OCR:
потерянная или подменённая цифра почти всегда рушит контрольный разряд.
"""

from __future__ import annotations

import re
import unicodedata

_INN_WEIGHTS_11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_INN_WEIGHTS_12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)


def digits(value: str) -> str:
    return "".join(
        str(unicodedata.decimal(character))
        for character in (value or "")
        if character.isdecimal()
    )


def _checksum(nums: list[int], weights: tuple[int, ...]) -> int:
    return sum(n * w for n, w in zip(nums, weights)) % 11 % 10


def valid_inn(value: str) -> bool:
    d = digits(value)
    if len(d) not in (10, 12) or not d.isdigit() or not d.strip("0"):
        return False
    nums = [int(c) for c in d]

    if len(d) == 10:
        return _checksum(nums[:9], _INN_WEIGHTS_11[1:]) == nums[9]

    return (
        _checksum(nums[:10], _INN_WEIGHTS_11) == nums[10]
        and _checksum(nums[:11], _INN_WEIGHTS_12) == nums[11]
    )


def valid_ogrn(value: str) -> bool:
    """Check the legal-entity OGRN type digit and 13-digit control number."""
    d = digits(value)
    return (
        len(d) == 13
        and d[0] in ("1", "5")
        and bool(d.strip("0"))
        and str(int(d[:12]) % 11 % 10) == d[-1]
    )


def valid_ogrnip(value: str) -> bool:
    """Check the 15-digit sole-proprietor OGRNIP control digit."""
    d = digits(value)
    return (
        len(d) == 15
        and d[0] == "3"
        and bool(d.strip("0"))
        and str(int(d[:14]) % 13 % 10) == d[-1]
    )


def valid_snils(value: str) -> bool:
    d = digits(value)
    if len(d) != 11:
        return False

    number, control = d[:9], int(d[9:])
    if len(set(number)) == 1 and number[0] == "0":
        return False

    total = sum(int(c) * (9 - i) for i, c in enumerate(number))
    if total < 100:
        expected = total
    elif total in (100, 101):
        expected = 0
    else:
        expected = total % 101
        if expected in (100, 101):
            expected = 0
    return expected == control


def format_snils(value: str) -> str:
    d = digits(value)
    if len(d) != 11:
        return value
    return f"{d[0:3]}-{d[3:6]}-{d[6:9]} {d[9:11]}"


def normalize_case_number(value: str) -> str:
    """Приводит номер дела к виду А41-61776/2026, чиня латиницу от OCR."""
    if not value:
        return value
    cleaned = re.sub(r"\s+", "", value).upper().lstrip("№")
    # Tesseract регулярно путает латинские буквы с русскими в индексе суда.
    for latin, cyrillic in (("A", "А"), ("B", "В"), ("C", "С"), ("E", "Е"), ("K", "К")):
        cleaned = cleaned.replace(latin, cyrillic)
    return cleaned
