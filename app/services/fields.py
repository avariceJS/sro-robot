"""Мост между контрактом сценария и экраном проверки.

Интерфейс не знает названий полей: он спрашивает их у сценария. Поэтому
карточка организации отрисуется тем же шаблоном, что и карточка физлица.
"""

from __future__ import annotations

from dataclasses import dataclass

from sro_robot.core.models import Field, card_from_fields
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
        return self.confidence < LOW_CONFIDENCE


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
                else "uncertain" if field.warning or field.confidence < LOW_CONFIDENCE
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
