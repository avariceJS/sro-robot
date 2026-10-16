"""Internal debtor cards and resumable appointment drafts."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db.base import get_session
from ..db.models import AssignmentDraft, AssignmentDraftRevision, DebtorCardDraft, DebtorCardDraftRevision, Task, User
from ..services import assignments
from ..services import tasks, fields
from ..observability import request_id_from_scope
from ..web.templates import templates
from .deps import current_user

router = APIRouter(tags=["internal-work"])


@router.post("/tasks/{task_id}/approve-internal-card", name="approve_internal_debtor_card")
async def approve_internal_debtor_card(
    request: Request,
    task_id: int,
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
        tasks.save_values(db, task, values, user.id, request_id=request_id_from_scope(request.scope))
        tasks.approve(db, task, user.id, request_id=request_id_from_scope(request.scope))
        card = assignments.create_debtor_card_from_task(db, task_id, user.id)
        db.commit()
    except assignments.DuplicateDraft:
        # This domain conflict occurs before insertion and leaves the transaction
        # usable. Preserve the employee's corrections and approval for review.
        db.commit()
        return RedirectResponse(f"/tasks/{task_id}?internal_card=duplicate", status_code=303)
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
    cards = db.scalars(
        select(DebtorCardDraft).order_by(DebtorCardDraft.updated_at.desc(), DebtorCardDraft.id.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "debtors.html",
        {"user": user, "cards": cards, "notice": "Карточка сохранена" if request.query_params.get("created") else ""},
    )


@router.post("/tasks/{task_id}/internal-debtor-card", name="create_internal_debtor_card")
def create_internal_debtor_card(
    request: Request,
    task_id: int,
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
    debtor_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    card = db.get(DebtorCardDraft, debtor_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Внутренняя карточка не найдена")
    assignments_for_card = db.scalars(
        select(AssignmentDraft)
        .where(AssignmentDraft.debtor_card_id == card.id)
        .order_by(AssignmentDraft.updated_at.desc(), AssignmentDraft.id.desc())
    ).all()
    revisions = db.execute(
        select(DebtorCardDraftRevision, User.full_name, User.login)
        .join(User, User.id == DebtorCardDraftRevision.changed_by_id)
        .where(DebtorCardDraftRevision.debtor_card_id == card.id)
        .order_by(DebtorCardDraftRevision.revision.desc()).limit(50)
    ).all()
    return templates.TemplateResponse(
        request,
        "debtor_card.html",
        {
            "user": user,
            "card": card,
            "assignments": assignments_for_card,
            "revisions": revisions,
            "fields": assignments.DEBTOR_CARD_FIELDS,
            "error": request.query_params.get("error", ""),
            "notice": request.query_params.get("saved", ""),
        },
    )


@router.post("/debtors/{debtor_id}", name="save_debtor_card")
async def save_debtor_card(
    request: Request,
    debtor_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    form = await request.form()
    try:
        version = int(str(form.get("version", "")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Обновите страницу и повторите сохранение") from exc
    values = {name: str(form.get(name, "") or "") for name in assignments.DEBTOR_CARD_FIELDS}
    try:
        assignments.update_debtor_card(db, debtor_id, user.id, version, values)
        db.commit()
    except assignments.DuplicateDraft:
        db.rollback()
        return RedirectResponse(f"/debtors/{debtor_id}?error=duplicate", status_code=303)
    except assignments.DraftConflict as exc:
        db.rollback()
        return RedirectResponse(f"/debtors/{debtor_id}?error=conflict", status_code=303)
    except assignments.DraftNotReady as exc:
        db.rollback()
        return RedirectResponse(f"/debtors/{debtor_id}?error=invalid", status_code=303)
    except IntegrityError as exc:
        db.rollback()
        return RedirectResponse(f"/debtors/{debtor_id}?error=duplicate", status_code=303)
    return RedirectResponse(f"/debtors/{debtor_id}?saved=1", status_code=303)


@router.post("/debtors/{debtor_id}/assignments", name="create_assignment_draft")
def create_assignment_draft(
    request: Request,
    debtor_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    try:
        draft = assignments.create_assignment_draft(db, debtor_id, user.id)
        db.commit()
    except assignments.DraftNotReady as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(f"/assignments/{draft.id}", status_code=303)


@router.get("/assignments", name="assignments_page")
def assignments_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    drafts = db.scalars(
        select(AssignmentDraft)
        .order_by(AssignmentDraft.updated_at.desc(), AssignmentDraft.id.desc())
    ).all()
    return templates.TemplateResponse(request, "assignments.html", {"user": user, "drafts": drafts})


@router.get("/assignments/{assignment_id}", name="assignment_page")
def assignment_page(
    request: Request,
    assignment_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    draft = db.get(AssignmentDraft, assignment_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Черновик назначения не найден")
    revisions = db.scalars(
        select(AssignmentDraftRevision)
        .where(AssignmentDraftRevision.assignment_draft_id == draft.id)
        .order_by(AssignmentDraftRevision.revision.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "assignment.html",
        {
            "user": user,
            "draft": draft,
            "fields": assignments.ASSIGNMENT_FIELDS,
            "statuses": assignments.ASSIGNMENT_STATUSES,
            "revisions": revisions,
            "error": request.query_params.get("error", ""),
            "notice": request.query_params.get("saved", ""),
        },
    )


@router.post("/assignments/{assignment_id}", name="save_assignment_draft")
async def save_assignment_draft(
    request: Request,
    assignment_id: int,
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
        return RedirectResponse(f"/assignments/{assignment_id}?error=conflict", status_code=303)
    except assignments.DraftNotReady as exc:
        db.rollback()
        return RedirectResponse(f"/assignments/{assignment_id}?error=invalid", status_code=303)
    return RedirectResponse(f"/assignments/{assignment_id}?saved=1", status_code=303)
