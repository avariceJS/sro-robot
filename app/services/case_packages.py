"""Explicit, replay-safe membership for employee-curated case workspaces."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from sqlalchemy import func, select, true
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session, joinedload

from ..db.models import (
    AssignmentDraft,
    CasePackage,
    CasePackageAssignmentEvent,
    CasePackageTaskEvent,
    Task,
)


class CasePackageConflict(ValueError):
    """A replay key was reused with another actor or package description."""


def _payload_hash(title: str, case_number: str) -> str:
    payload = f"{title}\0{case_number}".encode("utf-8")
    return hashlib.sha256(b"sro-robot/case-package/v1\0" + payload).hexdigest()


def create_once(
    db: Session, *, user_id: int, request_key: str, title: str, case_number: str
) -> tuple[CasePackage, bool]:
    title = title.strip()
    case_number = case_number.strip()
    if not title or len(title) > 255 or len(case_number) > 64:
        raise ValueError("Package title and case-number context are outside supported limits")
    if re.fullmatch(r"[0-9a-f]{64}", request_key) is None:
        raise ValueError("Invalid package request key")
    if db.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Case package creation requires PostgreSQL")
    request_hash = hashlib.sha256(
        f"sro-robot/case-package-request/v1\0{user_id}\0{request_key}".encode("ascii")
    ).hexdigest()
    payload_hash = _payload_hash(title, case_number)
    inserted_id = db.scalar(
        postgresql_insert(CasePackage)
        .values(
            title=title,
            case_number=case_number,
            created_by_id=user_id,
            request_key_sha256=request_hash,
            payload_sha256=payload_hash,
        )
        .on_conflict_do_nothing(constraint="uq_case_package_request_key_sha256")
        .returning(CasePackage.id)
    )
    if inserted_id is not None:
        package = db.get(CasePackage, inserted_id)
        if package is None:
            raise RuntimeError("New case package disappeared inside its transaction")
        return package, True
    package = db.scalar(
        select(CasePackage)
        .where(CasePackage.request_key_sha256 == request_hash)
        .with_for_update()
    )
    if package is None:
        raise RuntimeError("Conflicting case package request could not be read back")
    if package.created_by_id != user_id or package.payload_sha256 != payload_hash:
        raise CasePackageConflict("Request key was already used for another package")
    return package, False


def set_task_membership(
    db: Session, *, package_id: int, task_id: int, actor_id: int, included: bool,
    expected_event_id: int,
) -> bool:
    """Append one membership transition, serializing writers on the package row."""
    package = db.scalar(
        select(CasePackage).where(CasePackage.id == package_id).with_for_update()
    )
    if package is None:
        raise LookupError("Case package not found")
    latest = db.execute(
        select(CasePackageTaskEvent.id, CasePackageTaskEvent.action)
        .where(
            CasePackageTaskEvent.case_package_id == package_id,
            CasePackageTaskEvent.task_id == task_id,
        )
        .order_by(CasePackageTaskEvent.id.desc())
        .limit(1)
    ).first()
    latest_id, latest_action = latest if latest else (0, None)
    currently_included = latest_action == "added"
    if currently_included == included:
        return False
    if latest_id != expected_event_id:
        raise CasePackageConflict("Состав папки изменился. Обновите страницу и повторите действие.")
    db.add(CasePackageTaskEvent(
        case_package_id=package_id,
        task_id=task_id,
        action="added" if included else "removed",
        actor_id=actor_id,
    ))
    db.flush()
    return True


def set_assignment_membership(
    db: Session, *, package_id: int, assignment_id: int, actor_id: int, included: bool,
    expected_event_id: int,
) -> bool:
    """Append one explicit assignment membership transition under the package lock."""
    package = db.scalar(
        select(CasePackage).where(CasePackage.id == package_id).with_for_update()
    )
    if package is None:
        raise LookupError("Case package not found")
    latest = db.execute(
        select(CasePackageAssignmentEvent.id, CasePackageAssignmentEvent.action)
        .where(
            CasePackageAssignmentEvent.case_package_id == package_id,
            CasePackageAssignmentEvent.assignment_draft_id == assignment_id,
        )
        .order_by(CasePackageAssignmentEvent.id.desc())
        .limit(1)
    ).first()
    latest_id, latest_action = latest if latest else (0, None)
    currently_included = latest_action == "added"
    if currently_included == included:
        return False
    if latest_id != expected_event_id:
        raise CasePackageConflict("Состав назначений в папке изменился. Обновите страницу и повторите действие.")
    db.add(CasePackageAssignmentEvent(
        case_package_id=package_id,
        assignment_draft_id=assignment_id,
        action="added" if included else "removed",
        actor_id=actor_id,
    ))
    db.flush()
    return True


def latest_membership_events(db: Session, package_id: int) -> list[CasePackageTaskEvent]:
    latest_ids = (
        select(CasePackageTaskEvent.task_id, CasePackageTaskEvent.id.label("event_id"))
        .where(CasePackageTaskEvent.case_package_id == package_id)
        .distinct(CasePackageTaskEvent.task_id)
        .order_by(CasePackageTaskEvent.task_id, CasePackageTaskEvent.id.desc())
        .subquery()
    )
    return list(db.scalars(
        select(CasePackageTaskEvent)
        .join(latest_ids, CasePackageTaskEvent.id == latest_ids.c.event_id)
        .options(
            joinedload(CasePackageTaskEvent.actor),
            joinedload(CasePackageTaskEvent.task).joinedload(Task.document),
            joinedload(CasePackageTaskEvent.task).joinedload(Task.intake_item),
        )
        .order_by(CasePackageTaskEvent.id)
    ).all())


def current_membership_events(db: Session, package_id: int) -> list[CasePackageTaskEvent]:
    return [event for event in latest_membership_events(db, package_id) if event.action == "added"]


def membership_history_page(
    db: Session, package_id: int, *, offset: int, limit: int
) -> list[CasePackageTaskEvent]:
    return list(db.scalars(
        select(CasePackageTaskEvent)
        .where(CasePackageTaskEvent.case_package_id == package_id)
        .options(
            joinedload(CasePackageTaskEvent.actor),
            joinedload(CasePackageTaskEvent.task).joinedload(Task.document),
            joinedload(CasePackageTaskEvent.task).joinedload(Task.intake_item),
        )
        .order_by(CasePackageTaskEvent.id.desc())
        .offset(offset)
        .limit(limit)
    ).all())


def active_case_packages_for_task(db: Session, task_id: int) -> list[CasePackage]:
    """Return only packages whose latest event for this task is an addition."""
    latest_membership = (
        select(
            CasePackageTaskEvent.case_package_id,
            func.max(CasePackageTaskEvent.id).label("event_id"),
        )
        .where(CasePackageTaskEvent.task_id == task_id)
        .group_by(CasePackageTaskEvent.case_package_id)
        .subquery()
    )
    return list(db.scalars(
        select(CasePackage)
        .join(latest_membership, CasePackage.id == latest_membership.c.case_package_id)
        .join(CasePackageTaskEvent, CasePackageTaskEvent.id == latest_membership.c.event_id)
        .where(CasePackageTaskEvent.action == "added")
        .order_by(CasePackage.id.desc())
    ).all())


def latest_assignment_membership_events(
    db: Session, package_id: int
) -> list[CasePackageAssignmentEvent]:
    latest_ids = (
        select(
            CasePackageAssignmentEvent.assignment_draft_id,
            CasePackageAssignmentEvent.id.label("event_id"),
        )
        .where(CasePackageAssignmentEvent.case_package_id == package_id)
        .distinct(CasePackageAssignmentEvent.assignment_draft_id)
        .order_by(CasePackageAssignmentEvent.assignment_draft_id, CasePackageAssignmentEvent.id.desc())
        .subquery()
    )
    return list(db.scalars(
        select(CasePackageAssignmentEvent)
        .join(latest_ids, CasePackageAssignmentEvent.id == latest_ids.c.event_id)
        .options(
            joinedload(CasePackageAssignmentEvent.actor),
            joinedload(CasePackageAssignmentEvent.assignment).joinedload(AssignmentDraft.debtor_card),
        )
        .order_by(CasePackageAssignmentEvent.id)
    ).all())


def current_assignment_membership_events(
    db: Session, package_id: int
) -> list[CasePackageAssignmentEvent]:
    return [event for event in latest_assignment_membership_events(db, package_id) if event.action == "added"]


def assignment_membership_history_page(
    db: Session, package_id: int, *, offset: int, limit: int
) -> list[CasePackageAssignmentEvent]:
    return list(db.scalars(
        select(CasePackageAssignmentEvent)
        .where(CasePackageAssignmentEvent.case_package_id == package_id)
        .options(
            joinedload(CasePackageAssignmentEvent.actor),
            joinedload(CasePackageAssignmentEvent.assignment).joinedload(AssignmentDraft.debtor_card),
        )
        .order_by(CasePackageAssignmentEvent.id.desc())
        .offset(offset)
        .limit(limit)
    ).all())


def active_case_packages_for_assignment(db: Session, assignment_id: int) -> list[CasePackage]:
    latest_membership = (
        select(
            CasePackageAssignmentEvent.case_package_id,
            func.max(CasePackageAssignmentEvent.id).label("event_id"),
        )
        .where(CasePackageAssignmentEvent.assignment_draft_id == assignment_id)
        .group_by(CasePackageAssignmentEvent.case_package_id)
        .subquery()
    )
    return list(db.scalars(
        select(CasePackage)
        .join(latest_membership, CasePackage.id == latest_membership.c.case_package_id)
        .join(CasePackageAssignmentEvent, CasePackageAssignmentEvent.id == latest_membership.c.event_id)
        .where(CasePackageAssignmentEvent.action == "added")
        .order_by(CasePackage.id.desc())
    ).all())


def assignment_package_document_page(
    db: Session, *, assignment_id: int, package_id: int, page: int, page_size: int = 50
) -> dict[str, object]:
    """Read assignment and task membership plus one bounded page in one snapshot."""
    from ..db.models import CasePackageAssignmentEvent, Document, IntakeItem

    assignment_latest = (
        select(func.max(CasePackageAssignmentEvent.id).label("event_id"))
        .where(
            CasePackageAssignmentEvent.assignment_draft_id == assignment_id,
            CasePackageAssignmentEvent.case_package_id == package_id,
        )
        .cte("assignment_latest_event")
    )
    assignment_state = (
        select(CasePackageAssignmentEvent.action.label("action"))
        .join(assignment_latest, CasePackageAssignmentEvent.id == assignment_latest.c.event_id)
        .cte("assignment_state")
    )
    package = select(CasePackage.id, CasePackage.title).where(CasePackage.id == package_id).cte("selected_package")
    task_latest = (
        select(
            CasePackageTaskEvent.case_package_id,
            CasePackageTaskEvent.task_id,
            func.max(CasePackageTaskEvent.id).label("event_id"),
        )
        .where(CasePackageTaskEvent.case_package_id == package_id)
        .group_by(CasePackageTaskEvent.case_package_id, CasePackageTaskEvent.task_id)
        .cte("selected_package_task_latest")
    )
    active_tasks = (
        select(
            Task.id.label("task_id"),
            Task.case_number.label("case_number"),
            func.coalesce(IntakeItem.filename, Document.filename).label("filename"),
            Document.stored_path.label("stored_path"),
        )
        .join(task_latest, task_latest.c.task_id == Task.id)
        .join(CasePackageTaskEvent, CasePackageTaskEvent.id == task_latest.c.event_id)
        .join(Document, Document.id == Task.document_id)
        .outerjoin(IntakeItem, IntakeItem.id == Task.intake_item_id)
        .where(CasePackageTaskEvent.action == "added")
        .cte("active_package_tasks")
    )
    totals = select(func.count().label("total")).select_from(active_tasks).cte("task_total")
    page_rows = (
        select(
            active_tasks.c.task_id,
            active_tasks.c.case_number,
            active_tasks.c.filename,
            active_tasks.c.stored_path,
        )
        .order_by(active_tasks.c.task_id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .cte("task_page")
    )
    statement = (
        select(
            package.c.id.label("package_id"),
            package.c.title.label("package_title"),
            assignment_state.c.action.label("assignment_action"),
            totals.c.total.label("total"),
            page_rows.c.task_id,
            page_rows.c.case_number,
            page_rows.c.filename,
            page_rows.c.stored_path,
        )
        .select_from(package)
        .outerjoin(assignment_state, true())
        .join(totals, true())
        .outerjoin(page_rows, true())
    )
    rows = db.execute(statement).all()
    if not rows:
        return {"package_exists": False, "assignment_active": False, "total": 0, "page": page, "rows": []}
    first = rows[0]
    total = int(first.total or 0)
    page_count = max(1, (total + page_size - 1) // page_size)
    documents = [
        {
            "task_id": int(row.task_id),
            "case_number": row.case_number or "",
            "filename": row.filename or "имя поступления недоступно",
            "preview_supported": Path(str(row.stored_path)).suffix.casefold()
            in {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"},
        }
        for row in rows if row.task_id is not None
    ]
    return {
        "package_exists": True,
        "package_title": first.package_title,
        "assignment_active": first.assignment_action == "added",
        "total": total,
        "page": page,
        "page_count": page_count,
        "rows": documents,
    }
