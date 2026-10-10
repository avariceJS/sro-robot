from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sro_robot.config import _secure_private_directory, load_env


class PrivateRuntimeDirectoryTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX file permissions are required")
    def test_existing_directory_is_restricted_to_its_service_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            path.chmod(0o755)

            _secure_private_directory(path)

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    @unittest.skipUnless(os.name == "posix", "POSIX file ownership is required")
    def test_foreign_owned_directory_fails_before_permission_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            path.chmod(0o755)
            service_uid = os.geteuid() + 1
            with patch("sro_robot.config.os.geteuid", return_value=service_uid):
                with self.assertRaisesRegex(ValueError, "owned by the service user"):
                    _secure_private_directory(path)

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions are required")
    def test_secret_environment_file_rejects_group_or_other_access_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("SYNTHETIC_SECRET=do-not-read\n")
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "must not grant group or other access"):
                load_env(path)

    @unittest.skipUnless(os.name == "posix", "POSIX file ownership is required")
    def test_secret_environment_file_rejects_foreign_owner_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("SYNTHETIC_SECRET=do-not-read\n")
            path.chmod(0o600)
            service_uid = os.geteuid() + 1
            with (
                patch("sro_robot.config.os.geteuid", return_value=service_uid),
                self.assertRaisesRegex(ValueError, "owned by the service user"),
            ):
                load_env(path)


if __name__ == "__main__":
    unittest.main()
