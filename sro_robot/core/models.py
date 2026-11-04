"""Структуры данных, которые ездят между OCR, парсером и роботом СРО."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from typing import Literal, Optional

AddressKind = Literal["registration", "actual"]


def confidence_at_least(value: object, threshold: float = 0.7) -> bool:
    """Accept a confidence only when it is finite and within its defined scale."""
    if isinstance(value, bool):
        return False
    try:
        confidence = float(value)
        minimum = float(threshold)
    except (TypeError, ValueError, OverflowError):
        return False
    return (
        math.isfinite(confidence)
        and 0.0 <= confidence <= 1.0
        and math.isfinite(minimum)
        and 0.0 <= minimum <= 1.0
        and confidence >= minimum
    )


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
    # One-based PDF page when the recognized variant retained form-feed page
    # separators; absent for inputs whose page boundary is unknown. Kept last
    # to preserve positional construction of the older evidence fields.
    source_page: Optional[int] = None
    # Normalized word rectangles in the original image, collected during OCR.
    source_boxes: list[dict] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.value)

    @property
    def uncertain(self) -> bool:
        return not confidence_at_least(self.confidence)


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

    # Unbound OCR candidates remain review-only metadata and never enter
    # authoritative card fields or automatic card preparation.
    review_suggestions: list[dict] = field(default_factory=list)
    review_suggestions_complete: bool = True

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
