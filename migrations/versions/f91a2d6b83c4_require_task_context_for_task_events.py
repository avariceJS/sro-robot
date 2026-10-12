"""Require task links for every task-scoped audit event.

Revision ID: f91a2d6b83c4
Revises: e75a2c8d190b
Create Date: 2026-10-12
"""

from __future__ import annotations

import hashlib

from alembic import op
from sqlalchemy import text


revision = "f91a2d6b83c4"
down_revision = "e75a2c8d190b"
branch_labels = None
depends_on = None

_PREVIOUS_EVENT_FUNCTION_SHA256 = "2af447010f8fb5622426f1da7dea2acc9837843b98f2a86087f8a1cf50eaa138"
_GLOBAL_EVENT_KINDS = "'session_started', 'session_login_failed', 'session_ended'"
_ANCHOR = "    END CASE;\n\n    IF requires_actor AND NEW.user_id IS NULL THEN"
_REPLACEMENT = f"""    END CASE;

    IF NEW.kind NOT IN ({_GLOBAL_EVENT_KINDS}) THEN
        requires_task := true;
    END IF;

    IF requires_actor AND NEW.user_id IS NULL THEN"""


def upgrade() -> None:
    connection = op.get_bind()
    source = connection.execute(
        text(
            "SELECT p.prosrc FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname = 'sro_validate_event_insert' "
            "AND p.pronargs = 0 AND p.prorettype = 'trigger'::regtype"
        )
    ).scalar_one_or_none()
    if source is None:
        raise RuntimeError("Audit event attribution guard is missing")
    actual_sha256 = hashlib.sha256(" ".join(source.split()).encode("utf-8")).hexdigest()
    if actual_sha256 != _PREVIOUS_EVENT_FUNCTION_SHA256:
        raise RuntimeError("Audit event attribution guard changed before task-context migration")
    if source.count(_ANCHOR) != 1:
        raise RuntimeError("Audit event trigger task-context insertion point is ambiguous")

    updated_source = source.replace(_ANCHOR, _REPLACEMENT, 1)
    if "$sro_task_event_contract$" in updated_source:
        raise RuntimeError("Audit event trigger contains an unexpected migration delimiter")
    connection.exec_driver_sql(
        "/* sro-task-event-context */\n"
        "CREATE OR REPLACE FUNCTION public.sro_validate_event_insert() "
        "RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog "
        "AS $sro_task_event_contract$\n"
        + updated_source
        + "\n$sro_task_event_contract$"
    )


def downgrade() -> None:
    raise RuntimeError("Task-scoped audit event linkage must not be removed automatically")
