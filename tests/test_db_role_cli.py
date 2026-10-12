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
            "inherited_role_count": 2,
            "settable_role_count": 3,
            "settable_elevated_role_count": 1,
            "database_create": False,
            "public_schema_create": True,
            "public_schema_usage": True,
            "transaction_read_only": True,
            "public_table_count": 6,
            "public_table_select": 6,
            "public_table_insert": 5,
            "public_table_update": 5,
            "public_table_delete": 5,
            "public_table_rls": 0,
            "public_table_force_rls": 0,
            "public_sequence_count": 3,
            "public_sequence_usage": 3,
            "public_sequence_select": 3,
            "public_sequence_update": 0,
            "public_security_definer_exec_count": 1,
        }
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = connection
        output = StringIO()

        with patch.object(cli, "engine", engine), redirect_stdout(output):
            result = cli.cmd_db_role_check(None)

        self.assertEqual(result, 0)
        self.assertIn("superuser: False", output.getvalue())
        self.assertIn("inherited_role_count: 2", output.getvalue())
        self.assertIn("settable_role_count: 3", output.getvalue())
        self.assertIn("settable_elevated_role_count: 1", output.getvalue())
        self.assertIn("public_schema_create: True", output.getvalue())
        self.assertIn("public_table_insert: 5/6", output.getvalue())
        self.assertIn("public_table_rls: 0/6", output.getvalue())
        self.assertIn("public_sequence_usage: 3/3", output.getvalue())
        self.assertIn("public_security_definer_exec_count: 1", output.getvalue())
        self.assertIn("Informational snapshot only", output.getvalue())
        self.assertNotIn("role_name", output.getvalue())
        self.assertNotIn("database_name", output.getvalue())
        self.assertNotIn("runtime_security_definer_probe", output.getvalue())
        statement = str(connection.execute.call_args.args[0]).lstrip().lower()
        self.assertTrue(statement.startswith("select"))
        self.assertEqual(statement.count(";"), 0)
        self.assertIn("prosecdef", statement)
        self.assertIn("has_function_privilege", statement)
        self.assertEqual(
            str(connection.execute.call_args_list[0].args[0]),
            "SET TRANSACTION READ ONLY",
        )
        self.assertIn(
            "set_config('statement_timeout'",
            str(connection.execute.call_args_list[1].args[0]),
        )

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
