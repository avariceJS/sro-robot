"""Allow actor-attributed requests to recover OCR intake delivery.

Revision ID: e9c7b2a4106d
Revises: d12f6a84c3e1
Create Date: 2026-10-21
"""

from __future__ import annotations

import hashlib

from alembic import op
from sqlalchemy import text


revision = "e9c7b2a4106d"
down_revision = "d12f6a84c3e1"
branch_labels = None
depends_on = None

_PREVIOUS_EVENT_FUNCTION_SHA256 = "86b6e400d8b7ee67ed36911bfd9aa3ccbc612a7ac0d608d341e6eb06d9eb4720"
_ANCHOR = """        WHEN 'intake_replayed' THEN
            expected_message := 'Повтор запроса приёма связан с существующим заданием';
            requires_actor := true;
            requires_task := true;
            allowed_keys := ARRAY['request_id'];"""
_REPLACEMENT = _ANCHOR + """
        WHEN 'intake_recovery_requested' THEN
            expected_message := 'Сотрудник запросил восстановление доставки OCR';
            requires_actor := true;
            requires_task := true;
            allowed_keys := ARRAY['request_id'];"""
_EXPECTED_EVENT_FUNCTION_SHA256 = "f01da81d770b8f6ed92d7614582ad9b76ff88cb6b29bcafcd28c1fd6fa481a5b"


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
        raise RuntimeError("Audit event contract guard is missing")
    actual_sha256 = hashlib.sha256(" ".join(source.split()).encode("utf-8")).hexdigest()
    if actual_sha256 != _PREVIOUS_EVENT_FUNCTION_SHA256:
        raise RuntimeError("Audit event contract changed before intake recovery migration")
    if source.count(_ANCHOR) != 1:
        raise RuntimeError("Audit event trigger recovery insertion point is ambiguous")

    updated_source = source.replace(_ANCHOR, _REPLACEMENT, 1)
    updated_sha256 = hashlib.sha256(" ".join(updated_source.split()).encode("utf-8")).hexdigest()
    if updated_sha256 != _EXPECTED_EVENT_FUNCTION_SHA256:
        raise RuntimeError("Audit event recovery validator differs from its pinned contract")
    connection.exec_driver_sql(
        "/* sro-intake-recovery-event-contract */\n"
        "CREATE OR REPLACE FUNCTION public.sro_validate_event_insert() "
        "RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog "
        "AS $sro_intake_recovery_event_contract$\n"
        + updated_source
        + "\n$sro_intake_recovery_event_contract$"
    )


def downgrade() -> None:
    raise RuntimeError(
        "Audit event recovery history must be preserved; use a reviewed forward migration."
    )
