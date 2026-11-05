"""Private, durable screenshot creation without a real browser."""

import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from sro_robot.platform.client import SroClient


class SroScreenshotStorageTests(unittest.TestCase):
    def test_shot_writes_exclusive_private_file_and_syncs(self):
        with TemporaryDirectory() as directory:
            client = SroClient(cfg=SimpleNamespace(out_dir=Path(directory)))
            client.page = MagicMock()
            client.page.screenshot.return_value = b"synthetic png bytes"
            path = client.shot("general_filled")
            self.assertEqual(path.read_bytes(), b"synthetic png bytes")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            client.page.screenshot.assert_called_once_with(full_page=True)

    def test_bad_label_never_asks_browser_for_data(self):
        with TemporaryDirectory() as directory:
            client = SroClient(cfg=SimpleNamespace(out_dir=Path(directory)))
            client.page = MagicMock()
            with self.assertRaises(Exception):
                client.shot("../outside")
            client.page.screenshot.assert_not_called()
            self.assertEqual(os.listdir(directory), [])


if __name__ == "__main__":
    unittest.main()
