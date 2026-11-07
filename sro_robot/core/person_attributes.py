"""Review-only personal attributes within an already bounded person context.

The caller establishes whose context ``[start, end)`` is and where that context
ends. These suggestions never establish debtor identity or authorize a card.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from . import validators
from .address import closes_personal_data_parenthesis


Span = tuple[int, int]


@dataclass(frozen=True)
class PersonAttributes:
    inn: str | None = None
    snils: str | None = None
    address: str | None = None
    address_kind: str | None = None
    address_partial: bool = False
    birth_date: str | None = None
    birth_place: str | None = None
    inn_location: Span | None = None
    snils_location: Span | None = None
    address_location: Span | None = None
    birth_date_location: Span | None = None
    birth_place_location: Span | None = None
    ambiguous_fields: tuple[str, ...] = ()


# A standalone number must be exactly twelve digits. The checksum protects
# against case numbers, ordinary dates and unrelated numerical identifiers.
_INN = re.compile(r"(?<![\d/:-])(?<!\d\.)\d{12}(?!\d|[/:.-]\d)")
_SNILS = re.compile(r"(?<!\d)(?:\d{3}[- ]\d{3}[- ]\d{3}[ -]\d{2})(?!\d)")
_LABELED_SNILS = re.compile(r"\bСНИЛС\s*[:№-]?\s*(\d{11})(?!\d)", re.I)

_LABEL = re.compile(
    r"\b(?P<registration>адрес(?:а)?\s+регистрации)\s*[:—-]?\s*"
    r"|\b(?P<actual>адрес(?:\s+проживания)?|прожива\w*\s*(?:по\s+адресу)?|"
    r"фактическ\w*\s+адрес)\s*[:—-]\s*",
    re.I,
)
_CITY = re.compile(r"\b(?:г\.|город|пос\.|поселок|посёлок|с\.|село|дер\.|деревня)\s*[А-ЯЁа-яё][^,;\n()]{1,55}", re.I)
_STREET = re.compile(r"\b(?:ул\.|улица|проспект|пр\.|пер\.|переулок|ш\.|шоссе|наб\.|набережная)\s*[А-ЯЁа-яё0-9][^,;\n()]{1,65}", re.I)
_HOUSE = re.compile(r"\b(?:д\.|дом|домовладение)\s*№?\s*\d+[а-яёa-z]?(?:[/\-]\d+[а-яёa-z]?)?\b", re.I)
_UNIT_MARKER = r"(?:кв\.|квартира|корп\.|корпус|стр\.|строение|пом\.|помещение)"
_UNIT_LABEL = re.compile(rf"\s*,?\s*{_UNIT_MARKER}", re.I)
_UNIT = re.compile(rf"\s*,?\s*(?P<label>{_UNIT_MARKER})\s*№?\s*(?P<number>\d+[а-яёa-z]?(?:[/\-]\d+)?)\b", re.I)
_POSTCODE = re.compile(r"\d{6}\s*,?\s*$")
_ADDRESS_STOP = re.compile(r"\b(?:ИНН|СНИЛС|дата\s+рождения|место\s+рождения|должник\w*|заявител\w*)\b|[;)]", re.I)
_BIRTH_DATE = re.compile(r"\bдата\s+рождения\s*[:—-]?\s*(\d{2}[./-]\d{2}[./-]\d{4})", re.I)
_BIRTH_PLACE = re.compile(r"\bместо\s+рождения\s*[:—-]?\s*([^,;\n()]{2,100})", re.I)


def _choose(candidates: list[tuple[str, Span]]) -> tuple[str | None, Span | None, bool]:
    """Withhold distinct values; repeated identical values keep the first span."""
    unique: dict[str, Span] = {}
    for value, span in candidates:
        unique.setdefault(value, span)
    if len(unique) != 1:
        return None, None, len(unique) > 1
    value, span = next(iter(unique.items()))
    return value, span, False


def _address_candidates(section: str, offset: int) -> list[tuple[str, str | None, bool, Span]]:
    candidates: list[tuple[str, str | None, bool, Span]] = []
    labels = list(_LABEL.finditer(section))
    anchors: list[tuple[int, str | None, int | None]] = [
        (m.end(), "registration" if m.group("registration") else "actual", m.start())
        for m in labels
    ]
    # A strong address syntax also works when the address label is absent.
    anchors.extend((m.start(), None, None) for m in _CITY.finditer(section))
    anchors.extend((m.start(), None, None) for m in _STREET.finditer(section))
    seen_spans: set[Span] = set()
    for anchor, kind, label_start in sorted(anchors, key=lambda item: item[0]):
        limit = min(len(section), anchor + 250)
        next_label = next((m.start() for m in labels if m.start() > anchor), None)
        if next_label is not None:
            limit = min(limit, next_label)
        stop = _ADDRESS_STOP.search(section, anchor, limit)
        if stop:
            limit = stop.start()
        if limit <= anchor:
            continue
        raw = section[anchor:limit]
        city = _CITY.search(raw)
        street = _STREET.search(raw)
        house = _HOUSE.search(raw)
        if street and house and street.start() < house.start() and house.start() - street.end() < 40:
            if city and city.start() < street.start() and street.start() - city.end() < 75:
                local_start = city.start()
            elif kind is not None or label_start is None:
                local_start = street.start()
            else:
                continue
            if kind is not None:
                postcode = _POSTCODE.search(raw[:local_start])
                if postcode:
                    local_start = postcode.start()
            local_end = house.end()
            while unit := _UNIT.match(raw, local_end):
                gap = raw[unit.end("label"):unit.start("number")]
                if "\n" in gap:
                    suffix = section[anchor + unit.end():]
                    next_field = re.match(
                        r"[ \t]*,?[ \t]*\r?\n[ \t]*(?:ИНН|СНИЛС|дата\s+рождения|"
                        r"место\s+рождения|должник\w*|заявител\w*)\b", suffix, re.I,
                    )
                    if (suffix.strip() and next_field is None
                            and not closes_personal_data_parenthesis(section, anchor + unit.end())):
                        # Only a terminated value/next personal field proves
                        # this wrapped number is a unit, rather than prose.
                        break
                local_end = unit.end()
            partial = city is None or _UNIT_LABEL.match(raw, local_end) is not None
        elif kind is not None and city and not street and not house:
            local_start, local_end, partial = city.start(), city.end(), True
        else:
            continue
        beginning = anchor + local_start
        ending = anchor + local_end
        while beginning < ending and section[beginning].isspace():
            beginning += 1
        while ending > beginning and section[ending - 1].isspace():
            ending -= 1
        span = (offset + beginning, offset + ending)
        if span in seen_spans:
            continue
        seen_spans.add(span)
        value = re.sub(r"\s+", " ", section[beginning:ending]).strip(" ,.;:")
        if value:
            candidates.append((value, kind, partial, span))
    return candidates


def detect_person_attributes(text: str, start: int, end: int) -> PersonAttributes:
    """Suggest fields from a caller-established same-person ``[start, end)``.

    All locations are absolute source offsets. Conflicting values are withheld
    independently per field. Unlabeled addresses never imply registration.
    """
    if not 0 <= start <= end <= len(text):
        raise ValueError("invalid person context span")
    section = text[start:end]
    inns = [
        (m.group(), (start + m.start(), start + m.end()))
        for m in _INN.finditer(section)
        if validators.valid_inn(m.group())
        and not re.search(r"\bкадастров\w*\b", re.split(r"[,;\n]", section[max(0, m.start() - 50):m.start()])[-1], re.I)
    ]
    inn, inn_location, inn_ambiguous = _choose(inns)
    snils_candidates: list[tuple[str, Span]] = []
    for pattern, group in ((_SNILS, 0), (_LABELED_SNILS, 1)):
        for match in pattern.finditer(section):
            raw = match.group(group)
            if validators.valid_snils(raw):
                snils_candidates.append((validators.format_snils(raw), (start + match.start(group), start + match.end(group))))
    snils, snils_location, snils_ambiguous = _choose(snils_candidates)

    addresses = _address_candidates(section, start)
    # Street-only anchors inside a longer city/street/house candidate are the
    # same address, not contradictory addresses.
    addresses = [
        item for item in addresses
        if not any(
            other[3] != item[3]
            and other[3][0] <= item[3][0]
            and item[3][1] <= other[3][1]
            for other in addresses
        )
    ]
    distinct_addresses: dict[str, tuple[str | None, bool, Span]] = {}
    for value, kind, partial, span in addresses:
        previous = distinct_addresses.get(value)
        if previous is None or (previous[0] is None and kind is not None):
            distinct_addresses[value] = (kind, partial, span)
    address_ambiguous = len(distinct_addresses) > 1
    address = address_kind = address_location = None
    address_partial = False
    if len(distinct_addresses) == 1:
        address, (address_kind, address_partial, address_location) = next(iter(distinct_addresses.items()))

    dates: list[tuple[str, Span]] = []
    for match in _BIRTH_DATE.finditer(section):
        raw = match.group(1)
        day, month, year = (int(part) for part in re.split(r"[./-]", raw))
        try:
            date(year, month, day)
        except ValueError:
            continue
        dates.append((raw, (start + match.start(1), start + match.end(1))))
    birth_date, birth_date_location, date_ambiguous = _choose(dates)
    places = [
        (match.group(1).strip(), (start + match.start(1), start + match.end(1) - len(match.group(1)) + len(match.group(1).rstrip())))
        for match in _BIRTH_PLACE.finditer(section)
    ]
    birth_place, birth_place_location, place_ambiguous = _choose(places)
    ambiguous_fields = tuple(
        name for name, ambiguous in (
            ("inn", inn_ambiguous), ("snils", snils_ambiguous),
            ("address", address_ambiguous), ("birth_date", date_ambiguous),
            ("birth_place", place_ambiguous),
        ) if ambiguous
    )
    return PersonAttributes(
        inn=inn, snils=snils, address=address, address_kind=address_kind,
        address_partial=address_partial, birth_date=birth_date, birth_place=birth_place,
        inn_location=inn_location, snils_location=snils_location,
        address_location=address_location, birth_date_location=birth_date_location,
        birth_place_location=birth_place_location, ambiguous_fields=ambiguous_fields,
    )
