"""Database CLI migration safety checks."""

from __future__ import annotations

import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
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


if __name__ == "__main__":
    unittest.main()
