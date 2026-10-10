from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import unittest
from unittest.mock import MagicMock, patch

from app import cli


class DatabaseRoleCheckTests(unittest.TestCase):
    def test_reports_role_capabilities_without_identity_or_mutation(self) -> None:
        connection = MagicMock()
        connection.dialect.name = "postgresql"
        connection.execute.return_value.mappings.return_value.one.return_value = {
            "superuser": False,
            "create_role": False,
            "create_database": False,
            "replication": False,
            "bypass_row_security": False,
            "database_create": False,
            "public_schema_create": True,
            "public_schema_usage": True,
            "transaction_read_only": False,
        }
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = connection
        output = StringIO()

        with patch.object(cli, "engine", engine), redirect_stdout(output):
            result = cli.cmd_db_role_check(None)

        self.assertEqual(result, 0)
        self.assertIn("superuser: False", output.getvalue())
        self.assertIn("public_schema_create: True", output.getvalue())
        self.assertIn("Informational snapshot only", output.getvalue())
        self.assertNotIn("role_name", output.getvalue())
        self.assertNotIn("database_name", output.getvalue())
        statement = str(connection.execute.call_args.args[0]).lstrip().lower()
        self.assertTrue(statement.startswith("select"))
        self.assertEqual(statement.count(";"), 0)

    def test_refuses_non_postgresql_without_query(self) -> None:
        connection = MagicMock()
        connection.dialect.name = "sqlite"
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = connection
        output = StringIO()

        with patch.object(cli, "engine", engine), redirect_stdout(output):
            result = cli.cmd_db_role_check(None)

        self.assertEqual(result, 2)
        self.assertIn("requires PostgreSQL", output.getvalue())
        connection.execute.assert_not_called()

    def test_database_error_reports_only_exception_type(self) -> None:
        engine = MagicMock()
        engine.connect.side_effect = RuntimeError("synthetic-sensitive-detail")
        output = StringIO()

        with patch.object(cli, "engine", engine), redirect_stdout(output):
            result = cli.cmd_db_role_check(None)

        self.assertEqual(result, 2)
        self.assertIn("RuntimeError", output.getvalue())
        self.assertNotIn("synthetic-sensitive-detail", output.getvalue())


if __name__ == "__main__":
    unittest.main()
