from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocr_diagnostics as diagnostic


class Result:
    def __init__(self, rows=(), one=None):
        self.rows = rows
        self.result_one = one

    def __iter__(self):
        return iter(self.rows)

    def one(self):
        return self.result_one


class FakeSession:
    def __init__(self):
        self.statements: list[str] = []
        self.parameters: list[dict | None] = []
        self.rolled_back = False
        self.closed = False
        self.committed = False

    def begin(self):
        self.statements.append("BEGIN")

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.statements.append(sql)
        self.parameters.append(parameters)
        if "GROUP BY category" in sql:
            return Result([("needs_review", 3), ("failed", 2), ("other", 7)])
        if "GROUP BY field.name" in sql:
            return Result([
                ("case_number", 2, 1, 0, 0, 0),
                ("private_field_name", 99, 99, 99, 99, 99),
            ])
        if "FROM task" in sql and "known_error" in (parameters or {}):
            return Result(one=(2, 1))
        raise AssertionError("unexpected SQL shape")

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class FakeTransaction:
    def __init__(self):
        self.is_active = True
        self.rolled_back = False

    def rollback(self):
        self.rolled_back = True
        self.is_active = False


class FakeConnection:
    def __init__(self):
        self.statements: list[str] = []
        self.closed = False
        self.transaction = FakeTransaction()
        self.raw_rollback_count = 0

    @property
    def connection(self):
        return self

    def rollback(self):
        self.raw_rollback_count += 1
        self.statements.append("DBAPI_ROLLBACK")

    def begin(self):
        self.statements.append("BEGIN")
        return self.transaction

    def execute(self, statement):
        self.statements.append(str(statement))

    def close(self):
        self.closed = True


class OCRDiagnosticTests(unittest.TestCase):
    def test_aggregates_are_privacy_safe_and_transaction_is_rolled_back(self):
        session = FakeSession()
        connection = FakeConnection()
        sql_module = mock.MagicMock(text=lambda value: value)
        bound = []

        def session_factory(*, bind):
            self.assertIs(bind, connection)
            self.assertEqual(
                connection.statements,
                [
                    "DBAPI_ROLLBACK",
                    "BEGIN",
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
                    "SET LOCAL search_path TO public, pg_temp",
                ],
            )
            bound.append(bind)
            return session

        with mock.patch.dict(sys.modules, {"sqlalchemy": sql_module}):
            output = diagnostic.collect_summary(
                session_factory,
                lambda: connection,
                dialect="postgresql",
                debug=False,
            )
        rendered = json.dumps(output)

        self.assertEqual(output["tasks_by_status"]["failed"], 2)
        self.assertEqual(output["tasks_by_status"]["other"], 7)
        self.assertEqual(output["failed_errors"]["other_or_unclassified"], 1)
        self.assertEqual(output["recognized_card_fields"]["case_number"]["present"], 2)
        self.assertNotIn("private_status", rendered)
        self.assertNotIn("private_field_name", rendered)
        self.assertNotIn("private OCR text", rendered)

        self.assertEqual(
            connection.statements,
            [
                "DBAPI_ROLLBACK",
                "BEGIN",
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
                "SET LOCAL search_path TO public, pg_temp",
            ],
        )
        self.assertEqual(connection.raw_rollback_count, 1)
        self.assertEqual(bound, [connection])
        self.assertTrue(session.rolled_back)
        self.assertTrue(session.closed)
        self.assertTrue(connection.transaction.rolled_back)
        self.assertTrue(connection.closed)
        self.assertFalse(session.committed)
        for sql in session.statements[1:]:
            self.assertIn("count(*)", sql)
            self.assertNotRegex(sql, r"(?i)select\s+(?:task\.)?(?:id|stored_path|filename|recognized\s*[,\s])")
        self.assertNotIn("private OCR text", repr(session.parameters))

    def test_refuses_non_postgresql_or_debug_database_before_connecting(self):
        factory = mock.Mock(side_effect=AssertionError("must not connect"))
        for dialect, debug in (("sqlite", False), ("postgresql", True)):
            with self.subTest(dialect=dialect, debug=debug):
                with self.assertRaisesRegex(RuntimeError, "diagnostic_requires_production_postgresql"):
                    diagnostic.collect_summary(
                        factory,
                        lambda: self.fail("must not connect"),
                        dialect=dialect,
                        debug=debug,
                    )
        factory.assert_not_called()

    def test_compact_report_preserves_primary_fields_and_rolls_up_the_rest(self):
        summary = {
            "tasks_by_status": {"needs_review": 2},
            "failed_errors": {"with_message": 0},
            "recognized_card_fields": {
                name: {
                    "present": 1,
                    "warning_present": 1,
                    "source_categories": {category: 1 for category in diagnostic.SOURCE_CATEGORIES},
                }
                for name in diagnostic.CARD_FIELDS
            },
        }
        compact = diagnostic.compact_summary(summary)
        self.assertEqual(
            set(compact["primary_card_fields"]),
            {"doc_type", "doc_date", "case_number", "court"},
        )
        self.assertEqual(compact["other_card_fields"]["field_count"], len(diagnostic.CARD_FIELDS) - 4)
        self.assertEqual(compact["other_card_fields"]["present_occurrences"], len(diagnostic.CARD_FIELDS) - 4)
        self.assertNotIn("recognized_card_fields", compact)

    def test_failure_reporting_does_not_include_database_exception_text(self):
        # The entrypoint reports a fixed category and leaves driver details hidden.
        base = mock.MagicMock()
        base.SessionLocal.side_effect = RuntimeError("private-host user password")
        base.engine.dialect.name = "postgresql"
        settings = mock.MagicMock()
        settings.debug = False
        with mock.patch.dict(sys.modules, {
            "sqlalchemy": mock.MagicMock(text=lambda value: value),
            "app.db.base": base,
            "app.settings": mock.MagicMock(settings=settings),
        }), contextlib.redirect_stderr(io.StringIO()) as captured:
            result = diagnostic.main([])
        self.assertEqual(result, 1)
        self.assertEqual(captured.getvalue(), "ocr_diagnostic=failed reason=diagnostic_failed\n")
        self.assertNotIn("private-host", captured.getvalue())


@unittest.skipUnless(
    os.environ.get("SRO_OCR_DIAGNOSTICS_TEST_URL"),
    "set SRO_OCR_DIAGNOSTICS_TEST_URL to a disposable admin PostgreSQL database named ocrdiag",
)
class OCRDiagnosticPostgreSQLQualification(unittest.TestCase):
    def test_real_session_hook_snapshot_readonly_and_private_canaries(self):
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError
        from sqlalchemy.engine import make_url

        database_url = os.environ["SRO_OCR_DIAGNOSTICS_TEST_URL"]
        if make_url(database_url).database != "ocrdiag":
            self.fail("qualification database must be named ocrdiag")

        # Configure the application against the explicitly supplied disposable
        # database before importing its engine and SessionLocal.
        os.environ.update({
            "APP_DEBUG": "1",
            "APP_SECRET_KEY": "synthetic-diagnostic-qualification-key",
            "DATABASE_URL": database_url,
            "REDIS_URL": "redis://127.0.0.1:6379/1",
        })
        from app.db.base import SessionLocal, _create_database_engine

        engine = _create_database_engine(database_url, pool_size=1, max_overflow=0)
        self.addCleanup(engine.dispose)
        runtime_engine = None
        runtime_role = f"sro_ocr_diag_{uuid.uuid4().hex}"
        runtime_role_created = False
        connection = engine.connect()
        try:
            public_task_absent = connection.execute(
                text("SELECT to_regclass('public.task') IS NULL")
            ).scalar_one()
            if not public_task_absent:
                self.skipTest("disposable database already contains public.task")
            connection.execute(text(
                "CREATE TABLE public.task (id integer, status text, recognized jsonb, error text)"
            ))
            connection.execute(
                text("INSERT INTO task VALUES (1, 'needs_review', CAST(:recognized AS jsonb), ''), "
                     "(2, 'failed', '{}'::jsonb, :error)"),
                {
                    "recognized": json.dumps({
                        "case_number": {
                            "value": "PRIVATE_IDENTIFIER_CANARY",
                            "warning": "PRIVATE_WARNING_CANARY",
                            "source": "role_ambiguous",
                        },
                        "doc_type": {"value": "PRIVATE_RAW_TEXT_CANARY"},
                    }),
                    "error": "PRIVATE_ERROR_CANARY",
                },
            )
            connection.commit()
        finally:
            connection.close()

        try:
            with engine.begin() as admin:
                admin.execute(text(
                    f"CREATE ROLE {runtime_role} LOGIN NOSUPERUSER "
                    "NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS "
                    "PASSWORD 'synthetic_diagnostic_runtime_password'"
                ))
                admin.execute(text(f"GRANT CONNECT ON DATABASE ocrdiag TO {runtime_role}"))
                admin.execute(text(f"GRANT USAGE ON SCHEMA public TO {runtime_role}"))
                admin.execute(text(f"GRANT SELECT, INSERT ON public.task TO {runtime_role}"))
            runtime_role_created = True
            runtime_url = make_url(database_url).set(
                username=runtime_role,
                password="synthetic_diagnostic_runtime_password",
            )
            runtime_engine = _create_database_engine(
                runtime_url,
                pool_size=1,
                max_overflow=0,
                enforce_role_policy=True,
            )
            summary = diagnostic.collect_summary(
                SessionLocal,
                runtime_engine.connect,
                dialect="postgresql",
                debug=False,
            )
            rendered = json.dumps(summary, sort_keys=True)
            self.assertEqual(summary["tasks_by_status"]["needs_review"], 1)
            self.assertEqual(summary["tasks_by_status"]["failed"], 1)
            self.assertEqual(summary["recognized_card_fields"]["case_number"]["present"], 1)
            for canary in (
                "PRIVATE_IDENTIFIER_CANARY",
                "PRIVATE_WARNING_CANARY",
                "PRIVATE_RAW_TEXT_CANARY",
                "PRIVATE_ERROR_CANARY",
            ):
                self.assertNotIn(canary, rendered)

            connection = runtime_engine.connect()
            transaction = None
            session = None
            try:
                connection.connection.rollback()
                transaction = connection.begin()
                connection.execute(text(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                ))
                connection.execute(text("SET LOCAL search_path TO public, pg_temp"))
                session = SessionLocal(bind=connection)
                session.begin()
                self.assertEqual(
                    session.execute(text("SHOW transaction_read_only")).scalar_one(), "on"
                )
                self.assertEqual(
                    session.execute(text("SHOW transaction_isolation")).scalar_one(),
                    "repeatable read",
                )
                with self.assertRaises(DBAPIError) as write_error:
                    session.execute(text(
                        "INSERT INTO task VALUES (3, 'new', '{}'::jsonb, '')"
                    ))
                self.assertEqual(write_error.exception.orig.sqlstate, "25006")
                session.rollback()
                session.close()
                session = None
                self.assertEqual(
                    connection.execute(text("SELECT count(*) FROM task")).scalar_one(), 2
                )
            finally:
                if session is not None:
                    session.rollback()
                    session.close()
                if transaction is not None and transaction.is_active:
                    transaction.rollback()
                connection.close()
        finally:
            if runtime_engine is not None:
                runtime_engine.dispose()
            with engine.begin() as cleanup:
                cleanup.execute(text("DROP TABLE IF EXISTS public.task"))
                if runtime_role_created:
                    cleanup.execute(text(
                        f"REVOKE USAGE ON SCHEMA public FROM {runtime_role}"
                    ))
                    cleanup.execute(text(
                        f"REVOKE CONNECT ON DATABASE ocrdiag FROM {runtime_role}"
                    ))
                    cleanup.execute(text(f"DROP ROLE {runtime_role}"))
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
