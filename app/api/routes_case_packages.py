"""Explicit multi-act workspaces; no task is linked by case number alone."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from ..db.base import get_session
from ..db.models import (
    AssignmentDraft,
    CasePackageAssignmentEvent,
    CasePackage,
    CasePackageTaskEvent,
    DebtorCardDraft,
    Document,
    IntakeItem,
    TASK_STATUS_RU,
    Task,
    User,
)
from ..services import assignments, case_packages
from ..web.templates import templates
from .deps import current_user
from .params import DatabaseId


router = APIRouter(tags=["case-packages"])
PACKAGE_PAGE_SIZE = 50
CANDIDATE_PAGE_SIZE = 25
HISTORY_PAGE_SIZE = 50


@router.get("/case-packages", name="case_packages_page")
def case_packages_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    query = request.query_params.get("q", "").strip()[:120]
    packages_query = select(CasePackage)
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        packages_query = packages_query.where(or_(
            CasePackage.title.ilike(pattern, escape="\\"),
            CasePackage.case_number.ilike(pattern, escape="\\"),
        ))
    total = db.scalar(
        select(func.count()).select_from(packages_query.order_by(None).subquery())
    ) or 0
    pages = max(1, (total + PACKAGE_PAGE_SIZE - 1) // PACKAGE_PAGE_SIZE)
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    page = min(page, pages)
    packages = db.scalars(
        packages_query
        .options(joinedload(CasePackage.created_by))
        .order_by(CasePackage.id.desc())
        .offset((page - 1) * PACKAGE_PAGE_SIZE)
        .limit(PACKAGE_PAGE_SIZE)
    ).all()
    return templates.TemplateResponse(request, "case_packages.html", {
        "user": user,
        "packages": packages,
        "page": page,
        "pages": pages,
        "total": total,
        "query": query,
        "notice": request.query_params.get("created") == "1",
    })


@router.get("/case-packages/new", name="new_case_package_page")
def new_case_package_page(request: Request, user: User = Depends(current_user)):
    return templates.TemplateResponse(request, "case_package_new.html", {
        "user": user,
        "request_key": secrets.token_hex(32),
        "title": "",
        "case_number": "",
        "error": "",
    })


@router.post("/case-packages", name="create_case_package")
async def create_case_package(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    form = await request.form()
    title = str(form.get("title", ""))
    case_number = str(form.get("case_number", ""))
    try:
        package, _created = case_packages.create_once(
            db,
            user_id=user.id,
            request_key=str(form.get("request_key", "")),
            title=title,
            case_number=case_number,
        )
        db.commit()
    except case_packages.CasePackageConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        return templates.TemplateResponse(request, "case_package_new.html", {
            "user": user,
            "request_key": str(form.get("request_key", "")),
            "title": title,
            "case_number": case_number,
            "error": str(exc),
        }, status_code=422)
    except Exception:
        db.rollback()
        raise
    return RedirectResponse(f"/case-packages/{package.id}?created=1", status_code=303)


@router.get("/case-packages/{package_id}", name="case_package_page")
def case_package_page(
    request: Request,
    package_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    package = db.get(CasePackage, package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="Папка дела не найдена")
    latest_events = case_packages.latest_membership_events(db, package.id)
    members = [event for event in latest_events if event.action == "added"]
    latest_event_ids = {event.task_id: event.id for event in latest_events}
    history_total = db.scalar(
        select(func.count()).select_from(CasePackageTaskEvent).where(
            CasePackageTaskEvent.case_package_id == package.id
        )
    ) or 0
    history_pages = max(1, (history_total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    try:
        history_page = max(1, int(request.query_params.get("history_page", "1")))
    except ValueError:
        history_page = 1
    history_page = min(history_page, history_pages)
    history = case_packages.membership_history_page(
        db,
        package.id,
        offset=(history_page - 1) * HISTORY_PAGE_SIZE,
        limit=HISTORY_PAGE_SIZE,
    )
    latest_assignment_events = case_packages.latest_assignment_membership_events(db, package.id)
    assignment_members = [event for event in latest_assignment_events if event.action == "added"]
    latest_assignment_event_ids = {
        event.assignment_draft_id: event.id for event in latest_assignment_events
    }
    assignment_history_total = db.scalar(
        select(func.count()).select_from(CasePackageAssignmentEvent).where(
            CasePackageAssignmentEvent.case_package_id == package.id
        )
    ) or 0
    assignment_history_pages = max(
        1, (assignment_history_total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE
    )
    try:
        assignment_history_page = max(
            1, int(request.query_params.get("assignment_history_page", "1"))
        )
    except ValueError:
        assignment_history_page = 1
    assignment_history_page = min(assignment_history_page, assignment_history_pages)
    assignment_history = case_packages.assignment_membership_history_page(
        db,
        package.id,
        offset=(assignment_history_page - 1) * HISTORY_PAGE_SIZE,
        limit=HISTORY_PAGE_SIZE,
    )
    task_ids = [event.task_id for event in members]
    cards_by_task = {}
    assignments_by_task = {}
    if task_ids:
        for card in db.scalars(
            select(DebtorCardDraft).where(DebtorCardDraft.source_task_id.in_(task_ids))
        ).all():
            cards_by_task[card.source_task_id] = card
        for assignment in db.scalars(
            select(AssignmentDraft).where(AssignmentDraft.source_task_id.in_(task_ids))
        ).all():
            assignments_by_task[assignment.source_task_id] = assignment

    query = request.query_params.get("q", "").strip()[:120]
    already_added = set(task_ids)
    candidates_query = select(Task).join(Task.document).outerjoin(Task.intake_item)
    if already_added:
        candidates_query = candidates_query.where(Task.id.not_in(already_added))
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        candidates_query = candidates_query.where(or_(
            Task.case_number.ilike(pattern, escape="\\"),
            func.coalesce(IntakeItem.filename, Document.filename).ilike(pattern, escape="\\"),
        ))
    candidate_total = db.scalar(
        select(func.count()).select_from(candidates_query.order_by(None).subquery())
    ) or 0
    candidate_pages = max(1, (candidate_total + CANDIDATE_PAGE_SIZE - 1) // CANDIDATE_PAGE_SIZE)
    try:
        candidate_page = max(1, int(request.query_params.get("tasks_page", "1")))
    except ValueError:
        candidate_page = 1
    candidate_page = min(candidate_page, candidate_pages)
    candidates = db.scalars(
        candidates_query.options(joinedload(Task.document), joinedload(Task.intake_item))
        .order_by(Task.id.desc())
        .offset((candidate_page - 1) * CANDIDATE_PAGE_SIZE)
        .limit(CANDIDATE_PAGE_SIZE)
    ).unique().all()
    assignment_query = request.query_params.get("aq", "").strip()[:120]
    already_added_assignments = {event.assignment_draft_id for event in assignment_members}
    assignment_candidates_query = select(AssignmentDraft).join(
        DebtorCardDraft, DebtorCardDraft.id == AssignmentDraft.debtor_card_id
    )
    if already_added_assignments:
        assignment_candidates_query = assignment_candidates_query.where(
            AssignmentDraft.id.not_in(already_added_assignments)
        )
    if assignment_query:
        if assignment_query.startswith("#") and assignment_query[1:].isascii() and assignment_query[1:].isdigit():
            assignment_candidates_query = assignment_candidates_query.where(
                AssignmentDraft.id == int(assignment_query[1:])
            )
        else:
            escaped = assignment_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            assignment_candidates_query = assignment_candidates_query.where(or_(
                AssignmentDraft.case_number.ilike(pattern, escape="\\"),
                DebtorCardDraft.display_name.ilike(pattern, escape="\\"),
            ))
    assignment_candidate_total = db.scalar(
        select(func.count()).select_from(
            assignment_candidates_query.order_by(None).subquery()
        )
    ) or 0
    assignment_candidate_pages = max(
        1, (assignment_candidate_total + CANDIDATE_PAGE_SIZE - 1) // CANDIDATE_PAGE_SIZE
    )
    try:
        assignment_candidate_page = max(
            1, int(request.query_params.get("assignments_page", "1"))
        )
    except ValueError:
        assignment_candidate_page = 1
    assignment_candidate_page = min(assignment_candidate_page, assignment_candidate_pages)
    assignment_candidates = db.scalars(
        assignment_candidates_query
        .options(joinedload(AssignmentDraft.debtor_card))
        .order_by(AssignmentDraft.id.desc())
        .offset((assignment_candidate_page - 1) * CANDIDATE_PAGE_SIZE)
        .limit(CANDIDATE_PAGE_SIZE)
    ).all()
    return templates.TemplateResponse(request, "case_package.html", {
        "user": user,
        "package": package,
        "members": members,
        "history": history,
        "history_page": history_page,
        "history_pages": history_pages,
        "history_total": history_total,
        "latest_event_ids": latest_event_ids,
        "cards_by_task": cards_by_task,
        "assignments_by_task": assignments_by_task,
        "candidates": candidates,
        "candidate_page": candidate_page,
        "candidate_pages": candidate_pages,
        "candidate_total": candidate_total,
        "query": query,
        "notice": request.query_params.get("notice", ""),
        "task_status_labels": TASK_STATUS_RU,
        "assignment_status_labels": assignments.ASSIGNMENT_STATUSES,
        "assignment_members": assignment_members,
        "assignment_history": assignment_history,
        "assignment_history_page": assignment_history_page,
        "assignment_history_pages": assignment_history_pages,
        "assignment_history_total": assignment_history_total,
        "latest_assignment_event_ids": latest_assignment_event_ids,
        "assignment_query": assignment_query,
        "assignment_candidates": assignment_candidates,
        "assignment_candidate_page": assignment_candidate_page,
        "assignment_candidate_pages": assignment_candidate_pages,
        "assignment_candidate_total": assignment_candidate_total,
    })


def _change_membership(
    package_id: int,
    task_id: int,
    included: bool,
    user: User,
    db: Session,
    expected_event_id: int,
):
    if db.get(Task, task_id) is None:
        raise HTTPException(status_code=404, detail="Судебное задание не найдено")
    try:
        changed = case_packages.set_task_membership(
            db,
            package_id=package_id,
            task_id=task_id,
            actor_id=user.id,
            included=included,
            expected_event_id=expected_event_id,
        )
        db.commit()
    except case_packages.CasePackageConflict as exc:
        db.rollback()
        return RedirectResponse(
            f"/case-packages/{package_id}?notice={str(exc)}", status_code=303
        )
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    notice = "Документ добавлен" if included and changed else (
        "Документ удалён из папки" if changed else "Состав папки уже актуален"
    )
    return RedirectResponse(f"/case-packages/{package_id}?notice={notice}", status_code=303)


@router.post("/case-packages/{package_id}/tasks/{task_id}", name="add_task_to_case_package")
def add_task_to_case_package(
    package_id: DatabaseId,
    task_id: DatabaseId,
    expected_event_id: int = 0,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    return _change_membership(package_id, task_id, True, user, db, expected_event_id)


@router.post("/case-packages/{package_id}/tasks/{task_id}/remove", name="remove_task_from_case_package")
def remove_task_from_case_package(
    package_id: DatabaseId,
    task_id: DatabaseId,
    expected_event_id: int = 0,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    return _change_membership(package_id, task_id, False, user, db, expected_event_id)


def _change_assignment_membership(
    package_id: int,
    assignment_id: int,
    included: bool,
    user: User,
    db: Session,
    expected_event_id: int,
):
    if db.get(AssignmentDraft, assignment_id) is None:
        raise HTTPException(status_code=404, detail="Черновик назначения не найден")
    try:
        changed = case_packages.set_assignment_membership(
            db,
            package_id=package_id,
            assignment_id=assignment_id,
            actor_id=user.id,
            included=included,
            expected_event_id=expected_event_id,
        )
        db.commit()
    except case_packages.CasePackageConflict as exc:
        db.rollback()
        return RedirectResponse(
            f"/case-packages/{package_id}?notice={str(exc)}", status_code=303
        )
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    notice = "Назначение добавлено" if included and changed else (
        "Назначение удалено из папки" if changed else "Состав назначений уже актуален"
    )
    return RedirectResponse(f"/case-packages/{package_id}?notice={notice}", status_code=303)


@router.post(
    "/case-packages/{package_id}/assignments/{assignment_id}",
    name="add_assignment_to_case_package",
)
def add_assignment_to_case_package(
    package_id: DatabaseId,
    assignment_id: DatabaseId,
    expected_event_id: int = 0,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    return _change_assignment_membership(
        package_id, assignment_id, True, user, db, expected_event_id
    )


@router.post(
    "/case-packages/{package_id}/assignments/{assignment_id}/remove",
    name="remove_assignment_from_case_package",
)
def remove_assignment_from_case_package(
    package_id: DatabaseId,
    assignment_id: DatabaseId,
    expected_event_id: int = 0,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    return _change_assignment_membership(
        package_id, assignment_id, False, user, db, expected_event_id
    )
