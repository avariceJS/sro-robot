"""Мост между контрактом сценария и экраном проверки.

Интерфейс не знает названий полей: он спрашивает их у сценария. Поэтому
карточка организации отрисуется тем же шаблоном, что и карточка физлица.
"""

from __future__ import annotations

from dataclasses import dataclass

from sro_robot.core.models import Field, card_from_fields, confidence_at_least
from sro_robot.scenarios import Scenario, get_scenario

from ..db.models import Task

# Ниже этого порога значение подсвечивается: оператору стоит глянуть на скан.
LOW_CONFIDENCE = 0.7


@dataclass
class ReviewField:
    """Строка экрана проверки."""

    attr: str
    title: str
    value: str
    confidence: float
    warning: str
    evidence_status: str
    required: bool
    choices: tuple[tuple[str, str], ...]
    source_variant: int | None = None
    source_start: int | None = None
    source_end: int | None = None
    source_excerpt: str = ""
    source_page: int | None = None

    @property
    def uncertain(self) -> bool:
        return not confidence_at_least(self.confidence, LOW_CONFIDENCE)


def scenario_of(task: Task) -> Scenario:
    return get_scenario(task.scenario)


def card_of(task: Task):
    """Карточка задания: распознанное плюс правки оператора поверх."""
    scenario = scenario_of(task)
    card = card_from_fields(scenario.card_class, task.recognized or {})
    return scenario.card_from_values(task.values or {}, base=card)


def review_fields(task: Task) -> list[ReviewField]:
    scenario = scenario_of(task)
    card = card_of(task)
    rows: list[ReviewField] = []
    for spec in scenario.fields:
        field = getattr(card, spec.attr, None)
        if not isinstance(field, Field):
            continue
        rows.append(ReviewField(
            attr=spec.attr,
            title=spec.title,
            value=field.value or "",
            confidence=field.confidence,
            warning=field.warning or "",
            evidence_status=(
                "missing" if not field.value
                else "uncertain" if field.warning or field.uncertain
                else "recognized"
            ),
            required=spec.required,
            choices=spec.choices,
            source_variant=field.source_variant,
            source_page=field.source_page,
            source_start=field.source_start,
            source_end=field.source_end,
            source_excerpt=field.source_excerpt or "",
        ))

    # Role-unbound OCR candidates stay separate from authoritative OCR fields
    # and card preparation. For the review form only, prefill a value when every
    # application-person candidate contains the same copyable value. This keeps
    # the familiar editable workflow while leaving conflicting or incomplete
    # candidates for explicit employee selection.
    suggestions = (task.recognized or {}).get("_review_suggestions_v1", [])
    person_suggestions = [
        item for item in suggestions
        if isinstance(item, dict) and item.get("kind") == "application_person"
    ]
    rows_by_attr = {row.attr: row for row in rows}
    operator_values = task.values or {}
    if person_suggestions:
        attrs = {
            attr
            for item in person_suggestions
            for attr in (
                item.get("copyable_fields", [])
                if isinstance(item.get("copyable_fields", []), list) else []
            )
            if isinstance(attr, str)
        }
        for attr in attrs:
            row = rows_by_attr.get(attr)
            if row is None or row.value or attr in operator_values:
                continue
            candidates = []
            for item in person_suggestions:
                copyable_fields = item.get("copyable_fields", [])
                if not isinstance(copyable_fields, list) or attr not in copyable_fields:
                    candidates = []
                    break
                fields = item.get("fields", {})
                candidate = fields.get(attr) if isinstance(fields, dict) else None
                value = candidate.get("value") if isinstance(candidate, dict) else None
                if not isinstance(value, str) or not value.strip():
                    candidates = []
                    break
                candidates.append(candidate)
            values = {candidate["value"].strip() for candidate in candidates}
            if len(candidates) != len(person_suggestions) or len(values) != 1:
                continue
            source = candidates[0]
            row.value = source["value"].strip()
            row.confidence = min(float(source.get("confidence", 0)), LOW_CONFIDENCE - 0.01)
            row.warning = "Кандидат OCR без подтверждённой роли; проверьте по оригиналу."
            row.evidence_status = "uncertain"
            row.source_variant = source.get("source_variant")
            row.source_start = source.get("source_start")
            row.source_end = source.get("source_end")
            row.source_excerpt = source.get("source_excerpt", "") or ""
    return rows


def summary(task: Task) -> str:
    """Короткая подпись задания в списке: ФИО или первое заполненное поле."""
    card = card_of(task)
    fio = getattr(card, "fio", "")
    if fio:
        return fio
    for spec in scenario_of(task).fields:
        field = getattr(card, spec.attr, None)
        if isinstance(field, Field) and field.value:
            return field.value
    if task.intake_item is not None:
        return task.intake_item.filename
    return task.document.filename if task.document else f"Задание {task.id}"
