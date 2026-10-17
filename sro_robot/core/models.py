"""Структуры данных, которые ездят между OCR, парсером и роботом СРО."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Literal, Optional

AddressKind = Literal["registration", "actual"]


@dataclass
class Field:
    """Значение поля вместе с тем, откуда оно взялось и можно ли ему верить."""

    value: Optional[str] = None
    confidence: float = 0.0
    source: str = ""
    # Заполняется, если поле не прошло проверку и требует глаз оператора.
    warning: Optional[str] = None
    # Location in normalized OCR text; `source_variant` is the recognizer
    # output index, not a document page number.
    source_variant: Optional[int] = None
    source_start: Optional[int] = None
    source_end: Optional[int] = None
    source_excerpt: Optional[str] = None

    def __bool__(self) -> bool:
        return bool(self.value)


@dataclass
class DebtorCard:
    doc_type: Field = field(default_factory=Field)
    doc_date: Field = field(default_factory=Field)
    case_number: Field = field(default_factory=Field)
    court: Field = field(default_factory=Field)

    last_name: Field = field(default_factory=Field)
    first_name: Field = field(default_factory=Field)
    middle_name: Field = field(default_factory=Field)
    fio_raw: Field = field(default_factory=Field)

    inn: Field = field(default_factory=Field)
    snils: Field = field(default_factory=Field)

    address: Field = field(default_factory=Field)
    address_kind: Field = field(default_factory=Field)

    raw_text: str = ""

    @property
    def fio(self) -> str:
        parts = [self.last_name.value, self.first_name.value, self.middle_name.value]
        return " ".join(p for p in parts if p)

    @property
    def inn_or_case(self) -> str:
        """В карточку СРО вместо отсутствующего ИНН кладётся номер дела."""
        return self.inn.value or self.case_number.value or ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw_text", None)
        d["fio"] = self.fio
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


def card_to_fields(card) -> dict[str, dict]:
    """Поля карточки в вид, пригодный для JSONB: `{attr: {value, confidence, ...}}`.

    Работает с любой карточкой-датаклассом, а не только с `DebtorCard`:
    у организации и назначения будут свои наборы полей.
    """
    return {
        name: asdict(value)
        for name, value in vars(card).items()
        if isinstance(value, Field)
    }


def card_from_fields(card_class: type, data: dict[str, dict], raw_text: str = ""):
    """Обратное преобразование: карточка из сохранённых в базе полей."""
    card = card_class()
    for name, payload in (data or {}).items():
        if isinstance(getattr(card, name, None), Field) and isinstance(payload, dict):
            setattr(card, name, Field(**payload))
    card.raw_text = raw_text
    return card
