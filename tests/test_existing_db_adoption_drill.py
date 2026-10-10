from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "postgres-existing-db-adoption-drill.py"
SPEC = importlib.util.spec_from_file_location("existing_db_adoption_drill", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
DRILL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRILL)


class ExistingDatabaseAdoptionDrillTests(unittest.TestCase):
    def test_service_value_leaves_simple_values_unquoted(self) -> None:
        self.assertEqual(DRILL._service_value("127.0.0.1"), "127.0.0.1")

    def test_service_value_quotes_spaces_and_escapes_quotes_and_backslashes(self) -> None:
        formatted = DRILL._service_value('synthetic "password"\\value')

        self.assertTrue(formatted.startswith('"'))
        self.assertTrue(formatted.endswith('"'))
        self.assertIn('\\"password\\"', formatted)
        self.assertIn("\\\\value", formatted)

    def test_service_value_rejects_newlines(self) -> None:
        with self.assertRaises(ValueError):
            DRILL._service_value("synthetic\noption=unexpected")


if __name__ == "__main__":
    unittest.main()
