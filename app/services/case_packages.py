"""Explicit, replay-safe membership for employee-curated case workspaces."""

from __future__ import annotations

import hashlib
import re

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session, joinedload

from ..db.models import CasePackage, CasePackageTaskEvent, Task


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
