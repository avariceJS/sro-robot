"""Synthetic account-gate tests; no SRO browser or external write."""

from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.services import sro_account_lock


class SroAccountLockTests(unittest.TestCase):
    def test_account_key_normalizes_login_not_local_employee_id(self):
        key = sro_account_lock._lock_key("  SAME.Login ")
        self.assertEqual(key, sro_account_lock._lock_key("same.login"))
        self.assertNotEqual(key, sro_account_lock._lock_key("other.login"))

    def test_shared_and_exclusive_locks_release_before_pool_return(self):
        for shared, expected_name in (
            (True, "pg_try_advisory_lock_shared"),
            (False, "pg_try_advisory_lock"),
        ):
            with self.subTest(shared=shared):
                db = MagicMock()
                db.get.return_value = SimpleNamespace(sro_login="same.login")

                @contextmanager
                def session():
                    yield db

                connection = MagicMock()
                connection.execution_options.return_value = connection
                connection.scalar.side_effect = (True, True)
                with (
                    patch.object(sro_account_lock, "session_scope", side_effect=session),
                    patch.object(sro_account_lock.engine, "connect", return_value=connection),
                ):
                    with sro_account_lock.sro_account_lease(7, shared=shared):
                        connection.close.assert_not_called()
                sql = [str(call.args[0]) for call in connection.scalar.call_args_list]
                self.assertIn(expected_name, sql[0])
                self.assertIn("pg_advisory_unlock", sql[1])
                connection.exec_driver_sql.assert_called_once_with("COMMIT")
                connection.commit.assert_called_once()
                connection.execution_options.assert_called_once_with(isolation_level="AUTOCOMMIT")
                connection.close.assert_called_once()
                connection.invalidate.assert_not_called()

    def test_unlock_failure_invalidates_connection(self):
        db = MagicMock()
        db.get.return_value = SimpleNamespace(sro_login="same.login")

        @contextmanager
        def session():
            yield db

        connection = MagicMock()
        connection.execution_options.return_value = connection
        connection.scalar.side_effect = (True, False)
        with (
            patch.object(sro_account_lock, "session_scope", side_effect=session),
            patch.object(sro_account_lock.engine, "connect", return_value=connection),
            self.assertRaises(RuntimeError),
        ):
            with sro_account_lock.sro_account_lease(7, shared=False):
                pass
        connection.invalidate.assert_called()
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
