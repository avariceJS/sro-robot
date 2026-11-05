"""Actual PostgreSQL advisory-lock contention; no SRO/browser operation."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import text

from app.services import sro_account_lock


class SroAccountLockPostgresTests(unittest.TestCase):
    def setUp(self):
        try:
            with sro_account_lock.engine.connect() as connection:
                connection.scalar(text("SELECT 1"))
        except Exception:
            self.skipTest("disposable PostgreSQL unavailable")

    @staticmethod
    @contextmanager
    def _session():
        class Users:
            @staticmethod
            def get(_model, user_id):
                login = "synthetic-shared-account" if user_id in (7, 8) else "synthetic-other-account"
                return SimpleNamespace(sro_login=login)

        yield Users()

    def test_parallel_previews_allow_same_account_but_block_save(self):
        release_previews = threading.Event()
        first_entered = threading.Event()
        second_entered = threading.Event()

        def hold_shared(user_id, entered):
            with sro_account_lock.sro_account_lease(user_id, shared=True, wait_s=2):
                entered.set()
                self.assertTrue(release_previews.wait(3))

        with patch.object(sro_account_lock, "session_scope", side_effect=self._session):
            with ThreadPoolExecutor(max_workers=3) as pool:
                first = pool.submit(hold_shared, 7, first_entered)
                second = pool.submit(hold_shared, 8, second_entered)
                self.assertTrue(first_entered.wait(2))
                self.assertTrue(second_entered.wait(2))
                with self.assertRaises(sro_account_lock.SroAccountBusy):
                    with sro_account_lock.sro_account_lease(7, shared=False, wait_s=0.2):
                        pass
                # A distinct principal must not wait for the first account.
                with sro_account_lock.sro_account_lease(9, shared=False, wait_s=0.2):
                    pass
                release_previews.set()
                first.result(timeout=3)
                second.result(timeout=3)
            with sro_account_lock.sro_account_lease(7, shared=False, wait_s=1):
                pass

    def test_two_saves_for_same_account_serialize(self):
        entered = threading.Event()
        release = threading.Event()

        def hold_exclusive():
            with sro_account_lock.sro_account_lease(7, shared=False, wait_s=2):
                entered.set()
                self.assertTrue(release.wait(3))

        with patch.object(sro_account_lock, "session_scope", side_effect=self._session):
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(hold_exclusive)
                self.assertTrue(entered.wait(2))
                with self.assertRaises(sro_account_lock.SroAccountBusy):
                    with sro_account_lock.sro_account_lease(8, shared=False, wait_s=0.2):
                        pass
                release.set()
                first.result(timeout=3)
            with sro_account_lock.sro_account_lease(8, shared=False, wait_s=1):
                pass


if __name__ == "__main__":
    unittest.main()
