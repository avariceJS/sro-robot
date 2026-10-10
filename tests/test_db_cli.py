"""Database CLI migration safety checks."""

from __future__ import annotations

import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
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
            with patch.object(cli, "engine", engine), patch.object(
                cli.alembic_command, "upgrade"
            ) as upgrade:
                self.assertEqual(cli.cmd_init_db(Namespace(reset=False)), 0)

            config, revision = upgrade.call_args.args
            self.assertEqual(config.config_file_name, str(cli.ROOT / "alembic.ini"))
            self.assertEqual(revision, "head")
        finally:
            engine.dispose()

    def test_init_db_refuses_existing_unversioned_schema(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("CREATE TABLE task (id INTEGER PRIMARY KEY)")

            with patch.object(cli, "engine", engine), patch.object(
                cli.alembic_command, "upgrade"
            ) as upgrade:
                self.assertEqual(cli.cmd_init_db(Namespace(reset=False)), 2)

            upgrade.assert_not_called()
        finally:
            engine.dispose()

    def test_schema_check_reports_unversioned_match_without_applying_migrations(self) -> None:
        connection = MagicMock()
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
            patch.object(cli.alembic_command, "upgrade") as upgrade,
            redirect_stdout(output),
        ):
            result = cli.cmd_schema_check(Namespace())

        self.assertEqual(result, 0)
        self.assertIn("revision: unversioned", output.getvalue())
        self.assertIn("Schema matches ORM metadata", output.getvalue())
        self.assertIn("no migration or stamp was applied", output.getvalue())
        upgrade.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
