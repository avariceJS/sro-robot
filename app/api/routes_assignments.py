"""Internal debtor cards and resumable appointment drafts."""

from __future__ import annotations

import secrets
from pathlib import Path
from urllib.parse import urlencode

from sro_robot.core.models import confidence_at_least

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, literal, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from ..db.base import get_session
from ..db.models import (
    AssignmentDraft, AssignmentDraftRevision, CasePackage, CasePackageTaskEvent,
    DebtorCardDraft, DebtorCardDraftRevision, Document, IntakeItem, Task,
    TaskStatus, User,
)
from ..services import assignments
from ..services import case_packages
from ..services import tasks, fields
from ..observability import request_id_from_scope
from ..web.templates import templates
from .deps import current_user
from .params import DatabaseId, MAX_DATABASE_ID
from .review_conflicts import review_conflict_response


def _assignment_source_documents(db: Session, source_task_id: int | None) -> list[dict[str, str | int]]:
    """List the explicit source and acts in its currently active case packages."""
    if source_task_id is None:
        return []
    latest_source_membership = (
        select(
            CasePackageTaskEvent.case_package_id,
            func.max(CasePackageTaskEvent.id).label("event_id"),
        )
        .where(CasePackageTaskEvent.task_id == source_task_id)
        .group_by(CasePackageTaskEvent.case_package_id)
        .cte("latest_source_case_membership")
    )
    source_packages = (
        select(CasePackageTaskEvent.case_package_id.label("package_id"))
        .join(latest_source_membership, latest_source_membership.c.event_id == CasePackageTaskEvent.id)
        .where(
            CasePackageTaskEvent.action == "added",
        )
        .cte("active_source_case_packages")
    )
    latest_package_membership = (
        select(
            CasePackageTaskEvent.case_package_id,
            CasePackageTaskEvent.task_id,
            func.max(CasePackageTaskEvent.id).label("event_id"),
        )
        .where(CasePackageTaskEvent.case_package_id.in_(select(source_packages.c.package_id)))
        .group_by(CasePackageTaskEvent.case_package_id, CasePackageTaskEvent.task_id)
        .cte("latest_active_package_membership")
    )
    package_members = (
        select(
            CasePackage.title.label("package_title"),
            Task.id.label("task_id"),
            IntakeItem.filename.label("filename"),
        )
        .select_from(source_packages)
        .join(CasePackage, CasePackage.id == source_packages.c.package_id)
        .join(latest_package_membership, latest_package_membership.c.case_package_id == CasePackage.id)
        .join(CasePackageTaskEvent, CasePackageTaskEvent.id == latest_package_membership.c.event_id)
        .join(Task, Task.id == CasePackageTaskEvent.task_id)
        .join(Document, Document.id == Task.document_id)
        .outerjoin(IntakeItem, IntakeItem.id == Task.intake_item_id)
        .where(CasePackageTaskEvent.action == "added")
    )
    source_task = (
        select(
            literal("Исходное задание").label("package_title"),
            Task.id.label("task_id"),
            IntakeItem.filename.label("filename"),
        )
        .select_from(Task)
        .join(Document, Document.id == Task.document_id)
        .outerjoin(IntakeItem, IntakeItem.id == Task.intake_item_id)
        .where(Task.id == source_task_id)
    )
    rows = db.execute(source_task.union_all(package_members).order_by("task_id", "package_title")).all()
    if not rows:
        return []
    titles_by_task: dict[int, set[str]] = {}
    filenames: dict[int, str] = {}
    for package_title, task_id, filename in rows:
        titles_by_task.setdefault(task_id, set()).add(package_title)
        if filename:
            filenames[task_id] = filename
    return [
        {
            "task_id": task_id,
            "label": (
                f"Задание {task_id} · {', '.join(sorted(titles))} · "
                f"{filenames.get(task_id) or 'имя поступления недоступно'}"
            ),
            "preview_url": f"/tasks/{task_id}/document/preview",
            "download_url": f"/tasks/{task_id}/document",
        }
        for task_id, titles in sorted(titles_by_task.items(), key=lambda item: (item[0] != source_task_id, item[0]))
    ]

router = APIRouter(tags=["internal-work"])
WORKLIST_PAGE_SIZE = 50


def _positive_int(value: str) -> int | None:
    if not value.isascii() or not value.isdigit() or len(value) > 10:
        return None
    parsed = int(value)
    return parsed if 0 < parsed <= MAX_DATABASE_ID else None


def _literal_contains_pattern(value: str) -> str:
    """Build a literal substring pattern for SQL LIKE/ILIKE."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _debtor_identity_search_predicates(pattern: str):
    """Match names and identifiers shared by person, company, and IP cards."""
    return (
        DebtorCardDraft.display_name.ilike(pattern, escape="\\"),
        DebtorCardDraft.inn.ilike(pattern, escape="\\"),
        DebtorCardDraft.values["legal_name"]["value"].astext.ilike(pattern, escape="\\"),
        DebtorCardDraft.values["short_name"]["value"].astext.ilike(pattern, escape="\\"),
        DebtorCardDraft.values["debtor_name"]["value"].astext.ilike(pattern, escape="\\"),
        DebtorCardDraft.values["ogrn"]["value"].astext.ilike(pattern, escape="\\"),
        DebtorCardDraft.values["ogrnip"]["value"].astext.ilike(pattern, escape="\\"),
        DebtorCardDraft.values["kpp"]["value"].astext.ilike(pattern, escape="\\"),
    )


def _worklist_page(request, db, statement, entity, path, filters):
    """Return a stable ID-keyset page; inserts cannot shift the next-page boundary."""
    base = statement.order_by(None)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    pages = max(1, (total + WORKLIST_PAGE_SIZE - 1) // WORKLIST_PAGE_SIZE)
    before_id = _positive_int(request.query_params.get("before", ""))
    after_id = _positive_int(request.query_params.get("after", ""))
    if before_id:
        rows = db.scalars(
            statement.where(entity.id < before_id)
            .order_by(entity.id.desc()).limit(WORKLIST_PAGE_SIZE)
        ).all()
        cursor_page = True
    elif after_id:
        rows = list(db.scalars(
            statement.where(entity.id > after_id)
            .order_by(entity.id.asc()).limit(WORKLIST_PAGE_SIZE)
        ).all())
        rows.reverse()
        cursor_page = True
    else:
        raw_page = _positive_int(request.query_params.get("page", "1")) or 1
        page = min(raw_page, pages)
        rows = db.scalars(
            statement.order_by(entity.id.desc())
            .offset((page - 1) * WORKLIST_PAGE_SIZE).limit(WORKLIST_PAGE_SIZE)
        ).all()
        cursor_page = False

    if cursor_page and rows:
        base_rows = base.subquery()
        preceding = db.scalar(
            select(func.count()).select_from(base_rows).where(base_rows.c.id > rows[0].id)
        ) or 0
        page = min(pages, preceding // WORKLIST_PAGE_SIZE + 1)
    else:
        page = min(_positive_int(request.query_params.get("page", "1")) or 1, pages)

    previous_url = next_url = ""
    if rows and page > 1:
        previous_url = f"{path}?{urlencode({**filters, 'after': max(row.id for row in rows)})}"
    if rows and page < pages:
        next_url = f"{path}?{urlencode({**filters, 'before': min(row.id for row in rows)})}"
    return rows, total, page, pages, previous_url, next_url


def _revision_history_page(
    request, db, *, base, id_column, revision_column, parent_column, parent_id, path,
):
    """Load one immutable revision page, separate from worklist cursors."""
    statement = base.where(parent_column == parent_id)
    total = db.scalar(
        select(func.count(id_column)).where(parent_column == parent_id)
    ) or 0
    pages = max(1, (total + WORKLIST_PAGE_SIZE - 1) // WORKLIST_PAGE_SIZE)
    before = _positive_int(request.query_params.get("revision_before", ""))
    after = _positive_int(request.query_params.get("revision_after", ""))
    if before:
        rows = db.execute(
            statement.where(revision_column < before)
            .order_by(revision_column.desc()).limit(WORKLIST_PAGE_SIZE)
        ).all()
    elif after:
        rows = list(db.execute(
            statement.where(revision_column > after)
            .order_by(revision_column.asc()).limit(WORKLIST_PAGE_SIZE)
        ).all())
        rows.reverse()
    else:
        rows = db.execute(
            statement.order_by(revision_column.desc()).limit(WORKLIST_PAGE_SIZE)
        ).all()

    page = 1
    if (before or after) and rows:
        preceding = db.scalar(
            select(func.count(id_column)).where(
                parent_column == parent_id,
                revision_column > rows[0].revision,
            )
        ) or 0
        page = min(pages, preceding // WORKLIST_PAGE_SIZE + 1)

    previous_url = next_url = ""
    if rows and page > 1:
        previous_url = f"{path}?{urlencode({'revision_after': max(row.revision for row in rows)})}"
    if rows and page < pages:
        next_url = f"{path}?{urlencode({'revision_before': min(row.revision for row in rows)})}"
    return rows, total, page, pages, previous_url, next_url


def _card_revision_page(request, db, card_id):
    base = select(
        DebtorCardDraftRevision.revision,
        DebtorCardDraftRevision.created_at,
        DebtorCardDraftRevision.snapshot["preparation_state"].astext.label("preparation_state"),
        DebtorCardDraftRevision.snapshot["origin"].astext.label("origin"),
        User.full_name,
        User.login,
    ).join(User, User.id == DebtorCardDraftRevision.changed_by_id)
    return _revision_history_page(
        request,
        db,
        base=base,
        id_column=DebtorCardDraftRevision.id,
        revision_column=DebtorCardDraftRevision.revision,
        parent_column=DebtorCardDraftRevision.debtor_card_id,
        parent_id=card_id,
        path=f"/debtors/{card_id}",
    )


def _assignment_revision_page(request, db, assignment_id):
    base = select(
        AssignmentDraftRevision.revision,
        AssignmentDraftRevision.status,
        AssignmentDraftRevision.created_at,
        User.full_name,
        User.login,
    ).join(User, User.id == AssignmentDraftRevision.changed_by_id)
    return _revision_history_page(
        request,
        db,
        base=base,
        id_column=AssignmentDraftRevision.id,
        revision_column=AssignmentDraftRevision.revision,
        parent_column=AssignmentDraftRevision.assignment_draft_id,
        parent_id=assignment_id,
        path=f"/assignments/{assignment_id}",
    )


def _confidence_at_least(field: dict, threshold: float) -> bool:
    return confidence_at_least(field.get("confidence"), threshold)


@router.post("/tasks/{task_id}/approve-internal-card", name="approve_internal_debtor_card")
async def approve_internal_debtor_card(
    request: Request,
    task_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    """Save corrections, attribute approval and persist an internal card atomically."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задание не найдено")
    form = await request.form()
    values = {spec.attr: str(form.get(spec.attr, "") or "") for spec in fields.scenario_of(task).fields}
    try:
        tasks.assert_review_token(db, task, str(form.get("review_token", "")))
    except ValueError as exc:
        if tasks.approved_review_replay_matches(db, task, values, user.id):
            existing_card = db.scalar(
                select(DebtorCardDraft)
                .where(DebtorCardDraft.source_task_id == task_id)
                .with_for_update()
            )
            if existing_card is not None and existing_card.preparation_state == "reviewed":
                db.commit()
                return RedirectResponse(f"/debtors/{existing_card.id}?created=1", status_code=303)
        db.rollback()
        tasks.log_operator_action_not_applied(
            db,
            task,
            action="approve",
            user_id=user.id,
            request_id=request_id_from_scope(request.scope),
        )
        db.commit()
        return review_conflict_response(request, task, user, db, values)
    try:
        tasks.save_values(db, task, values, user.id, request_id=request_id_from_scope(request.scope))
        tasks.approve(db, task, user.id, request_id=request_id_from_scope(request.scope))
        card = assignments.create_debtor_card_from_task(db, task_id, user.id)
        db.commit()
    except assignments.DuplicateDraft:
        # This domain conflict occurs before insertion and leaves the transaction
        # usable. Preserve the employee's corrections and approval for review.
        db.commit()
        return RedirectResponse(f"/tasks/{task_id}?internal_card=duplicate", status_code=303)
    except assignments.DraftNotReady as exc:
        db.rollback()
        tasks.log_operator_action_not_applied(
            db,
            task,
            action="approve",
            user_id=user.id,
            request_id=request_id_from_scope(request.scope),
        )
        db.commit()
        return review_conflict_response(
            request, task, user, db, values,
            message=f"Внутренняя карточка не создана: {exc}. Введённые значения не сохранены.",
        )
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Данные изменились или не готовы. Обновите страницу и проверьте поля") from exc
    return RedirectResponse(f"/debtors/{card.id}?created=1", status_code=303)


@router.get("/debtors", name="debtor_cards_page")
def debtor_cards_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    query = request.query_params.get("q", "").strip()[:120]
    state = request.query_params.get("status", "").strip()
    source_task = None
    raw_source_task_id = request.query_params.get("source_task_id", "")
    source_task_id = _positive_int(raw_source_task_id)
    if source_task_id is not None:
        source_task = db.get(Task, source_task_id)
        if source_task is not None and (
            source_task.status != TaskStatus.approved or not tasks.approval_matches(db, source_task)
        ):
            source_task = None
    statement = select(DebtorCardDraft)
    if state in {"draft", "prepared", "reviewed"}:
        statement = statement.where(DebtorCardDraft.preparation_state == state)
    if source_task is not None:
        statement = statement.where(DebtorCardDraft.preparation_state == "reviewed")
    if query:
        pattern = _literal_contains_pattern(query)
        statement = statement.where(or_(
            *_debtor_identity_search_predicates(pattern),
            DebtorCardDraft.values["case_number"]["value"].astext.ilike(pattern, escape="\\"),
            DebtorCardDraft.values["address"]["value"].astext.ilike(pattern, escape="\\"),
        ))
    filters = {"q": query, "status": state}
    if source_task is not None:
        filters["source_task_id"] = str(source_task.id)
    filters = {key: value for key, value in filters.items() if value}
    cards, total, page, pages, previous_url, next_url = _worklist_page(
        request, db, statement, DebtorCardDraft, "/debtors", filters,
    )
    return templates.TemplateResponse(
        request,
        "debtors.html",
        {"user": user, "cards": cards, "query": query, "selected_state": state, "source_task": source_task,
         "entity_kinds": assignments.ENTITY_KINDS,
         "notice": "Карточка сохранена" if request.query_params.get("created") else "",
         "total": total, "page": page, "pages": pages,
         "previous_url": previous_url, "next_url": next_url},
    )


@router.get("/debtors/new", name="new_debtor_card_page")
def new_debtor_card_page(request: Request, user: User = Depends(current_user)):
    entity_kind = request.query_params.get("entity_kind", "person")
    if entity_kind not in assignments.ENTITY_CARD_FIELDS:
        entity_kind = "person"
    return templates.TemplateResponse(request, "debtor_card_new.html", {
        "user": user, "fields_by_kind": assignments.ENTITY_CARD_FIELDS,
        "entity_kinds": assignments.ENTITY_KINDS, "entity_kind": entity_kind,
        "values": {}, "error": "", "has_pending_values": False,
        "request_key": secrets.token_hex(32),
    })


@router.post("/debtors/new", name="create_manual_debtor_card")
async def create_manual_debtor_card(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    form = await request.form()
    action = str(form.get("action", "cancel"))
    if action == "cancel":
        return RedirectResponse("/debtors", status_code=303)
    if action not in {"draft", "confirm"}:
        raise HTTPException(status_code=422, detail="Неизвестное действие")
    entity_kind = str(form.get("entity_kind", "person"))
    field_names = assignments.ENTITY_CARD_FIELDS.get(entity_kind)
    if field_names is None:
        raise HTTPException(status_code=422, detail="Неизвестный тип должника")
    values = {name: str(form.get(name, "") or "") for name in field_names}
    has_pending_values = any(value.strip() for value in values.values())
    try:
        card = assignments.create_manual_debtor_card_once(
            db, user.id, entity_kind, values, action == "confirm",
            str(form.get("request_key", "")),
        )
        db.commit()
    except assignments.DebtorCardRequestConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except assignments.DuplicateDraft:
        db.rollback()
        return templates.TemplateResponse(request, "debtor_card_new.html", {
            "user": user, "fields_by_kind": assignments.ENTITY_CARD_FIELDS,
            "entity_kinds": assignments.ENTITY_KINDS, "entity_kind": entity_kind, "values": values,
            "has_pending_values": has_pending_values,
            "error": "Карточка с таким ИНН уже существует. Проверьте возможный дубль.",
            "request_key": secrets.token_hex(32),
        }, status_code=409)
    except assignments.DraftNotReady as exc:
        db.rollback()
        return templates.TemplateResponse(request, "debtor_card_new.html", {
            "user": user, "fields_by_kind": assignments.ENTITY_CARD_FIELDS,
            "entity_kinds": assignments.ENTITY_KINDS, "entity_kind": entity_kind, "values": values,
            "error": str(exc), "has_pending_values": has_pending_values,
            "request_key": secrets.token_hex(32),
        }, status_code=422)
    except IntegrityError as exc:
        db.rollback()
        return templates.TemplateResponse(request, "debtor_card_new.html", {
            "user": user, "fields_by_kind": assignments.ENTITY_CARD_FIELDS,
            "entity_kinds": assignments.ENTITY_KINDS, "entity_kind": entity_kind, "values": values,
            "has_pending_values": has_pending_values,
            "error": "Карточка с таким ИНН уже существует. Проверьте возможный дубль.",
            "request_key": secrets.token_hex(32),
        }, status_code=409)
    return RedirectResponse(f"/debtors/{card.id}?saved=1", status_code=303)


@router.post("/tasks/{task_id}/internal-debtor-card", name="create_internal_debtor_card")
def create_internal_debtor_card(
    request: Request,
    task_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    try:
        card = assignments.create_debtor_card_from_task(db, task_id, user.id)
        db.commit()
    except assignments.DuplicateDraft:
        db.rollback()
        return RedirectResponse(f"/tasks/{task_id}?internal_card=duplicate", status_code=303)
    except assignments.DraftConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except assignments.DraftNotReady as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Такая карточка уже создана другим сотрудником") from exc
    return RedirectResponse(f"/debtors/{card.id}?created=1", status_code=303)


@router.get("/debtors/{debtor_id}", name="debtor_card_page")
def debtor_card_page(
    request: Request,
    debtor_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    return _render_debtor_card_page(
        request, debtor_id, user, db,
        error=request.query_params.get("error", ""),
        notice=request.query_params.get("saved", ""),
    )


CARD_REVISION_LABELS = {
    "last_name": "Фамилия",
    "first_name": "Имя",
    "middle_name": "Отчество",
    "inn": "ИНН",
    "snils": "СНИЛС",
    "address": "Адрес",
    "address_kind": "Тип адреса",
    "case_number": "Номер дела",
    "court": "Суд",
    "doc_date": "Дата документа",
    "doc_type": "Тип документа",
    "legal_name": "Полное наименование",
    "short_name": "Сокращённое наименование",
    "legal_form": "Организационная форма",
    "debtor_name": "ФИО предпринимателя",
    "ogrn": "ОГРН",
    "ogrnip": "ОГРНИП",
    "kpp": "КПП",
}


def _revision_field_value(snapshot: dict, field_name: str) -> str:
    values = snapshot.get("values")
    if not isinstance(values, dict):
        return ""
    entry = values.get(field_name)
    if not isinstance(entry, dict):
        return ""
    value = entry.get("value")
    return str(value) if isinstance(value, (str, int, float)) else ""


def _assignment_revision_field_value(snapshot: dict, field_name: str) -> str:
    values = snapshot.get("values")
    if not isinstance(values, dict):
        return ""
    value = values.get(field_name)
    return value if isinstance(value, str) else ""


@router.get(
    "/debtors/{debtor_id}/revisions/{revision}",
    name="debtor_card_revision_page",
)
def debtor_card_revision_page(
    request: Request,
    debtor_id: DatabaseId,
    revision: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    card = db.get(DebtorCardDraft, debtor_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Внутренняя карточка не найдена")
    selected = db.scalar(select(DebtorCardDraftRevision).where(
        DebtorCardDraftRevision.debtor_card_id == debtor_id,
        DebtorCardDraftRevision.revision == revision,
    ))
    if selected is None:
        raise HTTPException(status_code=404, detail="Версия карточки не найдена")
    actor = db.get(User, selected.changed_by_id)
    previous = db.scalar(
        select(DebtorCardDraftRevision)
        .where(
            DebtorCardDraftRevision.debtor_card_id == debtor_id,
            DebtorCardDraftRevision.revision < revision,
        )
        .order_by(DebtorCardDraftRevision.revision.desc())
        .limit(1)
    )
    snapshot = selected.snapshot if isinstance(selected.snapshot, dict) else {}
    previous_snapshot = (
        previous.snapshot if previous is not None and isinstance(previous.snapshot, dict) else {}
    )
    entity_kind = snapshot.get("entity_kind")
    if not isinstance(entity_kind, str) or entity_kind not in assignments.ENTITY_CARD_FIELDS:
        entity_kind = getattr(card, "entity_kind", "person")
    field_names = tuple(
        name for name in assignments.ENTITY_CARD_FIELDS.get(entity_kind, ())
        if name in CARD_REVISION_LABELS
    )
    initial = previous is None
    migration_baseline = snapshot.get("origin") == "migration_baseline"
    changes = []
    for name in field_names:
        current_value = _revision_field_value(snapshot, name)
        previous_value = _revision_field_value(previous_snapshot, name)
        if initial or current_value != previous_value:
            changes.append((CARD_REVISION_LABELS.get(name, name), previous_value, current_value))
    response = templates.TemplateResponse(request, "debtor_card_revision.html", {
        "user": user,
        "card": card,
        "snapshot_display_name": snapshot.get("display_name") or card.display_name,
        "revision": selected,
        "actor": actor,
        "actor_name": (actor.full_name or actor.login) if actor is not None else "Сотрудник не найден",
        "previous_revision": previous,
        "initial": initial,
        "migration_baseline": migration_baseline,
        "changes": changes,
        "preparation_state": snapshot.get("preparation_state", ""),
        "entity_kind_label": assignments.ENTITY_KINDS.get(entity_kind, "Неизвестный тип"),
    })
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.get(
    "/assignments/{assignment_id}/revisions/{revision}",
    name="assignment_revision_page",
)
def assignment_revision_page(
    request: Request,
    assignment_id: DatabaseId,
    revision: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    draft = db.get(AssignmentDraft, assignment_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Черновик назначения не найден")
    selected = db.scalar(select(AssignmentDraftRevision).where(
        AssignmentDraftRevision.assignment_draft_id == assignment_id,
        AssignmentDraftRevision.revision == revision,
    ))
    if selected is None:
        raise HTTPException(status_code=404, detail="Версия назначения не найдена")
    actor = db.get(User, selected.changed_by_id)
    previous = db.scalar(
        select(AssignmentDraftRevision)
        .where(
            AssignmentDraftRevision.assignment_draft_id == assignment_id,
            AssignmentDraftRevision.revision < revision,
        )
        .order_by(AssignmentDraftRevision.revision.desc())
        .limit(1)
    )
    snapshot = selected.snapshot if isinstance(selected.snapshot, dict) else {}
    previous_snapshot = (
        previous.snapshot if previous is not None and isinstance(previous.snapshot, dict) else {}
    )
    initial = previous is None
    changes = []
    previous_case_number = previous_snapshot.get("case_number", "")
    current_case_number = snapshot.get("case_number", "")
    if not isinstance(previous_case_number, str):
        previous_case_number = ""
    if not isinstance(current_case_number, str):
        current_case_number = ""
    if initial or previous_case_number != current_case_number:
        changes.append(("Номер дела", previous_case_number, current_case_number))
    status_labels = assignments.ASSIGNMENT_STATUSES
    previous_status = previous.status if previous is not None else ""
    if initial or previous_status != selected.status:
        changes.append((
            "Состояние внутренней работы",
            status_labels.get(previous_status, previous_status),
            status_labels.get(selected.status, selected.status),
        ))
    for name, label in assignments.ASSIGNMENT_FIELDS:
        current_value = _assignment_revision_field_value(snapshot, name)
        previous_value = _assignment_revision_field_value(previous_snapshot, name)
        if initial or current_value != previous_value:
            changes.append((label, previous_value, current_value))
    response = templates.TemplateResponse(request, "assignment_revision.html", {
        "user": user,
        "draft": draft,
        "revision": selected,
        "actor_name": (actor.full_name or actor.login) if actor is not None else "Сотрудник не найден",
        "previous_revision": previous,
        "initial": initial,
        "changes": changes,
        "statuses": assignments.ASSIGNMENT_STATUSES,
    })
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _render_debtor_card_page(
    request: Request,
    debtor_id: DatabaseId,
    user: User,
    db: Session,
    *,
    error: str = "",
    notice: str = "",
    conflict_values: dict[str, str] | None = None,
    form_values: dict[str, str] | None = None,
    validation_message: str = "",
    person_card_query: str = "",
):
    card = db.get(DebtorCardDraft, debtor_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Внутренняя карточка не найдена")
    card_entity_kind = getattr(card, "entity_kind", "person")
    related_person_card = None
    person_card_choices = []
    person_card_query = person_card_query.strip()[:120]
    if card_entity_kind == "sole_proprietor":
        relation_value = str((card.values.get("related_person_card_id") or {}).get("value") or "")
        related_person_id = _positive_int(relation_value)
        if related_person_id:
            related_person_card = db.get(DebtorCardDraft, related_person_id)
        choices = select(DebtorCardDraft).where(
            DebtorCardDraft.entity_kind == "person",
            DebtorCardDraft.preparation_state == "reviewed",
        )
        if person_card_query:
            pattern = _literal_contains_pattern(person_card_query)
            choices = choices.where(or_(
                DebtorCardDraft.display_name.ilike(pattern, escape="\\"),
                DebtorCardDraft.inn.ilike(pattern, escape="\\"),
            ))
        person_card_choices = db.scalars(
            choices.order_by(DebtorCardDraft.display_name, DebtorCardDraft.id).limit(50)
        ).all()
    (
        assignments_for_card,
        assignment_total,
        assignment_page,
        assignment_pages,
        assignment_previous_url,
        assignment_next_url,
    ) = _worklist_page(
        request,
        db,
        select(AssignmentDraft).where(AssignmentDraft.debtor_card_id == card.id),
        AssignmentDraft,
        f"/debtors/{debtor_id}",
        {},
    )
    (
        revisions,
        revision_total,
        revision_page,
        revision_pages,
        revision_previous_url,
        revision_next_url,
    ) = _card_revision_page(request, db, card.id)
    source_task = db.get(Task, card.source_task_id) if card.source_task_id is not None else None
    source_assignment = (
        db.scalar(select(AssignmentDraft).where(AssignmentDraft.source_task_id == card.source_task_id))
        if card.source_task_id is not None else None
    )
    conflict_rows = []
    if conflict_values is not None:
        labels = {
            "last_name": "Фамилия", "first_name": "Имя", "middle_name": "Отчество",
            "inn": "ИНН", "snils": "СНИЛС", "address": "Адрес",
            "case_number": "Номер дела", "court": "Суд", "doc_date": "Дата документа",
            "doc_type": "Тип документа", "legal_name": "Полное наименование",
            "short_name": "Сокращённое наименование", "ogrn": "ОГРН",
            "ogrnip": "ОГРНИП", "kpp": "КПП",
            "legal_form": "Организационная форма", "debtor_name": "ФИО предпринимателя",
            "address_kind": "Тип адреса",
        }
        for name in assignments.ENTITY_CARD_FIELDS.get(card_entity_kind, ()):
            current = str((card.values.get(name, {}) or {}).get("value") or "")
            submitted = str(conflict_values.get(name, "") or "")
            if current != submitted:
                conflict_rows.append((name, labels.get(name, name), current, submitted))
    return templates.TemplateResponse(
        request,
        "debtor_card.html",
        {
            "user": user,
            "card": card,
            "related_person_card": related_person_card,
            "person_card_choices": person_card_choices,
            "person_card_query": person_card_query,
            "assignments": assignments_for_card,
            "assignment_total": assignment_total,
            "assignment_page": assignment_page,
            "assignment_pages": assignment_pages,
            "assignment_previous_url": assignment_previous_url,
            "assignment_next_url": assignment_next_url,
            "revision_total": revision_total,
            "revision_page": revision_page,
            "revision_pages": revision_pages,
            "revision_previous_url": revision_previous_url,
            "revision_next_url": revision_next_url,
            "source_task": source_task,
            "source_document_available": bool(source_task is not None and source_task.document is not None),
            "source_document_preview_kind": (
                Path(str(source_task.document.stored_path)).suffix.lower()
                if source_task is not None and source_task.document is not None else ""
            ),
            "source_task_approved": bool(
                source_task is not None
                and source_task.status == TaskStatus.approved
                and tasks.approval_matches(db, source_task)
            ),
            "source_assignment": source_assignment,
            "revisions": revisions,
            "fields": assignments.ENTITY_CARD_FIELDS.get(card_entity_kind, ()),
            "entity_fields": assignments.ENTITY_CARD_FIELDS.get(card_entity_kind, ()),
            "entity_kind_label": assignments.ENTITY_KINDS.get(card_entity_kind, "Неизвестный тип"),
            "error": error,
            "notice": notice,
            "conflict_values": conflict_values,
            "conflict_rows": conflict_rows,
            "form_values": form_values or {},
            "form_initially_dirty": form_values is not None or bool(conflict_rows),
            "validation_message": validation_message,
            "assignment_request_key": secrets.token_hex(32),
        },
    )


@router.post("/debtors/{debtor_id}", name="save_debtor_card")
async def save_debtor_card(
    request: Request,
    debtor_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    form = await request.form()
    try:
        version = int(str(form.get("version", "")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Обновите страницу и повторите сохранение") from exc
    card = db.get(DebtorCardDraft, debtor_id)
    field_names = assignments.ENTITY_CARD_FIELDS.get(card.entity_kind, ()) if card is not None else ()
    values = {name: str(form.get(name, "") or "") for name in field_names}
    action = str(form.get("action", "confirm"))
    if action not in {"draft", "confirm"}:
        raise HTTPException(status_code=422, detail="Неизвестное действие")
    try:
        assignments.update_debtor_card(db, debtor_id, user.id, version, values, confirm=action == "confirm")
        db.commit()
    except assignments.DuplicateDraft:
        db.rollback()
        response = _render_debtor_card_page(
            request, debtor_id, user, db, error="duplicate", form_values=values,
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except assignments.DraftConflict as exc:
        db.rollback()
        response = _render_debtor_card_page(
            request, debtor_id, user, db, error="conflict", conflict_values=values,
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except assignments.DraftNotReady as exc:
        db.rollback()
        response = _render_debtor_card_page(
            request, debtor_id, user, db, error="invalid",
            form_values={**values, "version": str(version)},
            validation_message=str(exc),
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except IntegrityError:
        db.rollback()
        response = _render_debtor_card_page(
            request, debtor_id, user, db, error="duplicate", form_values=values,
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response
    return RedirectResponse(f"/debtors/{debtor_id}?saved=1", status_code=303)


@router.post("/debtors/{debtor_id}/related-person", name="set_sole_proprietor_person_link")
async def set_sole_proprietor_person_link(
    request: Request,
    debtor_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    form = await request.form()
    try:
        version = int(str(form.get("version", "")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Обновите страницу и повторите выбор") from exc
    raw_person_id = str(form.get("person_card_id", "")).strip()
    person_card_id = None
    if raw_person_id:
        person_card_id = _positive_int(raw_person_id)
        if person_card_id is None:
            raise HTTPException(status_code=422, detail="Выберите карточку из списка")
    try:
        assignments.set_sole_proprietor_person_link(
            db, debtor_id, user.id, version, person_card_id,
        )
        db.commit()
    except assignments.DraftConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except assignments.DraftNotReady as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f"/debtors/{debtor_id}?saved=1", status_code=303)


@router.post("/debtors/{debtor_id}/related-person/search", name="search_sole_proprietor_person_cards")
async def search_sole_proprietor_person_cards(
    request: Request,
    debtor_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    form = await request.form()
    response = _render_debtor_card_page(
        request, debtor_id, user, db,
        person_card_query=str(form.get("person_q", "") or ""),
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.post("/debtors/{debtor_id}/assignments", name="create_assignment_draft")
async def create_assignment_draft(
    request: Request,
    debtor_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    form = await request.form()
    try:
        draft = assignments.create_assignment_draft_once(
            db, debtor_id, user.id, str(form.get("request_key", "")),
        )
        db.commit()
    except assignments.AssignmentRequestConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except assignments.DraftNotReady as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(f"/assignments/{draft.id}", status_code=303)


@router.post("/tasks/{task_id}/debtors/{debtor_id}/assignments", name="link_assignment_to_debtor")
def link_assignment_to_debtor(
    request: Request,
    task_id: DatabaseId,
    debtor_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    """Create an assignment from an employee-confirmed task and chosen debtor card."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задание не найдено")
    if task.status != TaskStatus.approved or not tasks.approval_matches(db, task):
        raise HTTPException(status_code=409, detail="Сначала проверьте и подтвердите исходное задание")
    recognized = task.recognized or {}
    case_field = recognized.get("case_number") or {}
    court_field = recognized.get("court") or {}
    reviewed_values = task.values or {}
    if "case_number" in reviewed_values:
        source_case = str(reviewed_values.get("case_number") or "").strip()
    else:
        source_case = str(case_field.get("value") or "").strip()
        if case_field.get("warning") or not _confidence_at_least(case_field, 0.7):
            source_case = ""
    source_court = str((task.values or {}).get("court") or "").strip()
    if not source_court and court_field.get("value") and not court_field.get("warning"):
        if _confidence_at_least(court_field, 0.7):
            source_court = str(court_field["value"]).strip()
    try:
        draft = assignments.create_assignment_draft(
            db, debtor_id, user.id, source_task_id=task.id,
            source_case_number=source_case, source_court=source_court,
        )
        db.commit()
    except assignments.DraftConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except assignments.DraftNotReady as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Исходное задание уже связано с другим назначением") from exc
    return RedirectResponse(f"/assignments/{draft.id}?created=1", status_code=303)


@router.get("/assignments", name="assignments_page")
def assignments_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    status = request.query_params.get("status", "").strip()
    query = request.query_params.get("q", "").strip()[:120]
    statement = select(AssignmentDraft).options(
        joinedload(AssignmentDraft.debtor_card),
        joinedload(AssignmentDraft.updated_by),
    )
    if status in assignments.ASSIGNMENT_STATUSES:
        statement = statement.where(AssignmentDraft.status == status)
    if query:
        pattern = _literal_contains_pattern(query)
        statement = statement.join(AssignmentDraft.debtor_card).where(
            or_(AssignmentDraft.case_number.ilike(pattern, escape="\\"),
                AssignmentDraft.values["practitioner"].astext.ilike(pattern, escape="\\"),
                *_debtor_identity_search_predicates(pattern))
        )
    filters = {key: value for key, value in {"q": query, "status": status}.items() if value}
    drafts, total, page, pages, previous_url, next_url = _worklist_page(
        request, db, statement, AssignmentDraft, "/assignments", filters,
    )
    return templates.TemplateResponse(
        request,
        "assignments.html",
        {"user": user, "drafts": drafts, "statuses": assignments.ASSIGNMENT_STATUSES,
         "selected_status": status, "query": query, "total": total,
         "page": page, "pages": pages, "previous_url": previous_url, "next_url": next_url},
    )


@router.get("/assignments/new", name="new_assignment_page")
def new_assignment_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    query = request.query_params.get("q", "").strip()[:120]
    statement = select(DebtorCardDraft).where(DebtorCardDraft.preparation_state == "reviewed")
    if query:
        pattern = _literal_contains_pattern(query)
        statement = statement.where(or_(
            *_debtor_identity_search_predicates(pattern),
            DebtorCardDraft.values["case_number"]["value"].astext.ilike(pattern, escape="\\"),
        ))
    filters = {"q": query} if query else {}
    cards, total, page, pages, previous_url, next_url = _worklist_page(
        request, db, statement, DebtorCardDraft, "/assignments/new", filters,
    )
    return templates.TemplateResponse(request, "assignment_new.html", {
        "user": user, "cards": cards, "query": query,
        "assignment_prefill_previews": {
            card.id: {
                name: assignments.assignment_prefill_preview((card.values or {}).get(name) or {})
                for name in ("case_number", "court")
            }
            for card in cards
        },
        "assignment_request_keys": {card.id: secrets.token_hex(32) for card in cards},
        "total": total, "page": page, "pages": pages,
        "previous_url": previous_url, "next_url": next_url,
    })


@router.post("/assignments/new", name="create_assignment_from_card")
async def create_assignment_from_card(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    form = await request.form()
    debtor_id = _positive_int(str(form.get("debtor_id", "")))
    if debtor_id is None:
        raise HTTPException(status_code=422, detail="Выберите карточку должника")
    try:
        draft = assignments.create_assignment_draft_once(
            db, debtor_id, user.id, str(form.get("request_key", "")),
        )
        db.commit()
    except assignments.AssignmentRequestConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except assignments.DraftNotReady as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Не удалось создать назначение; обновите список карточек") from exc
    return RedirectResponse(f"/assignments/{draft.id}?created=1", status_code=303)


@router.get("/assignments/{assignment_id}", name="assignment_page")
def assignment_page(
    request: Request,
    assignment_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    return _render_assignment_page(
        request, assignment_id, user, db,
        error=request.query_params.get("error", ""),
        notice=request.query_params.get("saved", ""),
    )


@router.get(
    "/assignments/{assignment_id}/case-packages/{package_id}/documents",
    name="assignment_case_package_documents",
)
def assignment_case_package_documents(
    request: Request,
    assignment_id: DatabaseId,
    package_id: DatabaseId,
    task_id: int | None = Query(default=None, gt=0, le=MAX_DATABASE_ID),
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    """Return current exact task membership for an explicitly included package."""
    page_value = _positive_int(request.query_params.get("page", "1")) or 1
    if page_value > 100_000:
        raise HTTPException(status_code=422, detail="Страница списка документов вне допустимых границ")
    result = case_packages.assignment_package_document_page(
        db,
        assignment_id=assignment_id,
        package_id=package_id,
        page=page_value,
        page_size=50,
    )
    if not result["package_exists"]:
        raise HTTPException(status_code=404, detail="Папка дела не найдена")
    if not result["assignment_active"]:
        raise HTTPException(
            status_code=409,
            detail="Назначение больше не включено в эту папку. Обновите карточку назначения.",
        )
    if task_id is not None and not any(item["task_id"] == task_id for item in result["rows"]):
        raise HTTPException(
            status_code=409,
            detail="Задание больше не состоит в выбранной папке или список изменился. Обновите список.",
        )
    documents = [
        {
            **item,
            "label": (
                f"Задание {item['task_id']} · {item['filename']}"
                + (f" · дело {item['case_number']}" if item["case_number"] else "")
            ),
            "preview_supported": item["preview_supported"],
            "preview_url": f"/tasks/{item['task_id']}/document/preview",
            "download_url": f"/tasks/{item['task_id']}/document",
        }
        for item in result["rows"]
    ]
    response = JSONResponse({
        "package_title": result["package_title"],
        "total": result["total"],
        "page": result["page"],
        "page_count": result["page_count"],
        "documents": documents,
    })
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _render_assignment_page(
    request: Request,
    assignment_id: DatabaseId,
    user: User,
    db: Session,
    *,
    error: str = "",
    notice: str = "",
    conflict_values: dict[str, str] | None = None,
    form_values: dict[str, str] | None = None,
    validation_message: str = "",
):
    draft = db.get(AssignmentDraft, assignment_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Черновик назначения не найден")
    source_documents = _assignment_source_documents(db, draft.source_task_id)
    membership_packages = case_packages.active_case_packages_for_assignment(db, draft.id)
    (
        revisions,
        revision_total,
        revision_page,
        revision_pages,
        revision_previous_url,
        revision_next_url,
    ) = _assignment_revision_page(request, db, draft.id)
    conflict_rows = []
    if conflict_values is not None:
        current_values = {
            "case_number": draft.case_number,
            "status": draft.status,
            **{name: str(draft.values.get(name, "") or "") for name, _label in assignments.ASSIGNMENT_FIELDS},
        }
        labels = {"case_number": "Номер дела", "status": "Состояние внутренней работы"}
        labels.update(dict(assignments.ASSIGNMENT_FIELDS))
        for name, current in current_values.items():
            submitted = str(conflict_values.get(name, "") or "")
            if current != submitted:
                conflict_rows.append((name, labels[name], current, submitted))
    return templates.TemplateResponse(
        request,
        "assignment.html",
        {
            "user": user,
            "draft": draft,
            "source_documents": source_documents,
            "membership_packages": membership_packages,
            "fields": assignments.ASSIGNMENT_FIELDS,
            "statuses": assignments.ASSIGNMENT_STATUSES,
            "entity_kinds": assignments.ENTITY_KINDS,
            "revisions": revisions,
            "revision_total": revision_total,
            "revision_page": revision_page,
            "revision_pages": revision_pages,
            "revision_previous_url": revision_previous_url,
            "revision_next_url": revision_next_url,
            "error": error,
            "notice": notice,
            "conflict_values": conflict_values,
            "conflict_rows": conflict_rows,
            "form_values": form_values or {},
            "form_initially_dirty": form_values is not None or bool(conflict_rows),
            "validation_message": validation_message,
        },
    )


@router.post("/assignments/{assignment_id}", name="save_assignment_draft")
async def save_assignment_draft(
    request: Request,
    assignment_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    form = await request.form()
    try:
        version = int(str(form.get("version", "")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Обновите страницу и повторите сохранение") from exc
    values = {name: str(form.get(name, "") or "") for name, _label in assignments.ASSIGNMENT_FIELDS}
    try:
        assignments.update_assignment_draft(
            db,
            assignment_id,
            user.id,
            version,
            str(form.get("case_number", "")),
            values,
            str(form.get("status", "draft")),
        )
        db.commit()
    except assignments.DraftConflict as exc:
        db.rollback()
        submitted_values = {
            **values,
            "case_number": str(form.get("case_number", "")),
            "status": str(form.get("status", "draft")),
        }
        response = _render_assignment_page(
            request, assignment_id, user, db,
            error="conflict", conflict_values=submitted_values,
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except assignments.DraftNotReady as exc:
        db.rollback()
        submitted_values = {
            **values,
            "case_number": str(form.get("case_number", "")),
            "status": str(form.get("status", "draft")),
            "version": str(version),
        }
        response = _render_assignment_page(
            request, assignment_id, user, db,
            error="invalid", form_values=submitted_values,
            validation_message=str(exc),
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response
    return RedirectResponse(f"/assignments/{assignment_id}?saved=1", status_code=303)
