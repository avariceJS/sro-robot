from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app.db.role_policy import enforce_runtime_privilege_ceiling


class RuntimeDatabaseRolePolicyTests(unittest.TestCase):
    def test_accepts_role_inside_runtime_privilege_ceiling(self) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = (False,)

        enforce_runtime_privilege_ceiling(cursor)

        cursor.execute.assert_called_once()
        cursor.fetchone.assert_called_once_with()

    def test_rejects_role_outside_runtime_privilege_ceiling(self) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = (True,)

        with self.assertRaisesRegex(RuntimeError, "exceeds its privilege ceiling"):
            enforce_runtime_privilege_ceiling(cursor)

    def test_missing_role_metadata_fails_closed(self) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = None

        with self.assertRaisesRegex(RuntimeError, "exceeds its privilege ceiling"):
            enforce_runtime_privilege_ceiling(cursor)

    def test_metadata_query_error_is_generic_and_does_not_leak_details(self) -> None:
        cursor = MagicMock()
        cursor.execute.side_effect = RuntimeError("secret role or connection detail")

        with self.assertRaises(RuntimeError) as captured:
            enforce_runtime_privilege_ceiling(cursor)

        self.assertEqual(
            str(captured.exception),
            "Production runtime database role privilege check failed",
        )
        self.assertIsNone(captured.exception.__cause__)
        self.assertNotIn("secret", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
