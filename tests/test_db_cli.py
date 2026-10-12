"""Database CLI migration safety checks."""

from __future__ import annotations

import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine

from app import cli


class DatabaseCliTests(unittest.TestCase):
    def test_legacy_reset_flag_refuses_destructive_schema_drop(self) -> None:
        with patch.object(cli.alembic_command, "upgrade") as upgrade:
            self.assertEqual(cli.cmd_init_db(Namespace(reset=True)), 2)

        upgrade.assert_not_called()

    def test_init_db_applies_alembic_head(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        try:
            with patch.object(cli, "migration_engine", engine), patch.object(
                cli.alembic_command, "upgrade"
            ) as upgrade:
                self.assertEqual(cli.cmd_init_db(Namespace(reset=False)), 0)

            config, revision = upgrade.call_args.args
            self.assertEqual(config.config_file_name, str(cli.ROOT / "alembic.ini"))
            self.assertEqual(revision, "head")
        finally:
            engine.dispose()

    def test_init_db_uses_migration_engine_instead_of_runtime_engine(self) -> None:
        migration_engine = create_engine("sqlite:///:memory:")
        runtime_engine = MagicMock()
        try:
            with (
                patch.object(cli, "migration_engine", migration_engine),
                patch.object(cli, "engine", runtime_engine),
                patch.object(cli.alembic_command, "upgrade") as upgrade,
            ):
                self.assertEqual(cli.cmd_init_db(Namespace(reset=False)), 0)

            runtime_engine.connect.assert_not_called()
            upgrade.assert_called_once()
        finally:
            migration_engine.dispose()

    def test_init_db_reports_migration_failure_without_traceback(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        output = StringIO()
        try:
            with (
                patch.object(cli, "migration_engine", engine),
                patch.object(cli.alembic_command, "upgrade", side_effect=RuntimeError("synthetic detail")),
                redirect_stdout(output),
            ):
                result = cli.cmd_init_db(Namespace(reset=False))

            self.assertEqual(result, 2)
            self.assertIn("миграции не выполнены (RuntimeError)", output.getvalue())
            self.assertNotIn("synthetic detail", output.getvalue())
        finally:
            engine.dispose()

    def test_init_db_refuses_existing_unversioned_schema(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("CREATE TABLE task (id INTEGER PRIMARY KEY)")

            with patch.object(cli, "migration_engine", engine), patch.object(
                cli.alembic_command, "upgrade"
            ) as upgrade:
                self.assertEqual(cli.cmd_init_db(Namespace(reset=False)), 2)

            upgrade.assert_not_called()
        finally:
            engine.dispose()

    def test_revoke_sessions_requires_explicit_confirmation_before_database_access(self) -> None:
        output = StringIO()
        with (
            patch.object(cli, "session_scope") as session_scope,
            redirect_stdout(output),
        ):
            result = cli.main(["revoke-sessions"])

        self.assertEqual(result, 2)
        self.assertIn("--confirm", output.getvalue())
        session_scope.assert_not_called()

    def test_revoke_sessions_reports_only_aggregate_count(self) -> None:
        output = StringIO()
        session_manager = MagicMock()
        session_manager.__enter__.return_value = MagicMock()
        with (
            patch.object(cli, "session_scope", return_value=session_manager),
            patch.object(cli.auth, "revoke_all_login_sessions", return_value=3),
            redirect_stdout(output),
        ):
            result = cli.main(["revoke-sessions", "--confirm"])

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue().strip(), "Отозвано активных браузерных сессий: 3")

    def test_deactivate_user_requires_confirmation_before_database_access(self) -> None:
        output = StringIO()
        with (
            patch.object(cli, "session_scope") as session_scope,
            redirect_stdout(output),
        ):
            result = cli.main(["deactivate-user", "synthetic-operator"])

        self.assertEqual(result, 2)
        self.assertIn("--confirm", output.getvalue())
        session_scope.assert_not_called()

    def test_deactivate_user_reports_only_account_state_and_session_count(self) -> None:
        output = StringIO()
        session_manager = MagicMock()
        session_manager.__enter__.return_value = MagicMock()
        login = "synthetic-private-login"
        with (
            patch.object(cli, "session_scope", return_value=session_manager),
            patch.object(
                cli.auth,
                "deactivate_user",
                return_value=(SimpleNamespace(id=7), 2),
            ) as deactivate,
            redirect_stdout(output),
        ):
            result = cli.main(["deactivate-user", login, "--confirm"])

        self.assertEqual(result, 0)
        deactivate.assert_called_once_with(session_manager.__enter__.return_value, login)
        self.assertIn("завершено браузерных сессий: 2", output.getvalue())
        self.assertNotIn(login, output.getvalue())

    def test_schema_check_reports_unversioned_match_without_applying_migrations(self) -> None:
        connection = MagicMock()
        connection.dialect.name = "postgresql"
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = connection
        migration_context = MagicMock()
        migration_context.get_current_revision.return_value = None
        inspector = MagicMock()
        inspector.get_table_names.return_value = ["document", "event", "task", "user"]
        output = StringIO()

        with (
            patch.object(cli, "engine", engine),
            patch.object(cli.MigrationContext, "configure", return_value=migration_context),
            patch.object(cli, "compare_metadata", return_value=[]),
            patch.object(cli, "inspect", return_value=inspector),
            patch.object(cli, "compare_postgresql_enums", return_value=[]),
            patch.object(cli, "compare_postgresql_objects", return_value=[]),
            patch.object(cli, "compare_postgresql_index_uniqueness", return_value=[]),
            patch.object(cli.alembic_command, "upgrade") as upgrade,
            redirect_stdout(output),
        ):
            result = cli.cmd_schema_check(Namespace())

        self.assertEqual(result, 0)
        self.assertIn("revision: unversioned", output.getvalue())
        self.assertIn("Schema matches ORM metadata", output.getvalue())
        self.assertIn("no migration or stamp was applied", output.getvalue())
        upgrade.assert_not_called()
        self.assertEqual(
            str(connection.execute.call_args_list[0].args[0]),
            "SET TRANSACTION READ ONLY",
        )
        self.assertIn(
            "set_config('lock_timeout'",
            str(connection.execute.call_args_list[1].args[0]),
        )

    def test_schema_check_returns_nonzero_when_metadata_differs(self) -> None:
        connection = MagicMock()
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = connection
        migration_context = MagicMock()
        migration_context.get_current_revision.return_value = "bc518c7514d3"
        inspector = MagicMock()
        inspector.get_table_names.return_value = ["task"]
        output = StringIO()

        with (
            patch.object(cli, "engine", engine),
            patch.object(cli.MigrationContext, "configure", return_value=migration_context),
            patch.object(cli, "compare_metadata", return_value=[("remove_table", "task")]),
            patch.object(cli, "inspect", return_value=inspector),
            redirect_stdout(output),
        ):
            result = cli.cmd_schema_check(Namespace())

        self.assertEqual(result, 1)
        self.assertIn("Schema differs from ORM metadata", output.getvalue())
        self.assertIn("remove_table", output.getvalue())

    def test_postgresql_enum_check_detects_missing_changed_and_unexpected_labels(self) -> None:
        expected = cli.expected_postgresql_enums()
        actual_enums = [
            {"name": name, "labels": labels}
            for name, labels in expected.items()
        ]
        connection = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        inspector = MagicMock()
        inspector.get_enums.return_value = actual_enums

        with patch.object(cli, "inspect", return_value=inspector):
            self.assertEqual(cli.compare_postgresql_enums(connection), [])

            missing_name = actual_enums[0]["name"]
            inspector.get_enums.return_value = actual_enums[1:]
            missing_differences = cli.compare_postgresql_enums(connection)

            changed = [
                {"name": name, "labels": labels}
                for name, labels in expected.items()
            ]
            changed[0]["labels"] = [*changed[0]["labels"], "unexpected_label"]
            inspector.get_enums.return_value = changed
            changed_differences = cli.compare_postgresql_enums(connection)

            unexpected = [*actual_enums, {"name": "unmanaged_enum", "labels": ["x"]}]
            inspector.get_enums.return_value = unexpected
            unexpected_differences = cli.compare_postgresql_enums(connection)

        self.assertEqual(len(missing_differences), 1)
        self.assertIn(missing_name, missing_differences[0])
        self.assertEqual(len(changed_differences), 1)
        self.assertIn("unexpected_label", changed_differences[0])
        self.assertEqual(len(unexpected_differences), 1)
        self.assertIn("unmanaged_enum", unexpected_differences[0])

    def test_enum_comparison_is_skipped_for_non_postgresql_dialects(self) -> None:
        connection = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
        with patch.object(cli, "inspect") as inspect_connection:
            self.assertEqual(cli.compare_postgresql_enums(connection), [])
        inspect_connection.assert_not_called()

    def test_postgresql_object_check_detects_missing_sequences_and_unmodeled_relations(self) -> None:
        expected_sequences = cli.expected_postgresql_sequences()
        connection = MagicMock()
        connection.dialect.name = "postgresql"
        connection.execute.return_value.scalars.return_value.all.return_value = [
            "trigger task.legacy_trigger"
        ]
        inspector = MagicMock()
        inspector.get_sequence_names.return_value = sorted(expected_sequences - {"task_id_seq"})
        inspector.get_view_names.return_value = ["legacy_view"]
        inspector.get_materialized_view_names.return_value = ["legacy_materialized_view"]
        inspector.get_foreign_table_names.return_value = ["remote_table"]

        with (
            patch.object(cli, "inspect", return_value=inspector),
            patch.object(cli, "_audit_event_guard_differences", return_value=[]),
            patch.object(cli, "_audit_event_contract_differences", return_value=[]),
            patch.object(cli, "_task_status_guard_differences", return_value=[]),
            patch.object(cli, "_task_status_history_guard_differences", return_value=[]),
            patch.object(cli, "_runtime_trigger_security_differences", return_value=[]),
        ):
            differences = cli.compare_postgresql_objects(connection)

        self.assertEqual(len(differences), 5)
        self.assertTrue(any("missing sequence task_id_seq" in item for item in differences))
        self.assertTrue(any("unexpected view legacy_view" in item for item in differences))
        self.assertTrue(
            any("unexpected materialized view legacy_materialized_view" in item for item in differences)
        )
        self.assertTrue(any("unexpected foreign table remote_table" in item for item in differences))
        self.assertTrue(any("unexpected trigger task.legacy_trigger" in item for item in differences))

    def test_postgresql_object_check_detects_row_security_and_policies(self) -> None:
        connection = MagicMock()
        connection.dialect.name = "postgresql"
        connection.execute.return_value.scalars.return_value.all.return_value = [
            "row-level security enabled on table task",
            "row-level security forced on table task",
            "row-level security policy task.owner_access",
        ]
        inspector = MagicMock()
        inspector.get_sequence_names.return_value = sorted(cli.expected_postgresql_sequences())
        inspector.get_view_names.return_value = []
        inspector.get_materialized_view_names.return_value = []
        inspector.get_foreign_table_names.return_value = []

        with (
            patch.object(cli, "inspect", return_value=inspector),
            patch.object(cli, "_audit_event_guard_differences", return_value=[]),
            patch.object(cli, "_audit_event_contract_differences", return_value=[]),
            patch.object(cli, "_task_status_guard_differences", return_value=[]),
            patch.object(cli, "_task_status_history_guard_differences", return_value=[]),
            patch.object(cli, "_runtime_trigger_security_differences", return_value=[]),
        ):
            differences = cli.compare_postgresql_objects(connection)

        self.assertEqual(
            differences,
            [
                "unexpected row-level security enabled on table task",
                "unexpected row-level security forced on table task",
                "unexpected row-level security policy task.owner_access",
            ],
        )

    def test_postgresql_object_check_detects_public_extensions_and_routines(self) -> None:
        connection = MagicMock()
        connection.dialect.name = "postgresql"
        connection.execute.return_value.scalars.return_value.all.return_value = [
            "extension public.synthetic_extension",
            "routine public.synthetic_function(integer)",
        ]
        inspector = MagicMock()
        inspector.get_sequence_names.return_value = sorted(cli.expected_postgresql_sequences())
        inspector.get_view_names.return_value = []
        inspector.get_materialized_view_names.return_value = []
        inspector.get_foreign_table_names.return_value = []

        with (
            patch.object(cli, "inspect", return_value=inspector),
            patch.object(cli, "_audit_event_guard_differences", return_value=[]),
            patch.object(cli, "_audit_event_contract_differences", return_value=[]),
            patch.object(cli, "_task_status_guard_differences", return_value=[]),
            patch.object(cli, "_task_status_history_guard_differences", return_value=[]),
            patch.object(cli, "_runtime_trigger_security_differences", return_value=[]),
        ):
            differences = cli.compare_postgresql_objects(connection)

        self.assertEqual(
            differences,
            [
                "unexpected extension public.synthetic_extension",
                "unexpected routine public.synthetic_function(integer)",
            ],
        )

    def test_runtime_trigger_functions_must_not_be_security_definer(self) -> None:
        function_names = (
            "sro_reject_event_mutation",
            "sro_validate_event_insert",
            "sro_enforce_task_status_transition",
            "sro_record_task_status_change",
            "sro_reject_task_status_history_mutation",
        )
        connection = MagicMock()
        connection.execute.return_value.all.return_value = [
            (name, name == "sro_validate_event_insert") for name in function_names
        ]

        self.assertEqual(
            cli._runtime_trigger_security_differences(connection),
            [
                "runtime trigger function unexpectedly SECURITY DEFINER: "
                "sro_validate_event_insert"
            ],
        )
        connection.execute.return_value.all.return_value = [
            (name, False) for name in function_names
        ]
        self.assertEqual(cli._runtime_trigger_security_differences(connection), [])

    def test_postgresql_object_check_is_skipped_for_non_postgresql_dialects(self) -> None:
        connection = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
        with patch.object(cli, "inspect") as inspect_connection:
            self.assertEqual(cli.compare_postgresql_objects(connection), [])
        inspect_connection.assert_not_called()

    def test_append_only_event_guard_catalog_drift_is_reported(self) -> None:
        connection = MagicMock()
        connection.execute.side_effect = [
            SimpleNamespace(all=lambda: [(
                "BEGIN RAISE EXCEPTION USING ERRCODE = '55000', "
                "MESSAGE = 'event rows are append-only'; END;",
                "plpgsql",
                ["search_path=pg_catalog"],
            )]),
            SimpleNamespace(all=lambda: [
                ("event_append_only", 27, "D", False, "sro_reject_event_mutation", "public"),
                ("event_no_truncate", 34, "O", False, "sro_reject_event_mutation", "public"),
            ]),
        ]

        self.assertEqual(
            cli._audit_event_guard_differences(connection),
            ["append-only event trigger shape differs: event_append_only"],
        )

        connection.execute.side_effect = [SimpleNamespace(all=lambda: []), SimpleNamespace(all=lambda: [])]
        self.assertEqual(
            cli._audit_event_guard_differences(connection),
            [
                "append-only event trigger function missing or ambiguous",
                "append-only event trigger set missing or ambiguous",
            ],
        )

    def test_append_only_event_guard_detects_missing_truncate_trigger(self) -> None:
        connection = MagicMock()
        connection.execute.side_effect = [
            SimpleNamespace(all=lambda: [(
                "BEGIN RAISE EXCEPTION USING ERRCODE = '55000', "
                "MESSAGE = 'event rows are append-only'; END;",
                "plpgsql",
                ["search_path=pg_catalog"],
            )]),
            SimpleNamespace(all=lambda: [(
                "event_append_only", 27, "O", False, "sro_reject_event_mutation", "public"
            )]),
        ]

        self.assertEqual(
            cli._audit_event_guard_differences(connection),
            ["append-only event trigger set missing or ambiguous"],
        )

    def test_audit_event_contract_guard_detects_missing_and_changed_objects(self) -> None:
        connection = MagicMock()
        connection.execute.side_effect = [
            SimpleNamespace(all=lambda: []),
            SimpleNamespace(all=lambda: []),
        ]
        self.assertEqual(
            cli._audit_event_contract_differences(connection),
            [
                "audit event contract trigger function missing or ambiguous",
                "audit event contract trigger missing or ambiguous",
            ],
        )

        connection.execute.side_effect = [
            SimpleNamespace(all=lambda: [("BEGIN RETURN NEW; END;", "plpgsql", ["search_path=pg_catalog"])]),
            SimpleNamespace(all=lambda: [(7, "O", False, "sro_validate_event_insert", "public", "CREATE TRIGGER event_contract_validate BEFORE INSERT ON public.event FOR EACH ROW EXECUTE FUNCTION public.sro_validate_event_insert()")]),
        ]
        self.assertEqual(
            cli._audit_event_contract_differences(connection),
            ["audit event contract trigger function definition differs"],
        )

    def test_task_status_guard_catalog_check_detects_missing_and_changed_graph(self) -> None:
        connection = MagicMock()
        connection.execute.side_effect = [
            SimpleNamespace(all=lambda: []),
            SimpleNamespace(all=lambda: []),
        ]
        self.assertEqual(
            cli._task_status_guard_differences(connection),
            [
                "task status trigger function missing or ambiguous",
                "task status trigger missing or ambiguous",
            ],
        )

        migration = (
            Path(cli.ROOT / "migrations/versions/3b6a9f2d41c8_guard_task_status_transitions.py")
            .read_text(encoding="utf-8")
        )
        source = migration.split("AS $$", 1)[1].split("$$", 1)[0]
        connection.execute.side_effect = [
            SimpleNamespace(all=lambda: [(source, "plpgsql", ["search_path=pg_catalog"])]),
            SimpleNamespace(
                all=lambda: [
                    (
                        "task_status_transition_guard",
                        19,
                        "O",
                        False,
                        "sro_enforce_task_status_transition",
                        "public",
                        "CREATE TRIGGER task_status_transition_guard BEFORE UPDATE OF status ON public.task FOR EACH ROW EXECUTE FUNCTION public.sro_enforce_task_status_transition()",
                    )
                ]
            ),
        ]
        self.assertEqual(cli._task_status_guard_differences(connection), [])

        changed_source = source.replace("'failed', 'rejected'", "'failed', 'done'", 1)
        connection.execute.side_effect = [
            SimpleNamespace(all=lambda: [(changed_source, "plpgsql", ["search_path=pg_catalog"])]),
            SimpleNamespace(
                all=lambda: [
                    (
                        "task_status_transition_guard",
                        19,
                        "O",
                        False,
                        "sro_enforce_task_status_transition",
                        "public",
                        "CREATE TRIGGER task_status_transition_guard BEFORE UPDATE OF status ON public.task FOR EACH ROW EXECUTE FUNCTION public.sro_enforce_task_status_transition()",
                    )
                ]
            ),
        ]
        self.assertEqual(
            cli._task_status_guard_differences(connection),
            ["task status trigger function definition differs"],
        )

    def test_task_status_history_catalog_check_requires_capture_and_immutability(self) -> None:
        connection = MagicMock()
        connection.execute.side_effect = [
            SimpleNamespace(all=lambda: []),
            SimpleNamespace(all=lambda: []),
        ]
        self.assertEqual(
            cli._task_status_history_guard_differences(connection),
            [
                "task status history trigger functions missing or ambiguous",
                "task status history trigger set missing or ambiguous",
            ],
        )

        connection.execute.side_effect = [
            SimpleNamespace(
                all=lambda: [
                    (
                        "sro_record_task_status_change",
                        "BEGIN INSERT INTO public.task_status_history "
                        "(task_id, from_status, to_status) VALUES "
                        "(NEW.id, OLD.status::text, NEW.status::text); RETURN NEW; END;",
                        "plpgsql",
                        ["search_path=pg_catalog"],
                    ),
                    (
                        "sro_reject_task_status_history_mutation",
                        "BEGIN RAISE EXCEPTION USING ERRCODE = '55000', "
                        "MESSAGE = 'task status history rows are append-only'; END;",
                        "plpgsql",
                        ["search_path=pg_catalog"],
                    ),
                ]
            ),
            SimpleNamespace(
                all=lambda: [
                    (
                        "task",
                        "task_status_history_record_change",
                        17,
                        "O",
                        False,
                        "sro_record_task_status_change",
                        "public",
                        "CREATE TRIGGER task_status_history_record_change AFTER UPDATE OF "
                        "status ON public.task FOR EACH ROW WHEN ((old.status IS DISTINCT "
                        "FROM new.status)) EXECUTE FUNCTION "
                        "public.sro_record_task_status_change()",
                    ),
                    (
                        "task_status_history",
                        "task_status_history_append_only",
                        27,
                        "O",
                        False,
                        "sro_reject_task_status_history_mutation",
                        "public",
                        "CREATE TRIGGER task_status_history_append_only BEFORE UPDATE OR "
                        "DELETE ON public.task_status_history FOR EACH ROW EXECUTE FUNCTION "
                        "public.sro_reject_task_status_history_mutation()",
                    ),
                    (
                        "task_status_history",
                        "task_status_history_no_truncate",
                        34,
                        "O",
                        False,
                        "sro_reject_task_status_history_mutation",
                        "public",
                        "CREATE TRIGGER task_status_history_no_truncate BEFORE TRUNCATE "
                        "ON public.task_status_history FOR EACH STATEMENT EXECUTE FUNCTION "
                        "public.sro_reject_task_status_history_mutation()",
                    ),
                ]
            ),
        ]
        self.assertEqual(cli._task_status_history_guard_differences(connection), [])

    def test_postgresql_index_check_detects_unique_flag_drift(self) -> None:
        connection = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        inspector = MagicMock()
        inspector.get_table_names.return_value = ["document"]
        inspector.get_indexes.return_value = [
            {"name": "ix_document_content_hash", "unique": False}
        ]
        with patch.object(cli, "inspect", return_value=inspector):
            differences = cli.compare_postgresql_index_uniqueness(connection)

        self.assertIn(
            "document.ix_document_content_hash uniqueness expected=True actual=False",
            differences,
        )

    def test_postgresql_index_check_accepts_expected_unique_flag(self) -> None:
        connection = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        inspector = MagicMock()
        inspector.get_table_names.return_value = ["document"]
        inspector.get_indexes.return_value = [
            {"name": "ix_document_content_hash", "unique": True}
        ]
        with patch.object(cli, "inspect", return_value=inspector):
            differences = cli.compare_postgresql_index_uniqueness(connection)

        self.assertEqual(differences, [])


if __name__ == "__main__":
    unittest.main()
