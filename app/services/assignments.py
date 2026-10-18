"""Internal debtor-card and assignment-draft workflows.

These records are SRO Robot work products. Nothing in this module opens an
SRO browser or creates an external SROArbitr record.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import hmac
import json
import re

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from sro_robot.core.models import card_to_fields
from sro_robot.core.validators import (
    digits,
    format_snils,
    is_digit_presentation,
    valid_inn,
    valid_ogrn,
    valid_ogrnip,
    valid_snils,
)

from ..db.models import (
    AssignmentCreateRequest,
    AssignmentDraft,
    DebtorCardCreateRequest,
    DebtorCardDraft,
    Task,
    TaskStatus,
)
from .fields import card_of
from . import tasks as task_service

ASSIGNMENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("arbitration_court", "Арбитражный суд"),
    ("incoming_number", "Входящий номер"),
    ("case_date", "Дата дела"),
    ("judge_name", "Судья (ФИО)"),
    ("judge_code", "Шифр судьи"),
    ("procedure_type", "Процедура — значение справочника СРО"),
    ("debtor_category", "Категория должника — значение справочника СРО"),
    ("debtor_kind", "Вид должника — значение справочника СРО"),
    ("appointment_type", "Тип назначения — значение справочника СРО"),
    ("applicant", "Заявитель"),
    ("applicant_address", "Адрес заявителя"),
    ("practitioner", "Арбитражный управляющий"),
    ("creditors", "Кредиторы"),
    ("annotation", "Аннотация"),
    ("comments", "Комментарии"),
    ("asset_type", "Тип активов — значение справочника СРО"),
    ("assets_description", "Описание активов"),
    ("additional_insurance", "Требуется ли дополнительное страхование"),
    ("outgoing_number", "Исходящий номер"),
    ("protocols", "Протоколы"),
    ("refusal_archive_publication", "Отказ / архив / публикация"),
    ("next_hearing", "Следующее заседание"),
    ("procedure_date", "Ранее введённая дата процедуры (требует уточнения)"),
    ("practitioner_appointment_date", "Дата назначения АУ"),
    ("procedure_termination_date", "Дата прекращения процедуры"),
    ("procedure_completion_date", "Дата завершения процедуры"),
    ("creditors_meeting_date", "Дата собрания кредиторов"),
    ("practitioner_release_date", "Дата освобождения или отстранения АУ"),
    ("suspension_dates", "Период приостановки назначения"),
    ("suspension_reason", "Причина приостановки"),
    ("practitioner_remuneration", "Вознаграждение АУ (руб./месяц, срок, процент, максимум)"),
    ("attracted_persons", "Привлечённые лица"),
    ("archive", "В архиве — внутреннее значение, СРО не обновляется"),
    ("publication", "Публикация — внутреннее значение, СРО не обновляется"),
)
ASSIGNMENT_STATUSES = {
    "draft": "Черновик",
    "awaiting_information": "Ожидает информацию",
    "ready_for_review": "Готово к проверке",
}
MAX_FIELD_CHARACTERS = 2000
MAX_DISPLAY_NAME_CHARACTERS = 512
MAX_CASE_NUMBER_CHARACTERS = 64
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
ENTITY_KINDS = {
    "person": "Физическое лицо",
    "legal_entity": "Юридическое лицо",
    "sole_proprietor": "Индивидуальный предприниматель",
}
ENTITY_CARD_FIELDS = {
    "person": DEBTOR_CARD_FIELDS,
    # These fields are limited to values visible in the read-only register and
    # the existing casework contract. Card-only requisites remain unverified.
    "legal_entity": (
        "legal_name", "short_name", "legal_form", "inn", "ogrn", "kpp", "address", "address_kind", "case_number", "court", "doc_date", "doc_type",
    ),
    "sole_proprietor": (
        "debtor_name", "inn", "ogrnip", "address", "address_kind", "case_number", "court", "doc_date", "doc_type",
    ),
}


def _validated_registry_id(entity_kind: str, field_name: str, value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if not is_digit_presentation(raw):
        labels = {"ogrn": "ОГРН", "ogrnip": "ОГРНИП", "kpp": "КПП"}
        raise DraftNotReady(f"Проверьте формат {labels[field_name]}")
    normalized = digits(raw)
    if entity_kind == "legal_entity" and field_name == "ogrn":
        valid = valid_ogrn(normalized)
        expected_length = 13
    elif entity_kind == "legal_entity" and field_name == "kpp":
        valid = len(normalized) == 9
        expected_length = 9
    elif entity_kind == "sole_proprietor" and field_name == "ogrnip":
        valid = valid_ogrnip(normalized)
        expected_length = 15
    else:
        return raw
    if len(normalized) != expected_length or not valid:
        labels = {"ogrn": "ОГРН", "ogrnip": "ОГРНИП", "kpp": "КПП"}
        if field_name in ("ogrn", "ogrnip"):
            raise DraftNotReady(f"Проверьте длину и контрольную цифру {labels[field_name]}")
        raise DraftNotReady(f"Проверьте формат {labels[field_name]}")
    return normalized


def _validated_person_snils(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if not is_digit_presentation(raw):
        raise DraftNotReady("Укажите СНИЛС только цифрами или оставьте поле пустым")
    normalized = digits(raw)
    if len(normalized) != 11 or not valid_snils(normalized):
        raise DraftNotReady("СНИЛС должен содержать 11 цифр и пройти проверку контрольной суммы")
    return format_snils(normalized)


class DraftConflict(ValueError):
    """A duplicate or stale draft needs an explicit employee decision."""


class DuplicateDraft(DraftConflict):
    """An identifier matches a separate internal debtor card."""


class AssignmentRequestConflict(DraftConflict):
    """An idempotency key was replayed for a different employee action."""


class DebtorCardRequestConflict(DraftConflict):
    """A manual-card idempotency key was replayed with different input."""


class DraftNotReady(ValueError):
    """The source task is not an approved physical-person review."""


def _checked_display_name(value: str) -> str:
    name = str(value or "").strip()
    if len(name) > MAX_DISPLAY_NAME_CHARACTERS:
        raise DraftNotReady(
            f"Наименование карточки не должно превышать {MAX_DISPLAY_NAME_CHARACTERS} символов"
        )
    return name


def _checked_case_number(value: str) -> str:
    case_number = str(value or "").strip()
    if len(case_number) > MAX_CASE_NUMBER_CHARACTERS:
        raise DraftNotReady(
            f"Номер дела не должен превышать {MAX_CASE_NUMBER_CHARACTERS} символа"
        )
    return case_number


PREPARATION_KEY = "_internal_preparation_v1"


def _lock_inn(db: Session, inn: str | None) -> None:
    if inn:
        lock_id = int.from_bytes(hashlib.sha256(("internal-card:" + inn).encode()).digest()[:8],
                                 "big", signed=True)
        db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})


def _ensure_unique_registry_id(
    db: Session,
    entity_kind: str,
    field_name: str,
    value: str | None,
    *,
    exclude_card_id: int | None = None,
) -> None:
    """Serialize and reject duplicate validated OGRN/OGRNIP identities."""
    if not value:
        return
    lock_material = f"internal-card-registry:{entity_kind}:{field_name}:{value}"
    lock_id = int.from_bytes(
        hashlib.sha256(lock_material.encode()).digest()[:8], "big", signed=True,
    )
    db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})
    identity_column = DebtorCardDraft.ogrn if field_name == "ogrn" else DebtorCardDraft.ogrnip
    statement = select(DebtorCardDraft).where(
        DebtorCardDraft.entity_kind == entity_kind,
        identity_column == value,
    )
    if exclude_card_id is not None:
        statement = statement.where(DebtorCardDraft.id != exclude_card_id)
    if db.scalar(statement) is not None:
        raise DuplicateDraft("possible_registry_duplicate")


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


def assignment_prefill_preview(field: dict) -> dict[str, str]:
    """Describe exactly what assignment creation will copy from a card field."""
    if _field_has_reliable_value(field):
        return {"value": str(field["value"]).strip(), "state": "confirmed"}
    if isinstance(field, dict) and str(field.get("value") or "").strip():
        return {"value": "", "state": "review"}
    return {"value": "", "state": "missing"}


def automatic_preparation_reason(card) -> str | None:
    """A checksum or OCR confidence alone never establishes a participant role."""
    if len(str(getattr(card, "fio", "") or "").strip()) > MAX_DISPLAY_NAME_CHARACTERS:
        return "display_name_too_long"
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
        duplicate = db.scalar(select(DebtorCardDraft).where(
            DebtorCardDraft.entity_kind == "person",
            DebtorCardDraft.inn == inn,
        ))
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
    """Persist one reviewed typed debtor card, idempotently by source task."""
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
        task.scenario not in ("person_card", "entity_review")
        or task.status != TaskStatus.approved
        or not task_service.approval_matches(db, task)
    ):
        raise DraftNotReady("Сначала проверьте и подтвердите данные должника")

    card = card_of(task)
    entity_kind = "person"
    if task.scenario == "entity_review":
        entity_kind = str(card.entity_kind.value or "")
        if entity_kind not in ("legal_entity", "sole_proprietor"):
            raise DraftNotReady("Выберите тип должника после сверки с исходным документом")
        display_name = str(
            (card.legal_name.value if entity_kind == "legal_entity" else card.debtor_name.value) or ""
        ).strip()
        if not display_name:
            raise DraftNotReady("Укажите наименование организации или ФИО предпринимателя")
        display_name = _checked_display_name(display_name)
    else:
        if not card.last_name.value or not card.first_name.value:
            raise DraftNotReady("Не хватает имени для внутренней карточки")
        display_name = _checked_display_name(card.fio)
        card.snils.value = _validated_person_snils(card.snils.value) or None
    card.case_number.value = _checked_case_number(card.case_number.value)
    if entity_kind in ("legal_entity", "sole_proprietor"):
        if card.address_kind.value not in (None, "", "registration", "postal", "actual"):
            raise DraftNotReady("Проверьте тип адреса по исходному документу")
        registry_fields = ("ogrn", "kpp") if entity_kind == "legal_entity" else ("ogrnip",)
        for field_name in registry_fields:
            field = getattr(card, field_name)
            field.value = _validated_registry_id(entity_kind, field_name, field.value or "") or None
    if existing_source is not None and existing_source.preparation_state == "reviewed":
        current = card_to_fields(card)
        if any(
            str((existing_source.values.get(name) or {}).get("value") or "").strip()
            != str((current.get(name) or {}).get("value") or "").strip()
            for name in ENTITY_CARD_FIELDS[entity_kind]
        ) or getattr(existing_source, "entity_kind", "person") != entity_kind:
            raise DraftConflict("Карточка уже проверена с другими данными. Откройте её и проверьте исправления")
        return existing_source
    raw_inn_value = str(card.inn.value or "").strip()
    if raw_inn_value and not is_digit_presentation(raw_inn_value):
        raise DraftNotReady("Укажите ИНН только цифрами или оставьте поле пустым")
    raw_inn = digits(raw_inn_value)
    expected_inn_length = 10 if entity_kind == "legal_entity" else 12
    if raw_inn and (len(raw_inn) != expected_inn_length or len(set(raw_inn)) < 2 or not valid_inn(raw_inn)):
        raise DraftNotReady(f"Укажите корректный {expected_inn_length}-значный ИНН или оставьте поле пустым")
    inn = raw_inn if valid_inn(raw_inn) else None
    _lock_inn(db, inn)
    registry_field = {"legal_entity": "ogrn", "sole_proprietor": "ogrnip"}.get(entity_kind)
    if registry_field is not None:
        registry_value = str((card_to_fields(card).get(registry_field) or {}).get("value") or "")
        _ensure_unique_registry_id(
            db, entity_kind, registry_field, registry_value,
            exclude_card_id=existing_source.id if existing_source is not None else None,
        )
    if inn:
        duplicate = db.scalar(
            select(DebtorCardDraft).where(
                DebtorCardDraft.entity_kind == entity_kind,
                DebtorCardDraft.inn == inn,
                DebtorCardDraft.source_task_id != task_id,
            )
        )
        if duplicate is not None:
            raise DuplicateDraft("possible_inn_duplicate")

    if existing_source is not None:
        if existing_source.version != 1:
            raise DraftConflict("Карточка уже исправлена; откройте её для дальнейшей проверки")
        updated_fields = card_to_fields(card)
        existing_source.values = updated_fields
        existing_source.display_name = display_name
        existing_source.inn = inn
        existing_source.ogrn = str((updated_fields.get("ogrn") or {}).get("value") or "") or None
        existing_source.ogrnip = str((updated_fields.get("ogrnip") or {}).get("value") or "") or None
        existing_source.preparation_state = "reviewed"
        existing_source.updated_by_id = user_id
        existing_source.version += 1
        db.flush()
        return existing_source

    card_fields = card_to_fields(card)
    debtor = DebtorCardDraft(
        entity_kind=entity_kind,
        display_name=display_name,
        inn=inn,
        ogrn=str((card_fields.get("ogrn") or {}).get("value") or "") or None,
        ogrnip=str((card_fields.get("ogrnip") or {}).get("value") or "") or None,
        preparation_state="reviewed",
        values=card_fields,
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
        # The route performs an early approval check for its response, but a
        # concurrent rejection or field edit may commit while this call waits
        # for the source-task lock. Recheck the current approval before creating
        # a new assignment from that source.
        if source_task.status != TaskStatus.approved or not task_service.approval_matches(db, source_task):
            raise DraftNotReady("Сначала проверьте и подтвердите исходное задание")
    source_fields = debtor.values or {}
    case_field = source_fields.get("case_number") or {}
    court_field = source_fields.get("court") or {}
    case_number = assignment_prefill_preview(case_field)["value"]
    court = assignment_prefill_preview(court_field)["value"]
    if source_case_number is not None:
        case_number = source_case_number.strip()
    if source_court is not None:
        court = source_court.strip()
    case_number = _checked_case_number(case_number)
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

    entity_kind = getattr(debtor, "entity_kind", "person")
    field_names = ENTITY_CARD_FIELDS.get(entity_kind)
    if field_names is None:
        raise DraftNotReady("Неизвестный тип внутренней карточки")

    updated = dict(debtor.values or {})
    for name in field_names:
        value = (values.get(name) or "").strip()
        if len(value) > MAX_FIELD_CHARACTERS:
            raise DraftNotReady("Одно из полей превышает 2000 символов")
        if name == "case_number":
            value = _checked_case_number(value)
        field = dict(updated.get(name) or {})
        if str(field.get("value") or "").strip() == value and field:
            continue
        if not field and not value:
            continue
        if name == "address_kind" and value not in {"", "registration", "postal", "actual"}:
            raise DraftNotReady("Выберите подтверждённый тип адреса")
        if field.get("value") != value:
            for source_key in ("source_variant", "source_start", "source_end", "source_excerpt"):
                field.pop(source_key, None)
        field.update(value=value, source="employee", warning=None, confidence=1.0)
        updated[name] = field

    if entity_kind == "person":
        last_name = str(updated.get("last_name", {}).get("value") or "").strip()
        first_name = str(updated.get("first_name", {}).get("value") or "").strip()
        middle_name = str(updated.get("middle_name", {}).get("value") or "").strip()
        if confirm and (not last_name or not first_name):
            raise DraftNotReady("Фамилия и имя обязательны для карточки физического лица")
        display_name = " ".join(part for part in (last_name, first_name, middle_name) if part)
    elif entity_kind == "legal_entity":
        display_name = str(updated.get("legal_name", {}).get("value") or "").strip()
        if confirm and not display_name:
            raise DraftNotReady("Укажите наименование юридического лица")
    else:
        display_name = str(updated.get("debtor_name", {}).get("value") or "").strip()
        if confirm and not display_name:
            raise DraftNotReady("Укажите ФИО индивидуального предпринимателя")

    inn_value = str(updated.get("inn", {}).get("value") or "").strip()
    if inn_value and not is_digit_presentation(inn_value):
        raise DraftNotReady("Укажите ИНН только цифрами или оставьте поле пустым")
    inn_digits = digits(inn_value)
    expected_inn_length = 10 if entity_kind == "legal_entity" else 12
    if inn_digits and (len(inn_digits) != expected_inn_length or not valid_inn(inn_digits)):
        if entity_kind == "legal_entity":
            raise DraftNotReady("ИНН юридического лица должен содержать 10 цифр и пройти проверку контрольной суммы")
        raise DraftNotReady("ИНН должен содержать 12 цифр и пройти проверку контрольной суммы")
    normalized_inn = inn_digits or None
    inn_field = dict(updated.get("inn") or {})
    if inn_field and inn_field.get("value") != (inn_digits or ""):
        inn_field["value"] = inn_digits or ""
        updated["inn"] = inn_field
    if entity_kind == "person":
        snils_field = dict(updated.get("snils") or {})
        snils = _validated_person_snils(str(snils_field.get("value") or ""))
        if snils_field and snils_field.get("value") != snils:
            snils_field["value"] = snils
            updated["snils"] = snils_field
    if entity_kind in ("legal_entity", "sole_proprietor"):
        registry_fields = ("ogrn", "kpp") if entity_kind == "legal_entity" else ("ogrnip",)
        for field_name in registry_fields:
            field = dict(updated.get(field_name) or {})
            field_value = _validated_registry_id(
                entity_kind, field_name, str(field.get("value") or ""),
            )
            if field_value:
                field["value"] = field_value
                updated[field_name] = field
    display_name = _checked_display_name(display_name or "Новая карточка")
    next_state = "reviewed" if confirm else "draft"
    unchanged = (
        updated == debtor.values
        and normalized_inn == debtor.inn
        and display_name == debtor.display_name
        and getattr(debtor, "preparation_state", "reviewed") == next_state
    )
    if debtor.version != expected_version:
        # A response may be lost after commit. Accept only an exact replay of
        # the normalized card state; never let stale input overwrite newer data.
        if expected_version < debtor.version and unchanged:
            return debtor
        raise DraftConflict("Карточку уже изменил другой сотрудник; обновите страницу")
    if unchanged:
        return debtor
    _lock_inn(db, normalized_inn)
    if normalized_inn and normalized_inn != debtor.inn:
        duplicate = db.scalar(
            select(DebtorCardDraft).where(
                DebtorCardDraft.entity_kind == entity_kind,
                DebtorCardDraft.inn == normalized_inn,
                DebtorCardDraft.id != debtor.id,
            )
        )
        if duplicate is not None:
            raise DuplicateDraft("possible_inn_duplicate")
    registry_field = {"legal_entity": "ogrn", "sole_proprietor": "ogrnip"}.get(entity_kind)
    if registry_field is not None:
        registry_value = str((updated.get(registry_field) or {}).get("value") or "")
        _ensure_unique_registry_id(
            db, entity_kind, registry_field, registry_value, exclude_card_id=debtor.id,
        )
    debtor.values = updated
    debtor.inn = normalized_inn
    debtor.ogrn = str((updated.get("ogrn") or {}).get("value") or "") or None
    debtor.ogrnip = str((updated.get("ogrnip") or {}).get("value") or "") or None
    debtor.display_name = display_name
    debtor.updated_by_id = user_id
    debtor.preparation_state = next_state
    debtor.version += 1
    db.flush()
    return debtor


def set_sole_proprietor_person_link(
    db: Session,
    debtor_id: int,
    user_id: int,
    expected_version: int,
    person_card_id: int | None,
) -> DebtorCardDraft:
    """Link an IP card to an explicitly selected, reviewed person card.

    No identifier/name matching is performed. The selection is kept inside
    the existing revisioned values document so every change has an actor and
    immutable card snapshot.
    """
    debtor = db.scalar(
        select(DebtorCardDraft)
        .where(DebtorCardDraft.id == debtor_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if debtor is None:
        raise DraftNotReady("Внутренняя карточка должника не найдена")
    if debtor.entity_kind != "sole_proprietor" or debtor.preparation_state != "reviewed":
        raise DraftNotReady("Связь доступна только для подтверждённой карточки ИП")
    person = None
    if person_card_id is not None:
        # Serialize link selection with concurrent edits to the selected person
        # card. Refresh after any lock wait before relying on its reviewed state.
        person = db.scalar(
            select(DebtorCardDraft)
            .where(DebtorCardDraft.id == person_card_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if person is None or person.entity_kind != "person" or person.preparation_state != "reviewed":
            raise DraftNotReady("Выберите подтверждённую карточку физического лица")
    values = dict(debtor.values or {})
    current = str((values.get("related_person_card_id") or {}).get("value") or "")
    selected = str(person.id) if person is not None else ""
    if current == selected:
        if expected_version <= debtor.version:
            return debtor
        raise DraftConflict("Карточка уже изменилась; обновите страницу")
    if debtor.version != expected_version:
        raise DraftConflict("Карточка уже изменилась; обновите страницу")
    if selected:
        values["related_person_card_id"] = {
            "value": selected, "source": "employee", "confidence": 1.0,
        }
    else:
        values.pop("related_person_card_id", None)
    debtor.values = values
    debtor.updated_by_id = user_id
    debtor.version += 1
    db.flush()
    return debtor


def create_manual_debtor_card(db: Session, user_id: int, entity_kind: str = "person") -> DebtorCardDraft:
    """Create a resumable internal shell; no SRO record is opened or written."""
    if entity_kind not in ENTITY_CARD_FIELDS:
        raise DraftNotReady("Неизвестный тип внутренней карточки")
    debtor = DebtorCardDraft(
        entity_kind=entity_kind,
        display_name="Новая карточка",
        inn=None,
        preparation_state="draft",
        values={name: {"value": "", "source": "employee", "confidence": 1.0}
                for name in ENTITY_CARD_FIELDS[entity_kind]},
        source_task_id=None,
        created_by_id=user_id,
        updated_by_id=user_id,
    )
    db.add(debtor)
    db.flush()
    return debtor


def create_manual_debtor_card_once(
    db: Session,
    user_id: int,
    entity_kind: str,
    values: dict[str, str],
    confirm: bool,
    request_key: str,
) -> DebtorCardDraft:
    """Create or replay one manual card submission using a durable browser key."""
    if not re.fullmatch(r"[0-9a-f]{64}", request_key or ""):
        raise DebtorCardRequestConflict("Ключ действия недействителен. Откройте форму создания заново")
    if entity_kind not in ENTITY_CARD_FIELDS:
        raise DraftNotReady("Неизвестный тип внутренней карточки")
    canonical_payload = json.dumps(
        {
            "entity_kind": entity_kind,
            "confirm": bool(confirm),
            "values": {name: str(values.get(name) or "") for name in ENTITY_CARD_FIELDS[entity_kind]},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    request_hash = hashlib.sha256(bytes.fromhex(request_key)).hexdigest()
    payload_hash = hmac.new(
        bytes.fromhex(request_key), canonical_payload, hashlib.sha256,
    ).hexdigest()
    claim = db.execute(
        pg_insert(DebtorCardCreateRequest)
        .values(request_hash=request_hash, payload_hash=payload_hash, user_id=user_id)
        .on_conflict_do_nothing(index_elements=[DebtorCardCreateRequest.request_hash])
        .returning(DebtorCardCreateRequest.request_hash)
    ).scalar_one_or_none()
    if claim is None:
        receipt = db.get(DebtorCardCreateRequest, request_hash)
        if receipt is None or receipt.user_id != user_id or receipt.payload_hash != payload_hash:
            raise DebtorCardRequestConflict("Этот ключ уже использован для другого действия")
        existing = db.get(DebtorCardDraft, receipt.debtor_card_id) if receipt.debtor_card_id else None
        if existing is None:
            raise DebtorCardRequestConflict("Результат предыдущего действия недоступен; проверьте список карточек")
        return existing

    card = create_manual_debtor_card(db, user_id, entity_kind)
    update_debtor_card(db, card.id, user_id, card.version, values, confirm=confirm)
    receipt = db.get(DebtorCardCreateRequest, request_hash)
    receipt.debtor_card_id = card.id
    db.flush()
    return card


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
    if status not in ASSIGNMENT_STATUSES:
        raise DraftNotReady("Недопустимый статус черновика")
    normalized_case = _checked_case_number(case_number)
    normalized_values = {}
    for name, _label in ASSIGNMENT_FIELDS:
        value = (values.get(name) or "").strip()
        if len(value) > MAX_FIELD_CHARACTERS:
            raise DraftNotReady("Одно из полей превышает 2000 символов")
        normalized_values[name] = value

    unchanged = (
        assignment.case_number == normalized_case
        and assignment.values == normalized_values
        and assignment.status == status
    )
    if assignment.version != expected_version:
        # A response may be lost after the commit. Treat a stale replay as
        # successful only when the requested full state is already present.
        if expected_version < assignment.version and unchanged:
            return assignment
        raise DraftConflict("Черновик уже изменён другим сотрудником; обновите страницу")
    if unchanged:
        return assignment
    assignment.case_number = normalized_case
    assignment.values = normalized_values
    assignment.status = status
    assignment.updated_by_id = user_id
    assignment.version += 1
    db.flush()
    return assignment
