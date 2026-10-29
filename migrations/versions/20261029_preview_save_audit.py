"""Add attributable one-shot screenshot preview save audit contracts.

Revision ID: 20261029_preview
Revises: 20261021_reproc
Create Date: 2026-10-29
"""
from __future__ import annotations

import hashlib

from alembic import op
from sqlalchemy import text

revision = "20261029_preview"
down_revision = "20261021_reproc"
branch_labels = None
depends_on = None

_PREVIOUS_EVENT_FUNCTION_SHA256 = "f01da81d770b8f6ed92d7614582ad9b76ff88cb6b29bcafcd28c1fd6fa481a5b"
_EXPECTED_EVENT_FUNCTION_SHA256 = "d1b9d8314932e999126dd8604afa1ddd81f83d059da0e1cab77d2f608edd3f52"

_NEW_EVENT_CASES = """        WHEN 'preview_claimed' THEN
            expected_message := 'Подготовка формы СРО закреплена за воркером';
            requires_actor := true;
            allowed_keys := ARRAY[]::text[];
        WHEN 'decision_save' THEN
            expected_message := 'Оператор запросил сохранение показанной формы СРО';
            requires_actor := true;
            allowed_keys := ARRAY['preview_token', 'request_id'];
            required_keys := ARRAY['preview_token'];
        WHEN 'preview_save_started' THEN
            expected_message := 'Начата однократная попытка сохранения формы СРО';
            requires_actor := true;
            allowed_keys := ARRAY['preview_token'];
            required_keys := ARRAY['preview_token'];
        WHEN 'preview_saved' THEN
            expected_message := 'Новая карточка СРО сохранена и проверена';
            requires_actor := true;
            allowed_keys := ARRAY[]::text[];
        WHEN 'preview_closed' THEN
            expected_message := 'Форма СРО закрыта без сохранения';
            requires_actor := true;
            allowed_keys := ARRAY[]::text[];
        WHEN 'preview_failed' THEN
            expected_message := 'Подготовка или закрытие формы СРО не завершены; требуется проверка';
            requires_actor := true;
            allowed_keys := ARRAY[]::text[];
        WHEN 'preview_save_unknown' THEN
            expected_message := 'Результат сохранения СРО неизвестен; повтор запрещён';
            requires_actor := true;
            allowed_keys := ARRAY[]::text[];
"""


def _updated_event_source(source: str) -> str:
    """Fail closed on drift, and retain all existing immutable audit history."""
    digest = hashlib.sha256(" ".join(source.split()).encode("utf-8")).hexdigest()
    if digest != _PREVIOUS_EVENT_FUNCTION_SHA256:
        raise RuntimeError("Audit event contract changed before preview-save migration")
    replacements = (
        ("allowed_keys := ARRAY['screenshot_count'];", "allowed_keys := ARRAY['screenshot_count', 'preview_token'];"),
        ("ELSIF field_key = 'values_sha256' THEN", "ELSIF field_key IN ('values_sha256', 'preview_token') THEN"),
        ("        WHEN 'live_save_blocked' THEN", _NEW_EVENT_CASES + "        WHEN 'live_save_blocked' THEN"),
    )
    for anchor, replacement in replacements:
        if source.count(anchor) != 1:
            raise RuntimeError("Audit event preview-save insertion point is ambiguous")
        source = source.replace(anchor, replacement, 1)
    digest = hashlib.sha256(" ".join(source.split()).encode("utf-8")).hexdigest()
    if digest != _EXPECTED_EVENT_FUNCTION_SHA256:
        raise RuntimeError("Audit preview-save validator differs from its pinned contract")
    return source


def upgrade() -> None:
    connection = op.get_bind()
    source = connection.execute(text(
        "SELECT p.prosrc FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = 'sro_validate_event_insert' "
        "AND p.pronargs = 0 AND p.prorettype = 'trigger'::regtype"
    )).scalar_one_or_none()
    if source is None:
        raise RuntimeError("Audit event contract guard is missing")
    updated_source = _updated_event_source(source)
    connection.exec_driver_sql(
        "CREATE OR REPLACE FUNCTION public.sro_validate_event_insert() "
        "RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog "
        "AS $sro_preview_save_contract$\n" + updated_source + "\n$sro_preview_save_contract$"
    )


def downgrade() -> None:
    raise RuntimeError("Preview-save audit history must be preserved; use a reviewed forward migration.")
