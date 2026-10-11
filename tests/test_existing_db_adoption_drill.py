from __future__ import annotations

import importlib.util
from pathlib import Path
import signal
import unittest
from unittest.mock import MagicMock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "postgres-existing-db-adoption-drill.py"
SPEC = importlib.util.spec_from_file_location("existing_db_adoption_drill", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
DRILL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRILL)


class ExistingDatabaseAdoptionDrillTests(unittest.TestCase):
    def _sequence_connection(
        self,
        states: list[tuple[str, bool]],
    ) -> tuple[MagicMock, list[str]]:
        connection = MagicMock()
        connection.dialect.identifier_preparer.quote.side_effect = lambda name: f'"{name}"'
        names = [f"synthetic_sequence_{index}" for index in range(len(states))]
        connection.execute.return_value.all.return_value = [
            (name, "1", "1", "1", "999999", "1", False)
            for name in names
        ]
        connection.exec_driver_sql.side_effect = [
            MagicMock(one=MagicMock(return_value=(last_value, is_called)))
            for last_value, is_called in states
        ]
        return connection, names

    def test_sequence_signature_checks_exact_next_value_state_without_disclosing_names(self) -> None:
        connection, names = self._sequence_connection([("41", True), ("7", False)])

        count, digest = DRILL._sequence_signature(connection)

        self.assertEqual(count, 2)
        for name in names:
            self.assertNotIn(name, digest)
        statements = [call.args[0] for call in connection.exec_driver_sql.call_args_list]
        self.assertIn('"synthetic_sequence_0"', statements[0])
        self.assertIn('"synthetic_sequence_1"', statements[1])

    def test_sequence_signature_distinguishes_called_from_not_called(self) -> None:
        called_connection, _ = self._sequence_connection([("41", True)])
        not_called_connection, _ = self._sequence_connection([("41", False)])

        self.assertNotEqual(
            DRILL._sequence_signature(called_connection),
            DRILL._sequence_signature(not_called_connection),
        )

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

    def test_termination_signals_unwind_through_cleanup(self) -> None:
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum), self.assertRaises(KeyboardInterrupt):
                DRILL._handle_shutdown_signal(signum, None)


if __name__ == "__main__":
    unittest.main()
