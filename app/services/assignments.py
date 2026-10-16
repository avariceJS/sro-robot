"""Internal debtor-card and assignment-draft workflows.

These records are SRO Robot work products. Nothing in this module opens an
SRO browser or creates an external SROArbitr record.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import re

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from sro_robot.core.models import card_to_fields
from sro_robot.core.validators import digits, valid_inn

from ..db.models import (
    AssignmentCreateRequest,
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


class AssignmentRequestConflict(DraftConflict):
    """An idempotency key was replayed for a different employee action."""


class DraftNotReady(ValueError):
    """The source task is not an approved physical-person review."""


PREPARATION_KEY = "_internal_preparation_v1"


def _lock_inn(db: Session, inn: str | None) -> None:
    if inn:
        lock_id = int.from_bytes(hashlib.sha256(("internal-card:" + inn).encode()).digest()[:8],
                                 "big", signed=True)
        db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})


def _field_has_reliable_value(field: dict) -> bool:
    if not isinstance(field, dict):
        return False
    if not field.get("value") or field.get("warning"):
        return False
    if field.get("source") == "employee":
        return True
    try:
        return float(field.get("confidence") or 0) >= 0.7
    except (TypeError, ValueError):
        return False


def automatic_preparation_reason(card) -> str | None:
    """A checksum or OCR confidence alone never establishes a participant role."""
    if card.fio_raw.source != "debtor_role:v1":
        return "debtor_role_uncertain"
    for name in ("last_name", "first_name", "middle_name", "inn", "snils", "address", "address_kind"):
        field = getattr(card, name)
        if field.value and field.source != "debtor_role:v1":
            return "field_role_uncertain"
    for name in ("last_name", "first_name", "middle_name", "inn", "case_number", "doc_date"):
        field = getattr(card, name)
        if not field.value or field.warning or field.confidence < 0.7:
            return "required_fields_uncertain"
    inn = card.inn.value or ""
    if (len(inn) != 12 or not inn.isascii() or not inn.isdigit()
            or len(set(inn)) < 2 or not valid_inn(inn)):
        return "person_inn_uncertain"
    if not re.fullmatch(r"А\d{1,2}-\d{3,7}/\d{4}", card.case_number.value or ""):
        return "case_uncertain"
    try:
        datetime.strptime(card.doc_date.value, "%d.%m.%Y")
    except ValueError:
        return "date_uncertain"
    for name in (*DEBTOR_CARD_FIELDS, "address_kind"):
        field = getattr(card, name)
        if field.warning and (field.value or field.source in ("role_ambiguous", "role_partial")):
            return "field_uncertain"
    return None


def prepare_debtor_card_from_ocr(db: Session, task: Task, card) -> DebtorCardDraft | None:
    """Prepare only a role-bound internal candidate; never approve or merge people.

    Caller holds the OCR task lock/fence. The employee is the intake initiator,
    while preparation_state explicitly identifies automatic system preparation.
    """
    if getattr(task, "scenario", None) != "person_card":
        return None
    reason = automatic_preparation_reason(card)
    metadata = {"state": "needs_review", "reason": reason or ""}
    if card.fio_raw.source != "debtor_role:v1":
        task.recognized = {**(task.recognized or {}), PREPARATION_KEY: metadata}
        return None
    if reason is None and task.assignee_id is not None:
        existing = db.scalar(select(DebtorCardDraft).where(DebtorCardDraft.source_task_id == task.id))
        if existing is not None:
            return existing
        inn = card.inn.value
        _lock_inn(db, inn)
        duplicate = db.scalar(select(DebtorCardDraft).where(DebtorCardDraft.inn == inn))
        if duplicate is not None:
            metadata.update(reason="possible_duplicate", possible_card_id=duplicate.id)
        else:
            prepared = DebtorCardDraft(
                entity_kind="person", display_name=card.fio, inn=inn,
                preparation_state="prepared", values=card_to_fields(card),
                source_task_id=task.id, created_by_id=task.assignee_id,
                updated_by_id=task.assignee_id,
            )
            db.add(prepared)
            db.flush()
            metadata.update(state="prepared", card_id=prepared.id)
            task.recognized = {**task.recognized, PREPARATION_KEY: metadata}
            return prepared
    task.recognized = {**task.recognized, PREPARATION_KEY: metadata}
    return None


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
        .with_for_update().execution_options(populate_existing=True)
    )
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
    if existing_source is not None and existing_source.preparation_state == "reviewed":
        current = card_to_fields(card)
        if any(
            str((existing_source.values.get(name) or {}).get("value") or "").strip()
            != str((current.get(name) or {}).get("value") or "").strip()
            for name in (*DEBTOR_CARD_FIELDS, "address_kind")
        ):
            raise DraftConflict("Карточка уже проверена с другими данными. Откройте её и проверьте исправления")
        return existing_source
    raw_inn = digits(card.inn.value or "")
    if raw_inn and (len(raw_inn) != 12 or len(set(raw_inn)) < 2 or not valid_inn(raw_inn)):
        raise DraftNotReady("Укажите корректный 12-значный ИНН физического лица или оставьте поле пустым")
    inn = raw_inn if valid_inn(raw_inn) else None
    _lock_inn(db, inn)
    if inn:
        duplicate = db.scalar(
            select(DebtorCardDraft).where(DebtorCardDraft.inn == inn, DebtorCardDraft.source_task_id != task_id)
        )
        if duplicate is not None:
            raise DuplicateDraft("possible_inn_duplicate")

    if existing_source is not None:
        if existing_source.version != 1:
            raise DraftConflict("Карточка уже исправлена; откройте её для дальнейшей проверки")
        existing_source.values = card_to_fields(card)
        existing_source.display_name = display_name
        existing_source.inn = inn
        existing_source.preparation_state = "reviewed"
        existing_source.updated_by_id = user_id
        existing_source.version += 1
        db.flush()
        return existing_source

    debtor = DebtorCardDraft(
        entity_kind="person",
        display_name=display_name,
        inn=inn,
        preparation_state="reviewed",
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
    source_task_id: int | None = None,
    source_case_number: str | None = None,
    source_court: str | None = None,
) -> AssignmentDraft:
    source_task = None
    if source_task_id is not None:
        # Always acquire source identity before debtor-card rows. OCR approval
        # and manual linking share this order, preventing a task/card lock cycle.
        source_task = db.scalar(
            select(Task)
            .where(Task.id == source_task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if source_task is None:
            raise DraftNotReady("Исходное судебное задание не найдено")
    debtor = db.scalar(
        select(DebtorCardDraft)
        .where(DebtorCardDraft.id == debtor_card_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if debtor is None:
        raise DraftNotReady("Внутренняя карточка должника не найдена")
    if debtor.preparation_state != "reviewed":
        raise DraftNotReady("Сначала проверьте и сохраните внутреннюю карточку должника")
    if source_task_id is not None:
        # Serialize concurrent staff/worker retries by the exact source task.
        # The same debtor and case can have independent assignments from other
        # source tasks, so neither is used as a deduplication key.
        existing = db.scalar(
            select(AssignmentDraft).where(AssignmentDraft.source_task_id == source_task_id)
        )
        if existing is not None:
            if existing.debtor_card_id != debtor.id:
                raise DraftConflict("Источник документа уже связан с другой карточкой должника")
            return existing
    source_fields = debtor.values or {}
    case_field = source_fields.get("case_number") or {}
    court_field = source_fields.get("court") or {}
    case_number = str(case_field.get("value") or "").strip()
    court = str(court_field.get("value") or "").strip()
    if not _field_has_reliable_value(case_field):
        case_number = ""
    if not _field_has_reliable_value(court_field):
        court = ""
    if source_case_number is not None:
        case_number = source_case_number.strip()
    if source_court is not None:
        court = source_court.strip()
    initial_values = {name: "" for name, _label in ASSIGNMENT_FIELDS}
    initial_values["arbitration_court"] = court
    assignment = AssignmentDraft(
        debtor_card_id=debtor.id,
        source_task_id=source_task_id,
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


def create_assignment_draft_once(
    db: Session,
    debtor_id: int,
    user_id: int,
    request_key: str,
) -> AssignmentDraft:
    """Create one independent assignment per opaque browser action key.

    The receipt and draft are committed by the caller in one transaction. A
    retry of the same request returns its original draft, while a fresh form
    receives a fresh key and can create another assignment for the same debtor
    and case.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", request_key or ""):
        raise AssignmentRequestConflict("Ключ действия недействителен. Откройте форму создания заново")
    request_hash = hashlib.sha256(bytes.fromhex(request_key)).hexdigest()
    claim = db.execute(
        pg_insert(AssignmentCreateRequest)
        .values(request_hash=request_hash, user_id=user_id, debtor_card_id=debtor_id)
        .on_conflict_do_nothing(index_elements=[AssignmentCreateRequest.request_hash])
        .returning(AssignmentCreateRequest.request_hash)
    ).scalar_one_or_none()
    if claim is None:
        receipt = db.get(AssignmentCreateRequest, request_hash)
        if (
            receipt is None
            or receipt.user_id != user_id
            or receipt.debtor_card_id != debtor_id
            or receipt.assignment_id is None
        ):
            raise AssignmentRequestConflict("Этот ключ уже использован для другого действия")
        existing = db.get(AssignmentDraft, receipt.assignment_id)
        if existing is None:
            raise AssignmentRequestConflict("Результат предыдущего действия недоступен; проверьте список назначений")
        return existing

    assignment = create_assignment_draft(db, debtor_id, user_id)
    receipt = db.get(AssignmentCreateRequest, request_hash)
    receipt.assignment_id = assignment.id
    db.flush()
    return assignment


def update_debtor_card(
    db: Session,
    debtor_id: int,
    user_id: int,
    expected_version: int,
    values: dict[str, str],
    confirm: bool = True,
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
    if confirm and (not last_name or not first_name):
        raise DraftNotReady("Фамилия и имя обязательны для карточки физического лица")
    inn_digits = digits(str(updated.get("inn", {}).get("value") or ""))
    if inn_digits and (len(inn_digits) != 12 or len(set(inn_digits)) < 2 or not valid_inn(inn_digits)):
        raise DraftNotReady("ИНН не прошёл проверку контрольной суммы")
    normalized_inn = inn_digits or None
    _lock_inn(db, normalized_inn)
    if normalized_inn and normalized_inn != debtor.inn:
        duplicate = db.scalar(
            select(DebtorCardDraft).where(
                DebtorCardDraft.inn == normalized_inn,
                DebtorCardDraft.id != debtor.id,
            )
        )
        if duplicate is not None:
            raise DuplicateDraft("possible_inn_duplicate")
    display_name = " ".join(part for part in (last_name, first_name, middle_name) if part) or "Новая карточка"
    next_state = "reviewed" if confirm else "draft"
    if (updated == debtor.values and normalized_inn == debtor.inn
            and display_name == debtor.display_name
            and getattr(debtor, "preparation_state", "reviewed") == next_state):
        return debtor
    debtor.values = updated
    debtor.inn = normalized_inn
    debtor.display_name = display_name
    debtor.updated_by_id = user_id
    debtor.preparation_state = next_state
    debtor.version += 1
    db.flush()
    return debtor


def create_manual_debtor_card(db: Session, user_id: int) -> DebtorCardDraft:
    """Create a resumable internal shell; no SRO record is opened or written."""
    debtor = DebtorCardDraft(
        entity_kind="person",
        display_name="Новая карточка",
        inn=None,
        preparation_state="draft",
        values={name: {"value": "", "source": "employee", "confidence": 1.0}
                for name in DEBTOR_CARD_FIELDS},
        source_task_id=None,
        created_by_id=user_id,
        updated_by_id=user_id,
    )
    db.add(debtor)
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
    if len(normalized_case) > 64:
        raise DraftNotReady("Номер дела не должен превышать 64 символа")
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
