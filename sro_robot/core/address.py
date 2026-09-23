"""Разбор адреса одной строкой на поля формы СРО.

Модалка адреса принимает адрес и целиком в одно поле, но заполненные по
отдельности индекс, регион и город делают карточку пригодной для поиска.
"""

from __future__ import annotations

import re

REGION_RE = re.compile(r"\b(область|обл\.?|край|респ\w*|округ|АО)\b", re.I)
CITY_RE = re.compile(r"\b(г\.|гор\.|город|пос\.|посёлок|поселок|с\.|село|д\.\s*[А-ЯЁ]|дер\.|ст\.)", re.I)
STREET_RE = re.compile(r"\b(ул\.|улица|пр-?кт|проспект|пер\.|переулок|шоссе|бул\.|бульвар|наб\.|проезд|линия|мкр)", re.I)
BUILDING_RE = re.compile(r"\b(д\.?\s*\d|дом\s*\d|корп|стр\.|влад)", re.I)
ROOM_RE = re.compile(r"\b(кв\.?\s*\d|квартира\s*\d|оф\.?\s*\d|офис\s*\d|комн)", re.I)
DISTRICT_RE = re.compile(r"\b(р-?н|район)\b", re.I)


def split_address(raw: str) -> dict[str, str]:
    """Раскладывает «141309, Московская область, г. Королёв, ул. ..., д. 8, кв. 42»."""
    result: dict[str, str] = {}
    if not raw:
        return result

    text = re.sub(r"\s+", " ", raw).strip()

    index = re.match(r"^(\d{6})\b[,\s]*", text)
    if index:
        result["index"] = index.group(1)
        text = text[index.end():]

    parts = [p.strip(" ,.;") for p in text.split(",") if p.strip(" ,.;")]
    leftovers: list[str] = []
    building: list[str] = []

    for part in parts:
        if ROOM_RE.search(part):
            result["room"] = part
        elif BUILDING_RE.search(part):
            building.append(part)
        elif STREET_RE.search(part):
            result["street"] = part
        elif CITY_RE.search(part) and "city" not in result:
            result["city"] = part
        elif DISTRICT_RE.search(part):
            result["district"] = part
        elif REGION_RE.search(part) and "area" not in result:
            result["area"] = part
        else:
            leftovers.append(part)

    if building:
        result["build"] = ", ".join(building)

    # Всё нераспознанное дописываем в улицу, чтобы ничего не потерять.
    if leftovers:
        street = result.get("street", "")
        result["street"] = ", ".join(filter(None, [street, *leftovers])) if street else ", ".join(leftovers)

    return result
