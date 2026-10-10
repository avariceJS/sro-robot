"""The default SRO client must refuse state-changing controls."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from sro_robot.platform.client import SroClient
from sro_robot.platform.errors import SroError
from sro_robot.platform.registry import Registry


class SroMutationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = SroClient(cfg=SimpleNamespace(), dry_run=True)
        self.client.page = MagicMock()

    def test_add_save_and_close_are_blocked_by_default(self) -> None:
        actions = (
            lambda: Registry(self.client, "/list").click_add(),
            self.client.click_save,
            self.client.click_close,
        )
        for action in actions:
            with self.subTest(action=action):
                with self.assertRaisesRegex(SroError, "disabled"):
                    action()
        self.client.page.locator.assert_not_called()
        self.client.page.get_by_text.assert_not_called()

    def test_add_like_text_is_blocked_before_browser_access(self) -> None:
        with self.assertRaisesRegex(SroError, "disabled"):
            self.client.click_text("Добавить адрес")
        self.client.page.locator.assert_not_called()
        self.client.page.get_by_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
