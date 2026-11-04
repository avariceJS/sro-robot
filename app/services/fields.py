"""Мост между контрактом сценария и экраном проверки.

Интерфейс не знает названий полей: он спрашивает их у сценария. Поэтому
карточка организации отрисуется тем же шаблоном, что и карточка физлица.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
    source_boxes: list[dict] = field(default_factory=list)
    source_value: str = ""

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
            source_boxes=field.source_boxes,
            source_value=field.value or "",
        ))

    # Role-unbound OCR candidates stay separate from authoritative OCR fields
    # and card preparation. The editable form should still show the best OCR
    # candidate so an employee can correct it. Choose the person candidate
    # supported by the most distinct OCR variants, breaking ties by confidence
    # and then by document order. Every copied field remains explicitly
    # uncertain and requires the normal employee confirmation.
    recognized = task.recognized or {}
    suggestions = recognized.get("_review_suggestions_v1", [])
    person_suggestions = [
        item for item in suggestions
        if isinstance(item, dict) and item.get("kind") == "application_person"
    ]
    rows_by_attr = {row.attr: row for row in rows}
    operator_values = task.values or {}

    def candidate_field(item: dict, attr: str) -> dict | None:
        fields = item.get("fields", {})
        candidate = fields.get(attr) if isinstance(fields, dict) else None
        if not isinstance(candidate, dict):
            return None
        value = candidate.get("value")
        return candidate if isinstance(value, str) and value.strip() else None

    def candidate_score(candidate: dict | None) -> float:
        if candidate is None:
            return 0.0
        try:
            score = float(candidate.get("confidence", 0))
        except (TypeError, ValueError):
            return 0.0
        return score if 0 <= score <= 1 else 0.0

    if person_suggestions:
        groups: dict[tuple[str, ...], list[tuple[int, dict]]] = {}
        for index, item in enumerate(person_suggestions):
            name = tuple(
                (candidate_field(item, attr) or {}).get("value", "").strip().casefold()
                for attr in ("last_name", "first_name", "middle_name")
            )
            # Keep candidates without a complete normalized FIO separate.
            key = name if all(name) else (f"_candidate_{index}",)
            groups.setdefault(key, []).append((index, item))

        identity_attrs = ("last_name", "first_name", "middle_name")
        identity_constraints = {
            attr: rows_by_attr[attr].value.strip().casefold()
            for attr in identity_attrs
            if attr in rows_by_attr and rows_by_attr[attr].value.strip()
        }
        if identity_constraints:
            matching_groups = {
                key: group for key, group in groups.items()
                if len(key) == len(identity_attrs)
                and all(key[identity_attrs.index(attr)] == value
                        for attr, value in identity_constraints.items())
            }
            # Never use a role-unbound person's identifiers to fill gaps beside
            # an already recognized or operator-entered different FIO.
            if not matching_groups:
                return rows
            groups = matching_groups

        def group_score(group: list[tuple[int, dict]]) -> tuple[int, float]:
            per_variant: dict[int | str, float] = {}
            for index, item in group:
                name_field = next(
                    (candidate_field(item, attr) for attr in ("last_name", "first_name", "middle_name")
                     if candidate_field(item, attr)),
                    None,
                )
                variant = name_field.get("source_variant") if name_field else None
                if not isinstance(variant, int) or isinstance(variant, bool):
                    variant = f"candidate_{index}"
                per_variant[variant] = max(
                    per_variant.get(variant, 0.0), candidate_score(name_field)
                )
            return len(per_variant), sum(per_variant.values())

        # max() keeps the first group on an exact tie; parser order follows the
        # first supporting OCR variant and then source-document position.
        selected_group = max(groups.values(), key=group_score)
        attrs = {
            attr
            for _index, item in selected_group
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
            candidates: list[tuple[int, dict]] = []
            for index, item in selected_group:
                copyable_fields = item.get("copyable_fields", [])
                candidate = candidate_field(item, attr)
                if isinstance(copyable_fields, list) and attr in copyable_fields and candidate:
                    candidates.append((index, candidate))
            if not candidates:
                continue

            # Preserve a deterministic editable value when OCR variants
            # disagree; the visible warning still requires checking it.
            _best_index, source = max(
                candidates,
                key=lambda pair: (candidate_score(pair[1]), -pair[0]),
            )
            row.value = source["value"].strip()
            row.confidence = min(candidate_score(source), LOW_CONFIDENCE - 0.01)
            row.warning = "Кандидат OCR; проверьте по оригиналу перед подтверждением."
            row.evidence_status = "uncertain"
            row.source_variant = source.get("source_variant")
            row.source_start = source.get("source_start")
            row.source_end = source.get("source_end")
            row.source_excerpt = source.get("source_excerpt", "") or ""
            row.source_page = source.get("source_page")
            row.source_boxes = source.get("source_boxes", [])
            row.source_value = row.value
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
