"""Require mandatory typed fields for reviewed audit event kinds.

Revision ID: d45e6b7a19c2
Revises: b67d4c91a20e
Create Date: 2026-10-12
"""

from __future__ import annotations

from alembic import op


revision = "d45e6b7a19c2"
down_revision = "b67d4c91a20e"
branch_labels = None
depends_on = None


_EVENT_CONTRACT_FUNCTION = r"""
CREATE OR REPLACE FUNCTION public.sro_validate_event_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    expected_message text;
    allowed_keys text[];
    required_keys text[];
    field_key text;
    field_value jsonb;
    scalar_value text;
BEGIN
    required_keys := ARRAY[]::text[];
    CASE NEW.kind
        WHEN 'session_started' THEN
            expected_message := 'Сотрудник вошёл в приложение';
            allowed_keys := ARRAY['request_id'];
        WHEN 'session_login_failed' THEN
            expected_message := 'Вход отклонён: учётные данные не подтверждены';
            allowed_keys := ARRAY['request_id'];
        WHEN 'session_ended' THEN
            expected_message := 'Сотрудник завершил браузерную сессию';
            allowed_keys := ARRAY['request_id'];
        WHEN 'kad_lookup_started' THEN
            expected_message := 'Поиск судебного акта в КАД начат';
            allowed_keys := ARRAY[]::text[];
        WHEN 'kad_document_received' THEN
            expected_message := 'Судебный акт получен; начинается разбор PDF';
            allowed_keys := ARRAY[]::text[];
        WHEN 'not_approved' THEN
            expected_message := 'Локальная проверка не выполнена: задание не подтверждено';
            allowed_keys := ARRAY[]::text[];
        WHEN 'ocr_attempts_exhausted' THEN
            expected_message := 'Лимит попыток локального распознавания исчерпан; требуется проверка оператора';
            allowed_keys := ARRAY['attempts'];
            required_keys := ARRAY['attempts'];
        WHEN 'ocr_claimed' THEN
            expected_message := 'Локальное распознавание захвачено воркером';
            allowed_keys := ARRAY['attempt', 'generation'];
            required_keys := ARRAY['attempt', 'generation'];
        WHEN 'ocr_claim_completed' THEN
            expected_message := 'Результат распознавания сохранён владельцем актуальной аренды';
            allowed_keys := ARRAY['attempt', 'generation'];
            required_keys := ARRAY['attempt', 'generation'];
        WHEN 'ocr_claim_failed' THEN
            expected_message := 'Локальное распознавание завершилось ошибкой';
            allowed_keys := ARRAY['attempt', 'generation'];
            required_keys := ARRAY['attempt', 'generation'];
        WHEN 'approval_invalidated' THEN
            expected_message := 'Подтверждение оператора больше не соответствует текущим полям';
            allowed_keys := ARRAY['request_id'];
        WHEN 'operator_action_not_applied' THEN
            expected_message := 'Операторское действие не применено: предусловие состояния не выполнено';
            allowed_keys := ARRAY['action', 'reason', 'request_id'];
            required_keys := ARRAY['action', 'reason'];
        WHEN 'created' THEN
            expected_message := 'Document received';
            allowed_keys := ARRAY['request_id'];
        WHEN 'created_from_kad' THEN
            expected_message := 'Получение судебного акта из КАД запланировано';
            allowed_keys := ARRAY['request_id'];
        WHEN 'task_claimed' THEN
            expected_message := 'Фоновая обработка принята воркером';
            allowed_keys := ARRAY['claim_mode'];
            required_keys := ARRAY['claim_mode'];
        WHEN 'recognized' THEN
            expected_message := 'Документ распознан';
            allowed_keys := ARRAY[]::text[];
        WHEN 'fields_saved' THEN
            expected_message := 'Оператор сохранил поля задания';
            allowed_keys := ARRAY['changed', 'field_count', 'values_sha256', 'request_id'];
            required_keys := ARRAY['changed', 'field_count', 'values_sha256'];
        WHEN 'approved' THEN
            expected_message := 'Оператор подтвердил поля';
            allowed_keys := ARRAY['values_sha256', 'request_id'];
            required_keys := ARRAY['values_sha256'];
        WHEN 'rejected' THEN
            expected_message := 'Оператор отклонил задание до активной обработки';
            allowed_keys := ARRAY['request_id'];
        WHEN 'local_validation_requested' THEN
            expected_message := 'Оператор запросил локальную проверку задания';
            allowed_keys := ARRAY['execution_mode', 'saved', 'request_id'];
            required_keys := ARRAY['execution_mode', 'saved'];
        WHEN 'writing_started' THEN
            expected_message := 'Task execution started';
            allowed_keys := ARRAY['dry_run'];
            required_keys := ARRAY['dry_run'];
        WHEN 'done' THEN
            expected_message := 'Task processing completed';
            allowed_keys := ARRAY[]::text[];
        WHEN 'failed' THEN
            expected_message := 'Обработка завершилась ошибкой; требуется проверка';
            allowed_keys := ARRAY[]::text[];
        WHEN 'duplicate' THEN
            expected_message := 'Duplicate document detected';
            allowed_keys := ARRAY['count'];
            required_keys := ARRAY['count'];
        WHEN 'local_validation_completed' THEN
            expected_message := 'Локальная проверка завершена; запись в СРО не выполнялась';
            allowed_keys := ARRAY['execution_mode', 'saved'];
            required_keys := ARRAY['execution_mode', 'saved'];
        WHEN 'preview_started' THEN
            expected_message := 'Operator preview is ready';
            allowed_keys := ARRAY['screenshot_count'];
            required_keys := ARRAY['screenshot_count'];
        WHEN 'live_save_blocked' THEN
            expected_message := 'Запись в реальный реестр СРО отключена';
            allowed_keys := ARRAY['saved', 'request_id'];
            required_keys := ARRAY['saved'];
        WHEN 'decision_close' THEN
            expected_message := 'Решение оператора по превью';
            allowed_keys := ARRAY['request_id'];
        WHEN 'preview_timeout' THEN
            expected_message := 'Время ожидания решения оператора истекло';
            allowed_keys := ARRAY[]::text[];
        WHEN 'intake_replayed' THEN
            expected_message := 'Повтор запроса приёма связан с существующим заданием';
            allowed_keys := ARRAY['request_id'];
        ELSE
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'unsupported audit event kind';
    END CASE;

    IF jsonb_typeof(NEW.payload) IS DISTINCT FROM 'object'
       OR NEW.payload - allowed_keys <> '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid audit event payload fields';
    END IF;

    IF NOT (NEW.payload ?& required_keys) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'missing required audit event payload fields';
    END IF;

    FOR field_key, field_value IN SELECT key, value FROM jsonb_each(NEW.payload) LOOP
        scalar_value := field_value #>> '{}';
        IF field_key IN ('attempts', 'attempt', 'generation', 'count', 'field_count', 'screenshot_count') THEN
            IF jsonb_typeof(field_value) <> 'number'
               OR scalar_value !~ '^(0|[1-9][0-9]*)$' THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid audit event count';
            END IF;
        ELSIF field_key IN ('changed', 'saved', 'dry_run') THEN
            IF jsonb_typeof(field_value) <> 'boolean' THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid audit event boolean';
            END IF;
        ELSIF field_key = 'request_id' THEN
            IF jsonb_typeof(field_value) <> 'string'
               OR scalar_value !~ '^[0-9a-f]{32}$' THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid audit event request id';
            END IF;
        ELSIF field_key = 'values_sha256' THEN
            IF jsonb_typeof(field_value) <> 'string'
               OR scalar_value !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid audit event digest';
            END IF;
        ELSIF field_key = 'claim_mode' THEN
            IF field_value <> '"legacy_unleased"'::jsonb THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid audit event claim mode';
            END IF;
        ELSIF field_key = 'action' THEN
            IF field_value NOT IN ('"save_fields"'::jsonb, '"approve"'::jsonb, '"reject"'::jsonb,
                                   '"local_validation"'::jsonb, '"preview_decision"'::jsonb) THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid audit event action';
            END IF;
        ELSIF field_key = 'reason' THEN
            IF field_value <> '"precondition_not_met"'::jsonb THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid audit event reason';
            END IF;
        ELSIF field_key = 'execution_mode' THEN
            IF field_value <> '"local_validation"'::jsonb THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid audit event execution mode';
            END IF;
        ELSE
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'unsupported audit event payload field';
        END IF;
    END LOOP;

    NEW.message := expected_message;
    RETURN NEW;
END;
$$
"""


def upgrade() -> None:
    op.execute(_EVENT_CONTRACT_FUNCTION)


def downgrade() -> None:
    raise RuntimeError(
        "Audit event payload requirements must not be removed automatically; use a reviewed "
        "forward migration after preserving the established integrity boundary."
    )
