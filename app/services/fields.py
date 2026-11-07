"""Мост между контрактом сценария и экраном проверки.

Интерфейс не знает названий полей: он спрашивает их у сценария. Поэтому
карточка организации отрисуется тем же шаблоном, что и карточка физлица.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import TYPE_CHECKING

from sro_robot.core.models import Field, card_from_fields, confidence_at_least
from sro_robot.core.validators import valid_inn
from sro_robot.scenarios import Scenario, get_scenario

if TYPE_CHECKING:
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


def review_fields_with_occurrence(task: Task) -> tuple[list[ReviewField], tuple[int, dict] | None]:
    """Return displayed fields and the exact OCR occurrence selected for them."""
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

    def reviewable_attrs(item: dict) -> set[str]:
        copyable = item.get("copyable_fields", [])
        attrs = {attr for attr in copyable if isinstance(attr, str)} if isinstance(copyable, list) else set()
        address = candidate_field(item, "address")
        kind = candidate_field(item, "address_kind")
        # The parser already retains short, explicitly labeled city addresses.
        # Show these in employee review even in previously processed documents;
        # keep authoritative extraction and automatic preparation unchanged.
        if address and kind and kind["value"] in {"registration", "actual"} and re.fullmatch(
            r"(?i:город|г\.)\s+[А-ЯЁ][А-ЯЁа-яё-]*(?:\s+[А-ЯЁ][А-ЯЁа-яё-]*)*\.?",
            address["value"].strip(),
        ):
            attrs.update(("address", "address_kind"))
        return attrs

    selected_occurrence_ref: tuple[int, dict] | None = None
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
                return rows, None
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

        # Keep the existing cross-variant identity ranking, then choose one
        # coherent occurrence. A repeated FIO can refer to different people;
        # field-by-field selection across that group can combine their IDs and
        # addresses into a record that never appeared in the source.
        selected_group = max(groups.values(), key=group_score)

        def normalized_address(value: str) -> str:
            return " ".join(value.split()).casefold()

        # Preserve the existing uniquely compatible apartment completion only
        # when every other available person attribute agrees. The chosen row
        # still comes from one occurrence; this preference never merges fields.
        other_values: dict[str, set[str]] = {}
        for _index, item in selected_group:
            for attr in reviewable_attrs(item):
                if attr == "address":
                    continue
                candidate = candidate_field(item, attr)
                if candidate:
                    other_values.setdefault(attr, set()).add(candidate["value"].strip().casefold())
        compatible_address_values: set[str] = set()
        if all(len(values) <= 1 for values in other_values.values()):
            address_candidates = [
                (index, candidate_field(item, "address"))
                for index, item in selected_group
                if "address" in reviewable_attrs(item) and candidate_field(item, "address")
            ]
            values = {
                normalized_address(candidate["value"])
                for _index, candidate in address_candidates
            }
            complete = {
                value for value in values
                if all(other == value or (
                    value.startswith(other)
                    and re.fullmatch(
                        r"\s*,?\s*кв(?:артира)?\.?\s*№?\s*\d+[а-яёa-z]?(?:[/\-]\d+)?",
                        value[len(other):], re.I,
                    ) is not None
                ) for other in values)
            }
            if len(values) > 1 and len(complete) == 1:
                compatible_address_values = complete

        candidate_occurrences = selected_group
        if compatible_address_values:
            preferred_occurrences = [
                (index, item) for index, item in selected_group
                if (candidate := candidate_field(item, "address")) is not None
                and normalized_address(candidate["value"]) in compatible_address_values
            ]
            if preferred_occurrences:
                candidate_occurrences = preferred_occurrences

        def occurrence_score(pair: tuple[int, dict]) -> tuple[int, float, int, int, int]:
            index, item = pair
            available = [
                candidate_field(item, attr)
                for attr in sorted(reviewable_attrs(item))
                if attr in rows_by_attr and candidate_field(item, attr)
            ]
            available_attrs = {
                attr for attr in reviewable_attrs(item)
                if attr in rows_by_attr and candidate_field(item, attr)
            }
            completeness = len(available_attrs)
            if "address" in available_attrs and "address_kind" in available_attrs:
                # Address kind describes the address occurrence; it is not
                # independent person evidence when comparing candidates.
                completeness -= 1
            confidence = (
                sum(candidate_score(candidate) for candidate in available) / len(available)
                if available else 0.0
            )
            name_field = next(
                (candidate_field(item, attr) for attr in identity_attrs
                 if candidate_field(item, attr)),
                {},
            )
            variant = name_field.get("source_variant")
            variant = variant if isinstance(variant, int) and not isinstance(variant, bool) else 10**9
            start = name_field.get("source_start")
            start = start if isinstance(start, int) and not isinstance(start, bool) else index
            # Prefer more complete occurrences, then stronger evidence, then
            # earlier OCR variant/document position and stable parser order.
            return completeness, confidence, -variant, -start, -index

        _selected_index, selected_occurrence = max(candidate_occurrences, key=occurrence_score)
        selected_occurrence_ref = (_selected_index, selected_occurrence)
        attrs = set(reviewable_attrs(selected_occurrence))
        # A previously saved operator value may have come from another OCR
        # variant. Include its attr only for the exact-value provenance path
        # below; new or blank fields still come solely from the selected item.
        for attr, row in rows_by_attr.items():
            saved_candidate = (
                bool(row.value) and attr in operator_values and not row.source_boxes
                and row.source_variant is None and row.source_start is None and row.source_end is None
            )
            if saved_candidate and any(
                attr in reviewable_attrs(item) for _index, item in selected_group
            ):
                attrs.add(attr)
        for attr in attrs:
            row = rows_by_attr.get(attr)
            if row is None:
                continue
            saved_candidate = (
                bool(row.value) and attr in operator_values and not row.source_boxes
                and row.source_variant is None and row.source_start is None and row.source_end is None
            )
            if (row.value and not saved_candidate) or (not row.value and attr in operator_values):
                continue
            source = candidate_field(selected_occurrence, attr)
            if saved_candidate:
                # Restoring provenance for a saved employee value is safe
                # only when every occurrence carrying this attr agrees on the
                # exact value. This retains matching source arrows across OCR
                # variants without assigning geometry from a conflicting
                # same-name person.
                values_for_attr = {
                    candidate["value"].strip()
                    for _index, item in selected_group
                    if attr in reviewable_attrs(item)
                    and (candidate := candidate_field(item, attr)) is not None
                }
                matches = [
                    candidate for _index, item in selected_group
                    if attr in reviewable_attrs(item)
                    and (candidate := candidate_field(item, attr)) is not None
                    and candidate["value"].strip() == row.value
                ]
                if len(values_for_attr) == 1 and row.value in values_for_attr and matches:
                    source = max(matches, key=candidate_score)
                else:
                    source = None
            if source is None or (saved_candidate and source["value"].strip() != row.value):
                continue
            # Saving an unchanged review candidate creates an operator field.
            # Restore its matching source only, preserving the employee's value
            # and review status instead of treating it as a new OCR suggestion.
            if not saved_candidate:
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
    return rows, selected_occurrence_ref


def review_fields(task: Task) -> list[ReviewField]:
    return review_fields_with_occurrence(task)[0]


def alternate_person_inn(task: Task, rows: list[ReviewField]) -> dict | None:
    """Expose an OCR INN for explicit employee copy, never merge occurrences."""
    if task.scenario != "person_card":
        return None
    by_attr = {row.attr: row for row in rows}
    if by_attr.get("inn") is None or by_attr["inn"].value:
        return None
    identity = tuple((by_attr.get(attr).value if by_attr.get(attr) else "").strip().casefold()
                     for attr in ("last_name", "first_name", "middle_name"))
    if not all(identity):
        return None
    candidates = []
    foreign_inn = False
    for suggestion in (task.recognized or {}).get("_review_suggestions_v1", []):
        if not isinstance(suggestion, dict) or suggestion.get("kind") != "application_person":
            continue
        fields = suggestion.get("fields") or {}
        if not isinstance(fields, dict):
            continue
        inn = fields.get("inn") or {}
        value = inn.get("value") if isinstance(inn, dict) else None
        if not isinstance(value, str) or not value or not valid_inn(value) or len(value) != 12:
            continue
        name = tuple(str((fields.get(attr) or {}).get("value") or "").strip().casefold()
                     for attr in ("last_name", "first_name", "middle_name"))
        if name != identity:
            foreign_inn = True
            continue
        if "inn" not in (suggestion.get("copyable_fields") or []):
            continue
        if not isinstance(inn.get("source_variant"), int) or not isinstance(inn.get("source_start"), int):
            continue
        if not isinstance(inn.get("source_end"), int) or inn["source_start"] >= inn["source_end"]:
            continue
        candidates.append(inn)
    if foreign_inn or len({item["value"] for item in candidates}) != 1:
        return None
    return next((item for item in candidates if item.get("source_boxes")), candidates[0])


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
