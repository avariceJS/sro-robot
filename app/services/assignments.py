"""Internal debtor-card and assignment-draft workflows.

These records are SRO Robot work products. Nothing in this module opens an
SRO browser or creates an external SROArbitr record.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from sro_robot.core.models import card_to_fields
from sro_robot.core.validators import digits, valid_inn

from ..db.models import (
    AssignmentDraft,
    DebtorCardDraft,
    Task,
    TaskStatus,
)
from .fields import card_of
from . import tasks as task_service

ASSIGNMENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("arbitration_court", "Арбитражный суд"),
    ("incoming_number", "Входящий номер"),
    ("applicant", "Заявитель"),
    ("applicant_address", "Адрес заявителя"),
    ("practitioner", "Арбитражный управляющий"),
    ("comments", "Комментарии"),
    ("outgoing_number", "Исходящий номер"),
    ("protocols", "Протоколы"),
    ("refusal_archive_publication", "Отказ / архив / публикация"),
    ("next_hearing", "Следующее заседание"),
    ("procedure_date", "Дата процедуры"),
    ("practitioner_appointment_date", "Дата назначения управляющего"),
)
ASSIGNMENT_STATUSES = {
    "draft": "Черновик",
    "awaiting_information": "Ожидает информацию",
    "ready_for_review": "Готово к проверке",
}
MAX_FIELD_CHARACTERS = 2000
DEBTOR_CARD_FIELDS = (
    "last_name",
    "first_name",
    "middle_name",
    "inn",
    "snils",
    "address",
    "case_number",
    "court",
    "doc_date",
    "doc_type",
)


class DraftConflict(ValueError):
    """A duplicate or stale draft needs an explicit employee decision."""


class DuplicateDraft(DraftConflict):
    """An identifier matches a separate internal debtor card."""


class DraftNotReady(ValueError):
    """The source task is not an approved physical-person review."""


def create_debtor_card_from_task(
    db: Session,
    task_id: int,
    user_id: int,
) -> DebtorCardDraft:
    """Persist one reviewed physical-person card, idempotently by source task."""
    task = db.scalar(
        select(Task)
        .where(Task.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None:
        raise DraftNotReady("Задание не найдено")
    existing_source = db.scalar(
        select(DebtorCardDraft).where(DebtorCardDraft.source_task_id == task_id)
    )
    if existing_source is not None:
        return existing_source
    if (
        task.scenario != "person_card"
        or task.status != TaskStatus.approved
        or not task_service.approval_matches(db, task)
    ):
        raise DraftNotReady("Сначала проверьте и подтвердите данные физического лица")

    card = card_of(task)
    if not card.last_name.value or not card.first_name.value:
        raise DraftNotReady("Не хватает имени для внутренней карточки")
    display_name = card.fio.strip()
    raw_inn = digits(card.inn.value or "")
    inn = raw_inn if valid_inn(raw_inn) else None
    if inn:
        duplicate = db.scalar(
            select(DebtorCardDraft).where(DebtorCardDraft.inn == inn)
        )
        if duplicate is not None:
            raise DuplicateDraft("possible_inn_duplicate")

    debtor = DebtorCardDraft(
        entity_kind="person",
        display_name=display_name,
        inn=inn,
        values=card_to_fields(card),
        source_task_id=task.id,
        created_by_id=user_id,
        updated_by_id=user_id,
    )
    db.add(debtor)
    db.flush()
    return debtor


def create_assignment_draft(
    db: Session,
    debtor_card_id: int,
    user_id: int,
) -> AssignmentDraft:
    debtor = db.get(DebtorCardDraft, debtor_card_id)
    if debtor is None:
        raise DraftNotReady("Внутренняя карточка должника не найдена")
    source_fields = debtor.values or {}
    case_number = str((source_fields.get("case_number") or {}).get("value") or "").strip()
    court = str((source_fields.get("court") or {}).get("value") or "").strip()
    initial_values = {name: "" for name, _label in ASSIGNMENT_FIELDS}
    initial_values["arbitration_court"] = court
    assignment = AssignmentDraft(
        debtor_card_id=debtor.id,
        case_number=case_number,
        status="draft",
        values=initial_values,
        version=1,
        created_by_id=user_id,
        updated_by_id=user_id,
    )
    db.add(assignment)
    db.flush()
    return assignment


def update_debtor_card(
    db: Session,
    debtor_id: int,
    user_id: int,
    expected_version: int,
    values: dict[str, str],
) -> DebtorCardDraft:
    """Save employee corrections with stale-edit and duplicate guards."""
    debtor = db.scalar(
        select(DebtorCardDraft)
        .where(DebtorCardDraft.id == debtor_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if debtor is None:
        raise DraftNotReady("Внутренняя карточка должника не найдена")
    if debtor.version != expected_version:
        raise DraftConflict("Карточку уже изменил другой сотрудник; обновите страницу")

    updated = dict(debtor.values or {})
    for name in DEBTOR_CARD_FIELDS:
        value = (values.get(name) or "").strip()
        if len(value) > MAX_FIELD_CHARACTERS:
            raise DraftNotReady("Одно из полей превышает 2000 символов")
        field = dict(updated.get(name) or {})
        if str(field.get("value") or "").strip() == value and field:
            continue
        if not field and not value:
            continue
        field.update(value=value, source="employee", warning=None, confidence=1.0)
        updated[name] = field

    last_name = str(updated.get("last_name", {}).get("value") or "").strip()
    first_name = str(updated.get("first_name", {}).get("value") or "").strip()
    middle_name = str(updated.get("middle_name", {}).get("value") or "").strip()
    if not last_name or not first_name:
        raise DraftNotReady("Фамилия и имя обязательны для карточки физического лица")
    inn_digits = digits(str(updated.get("inn", {}).get("value") or ""))
    if inn_digits and not valid_inn(inn_digits):
        raise DraftNotReady("ИНН не прошёл проверку контрольной суммы")
    normalized_inn = inn_digits or None
    if normalized_inn and normalized_inn != debtor.inn:
        duplicate = db.scalar(
            select(DebtorCardDraft).where(
                DebtorCardDraft.inn == normalized_inn,
                DebtorCardDraft.id != debtor.id,
            )
        )
        if duplicate is not None:
            raise DuplicateDraft("possible_inn_duplicate")
    display_name = " ".join(part for part in (last_name, first_name, middle_name) if part)
    if updated == debtor.values and normalized_inn == debtor.inn and display_name == debtor.display_name:
        return debtor
    debtor.values = updated
    debtor.inn = normalized_inn
    debtor.display_name = display_name
    debtor.updated_by_id = user_id
    debtor.version += 1
    db.flush()
    return debtor


def update_assignment_draft(
    db: Session,
    assignment_id: int,
    user_id: int,
    expected_version: int,
    case_number: str,
    values: dict[str, str],
    status: str,
) -> AssignmentDraft:
    assignment = db.scalar(
        select(AssignmentDraft)
        .where(AssignmentDraft.id == assignment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if assignment is None:
        raise DraftNotReady("Черновик назначения не найден")
    if assignment.version != expected_version:
        raise DraftConflict("Черновик уже изменён другим сотрудником; обновите страницу")
    if status not in ASSIGNMENT_STATUSES:
        raise DraftNotReady("Недопустимый статус черновика")
    normalized_case = (case_number or "").strip()
    if not normalized_case or len(normalized_case) > 64:
        raise DraftNotReady("Укажите номер дела (не более 64 символов)")
    normalized_values = {}
    for name, _label in ASSIGNMENT_FIELDS:
        value = (values.get(name) or "").strip()
        if len(value) > MAX_FIELD_CHARACTERS:
            raise DraftNotReady("Одно из полей превышает 2000 символов")
        normalized_values[name] = value

    if (
        assignment.case_number == normalized_case
        and assignment.values == normalized_values
        and assignment.status == status
    ):
        return assignment
    assignment.case_number = normalized_case
    assignment.values = normalized_values
    assignment.status = status
    assignment.updated_by_id = user_id
    assignment.version += 1
    db.flush()
    return assignment
