from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests.postgres_safety import assert_disposable_postgres
from tests.postgres_safety import POSTGRES_IMAGE


class DisposablePostgresGuardTests(unittest.TestCase):
    def test_missing_disposable_marker_fails_before_docker_or_database_access(self) -> None:
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://localhost/postgres"}, clear=True):
            with patch("tests.postgres_safety.subprocess.run") as run:
                with self.assertRaisesRegex(AssertionError, "disposable-drill marker"):
                    assert_disposable_postgres()
        run.assert_not_called()

    def test_non_loopback_database_fails_before_docker_inspection(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SRO_DISPOSABLE_DRILL": "1",
                "SRO_DRILL_CONTAINER_NAME": "sro-restore-drill-123",
                "DATABASE_URL": "postgresql://db.example/postgres",
            },
            clear=True,
        ):
            with patch("tests.postgres_safety.subprocess.run") as run:
                with self.assertRaisesRegex(AssertionError, "loopback database"):
                    assert_disposable_postgres()
        run.assert_not_called()

    def test_unowned_port_fails_closed(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SRO_DISPOSABLE_DRILL": "1",
                "SRO_DRILL_CONTAINER_NAME": "sro-restore-drill-123",
                "DATABASE_URL": "postgresql://127.0.0.1:55432/postgres",
            },
            clear=True,
        ):
            with patch(
                "tests.postgres_safety.subprocess.run",
                side_effect=[
                    type("Result", (), {"returncode": 0, "stdout": f"true|{POSTGRES_IMAGE}"})(),
                    type("Result", (), {"returncode": 0, "stdout": "127.0.0.1:55433\n"})(),
                ],
            ):
                with self.assertRaisesRegex(AssertionError, "not owned"):
                    assert_disposable_postgres()

    def test_matching_live_restore_drill_container_is_accepted(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SRO_DISPOSABLE_DRILL": "1",
                "SRO_DRILL_CONTAINER_NAME": "sro-restore-drill-123",
                "DATABASE_URL": "postgresql://127.0.0.1:55432/postgres",
            },
            clear=True,
        ):
            with patch(
                "tests.postgres_safety.subprocess.run",
                side_effect=[
                    type("Result", (), {"returncode": 0, "stdout": f"true|{POSTGRES_IMAGE}"})(),
                    type("Result", (), {"returncode": 0, "stdout": "127.0.0.1:55432\n"})(),
                ],
            ) as run:
                assert_disposable_postgres()
        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
