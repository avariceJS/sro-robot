from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.settings import Settings
from sro_robot.config import _secure_private_directory, load_env, private_setting


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

    @unittest.skipUnless(os.name == "posix", "POSIX directory handles are required")
    def test_directory_symlink_is_rejected_without_changing_target_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir(mode=0o755)
            target.chmod(0o755)
            link = root / "runtime"
            link.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "without symlinks"):
                _secure_private_directory(link)

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    @unittest.skipUnless(os.name == "posix", "POSIX file ownership is required")
    def test_upload_directory_symlink_is_rejected_by_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir(mode=0o755)
            target.chmod(0o755)
            link = root / "uploads"
            link.symlink_to(target, target_is_directory=True)

            with (
                patch("app.settings.load_env"),
                patch.dict(os.environ, {"APP_DEBUG": "1", "SRO_UPLOAD_DIR": str(link)}),
                self.assertRaisesRegex(ValueError, "without symlinks"),
            ):
                Settings.load()

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

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

    @unittest.skipUnless(os.name == "posix", "POSIX no-follow file open is required")
    def test_secret_environment_file_rejects_symlink_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "secret-source"
            target.write_text("SYNTHETIC_SYMLINK_SECRET=must-not-load\n")
            target.chmod(0o600)
            link = root / ".env"
            link.symlink_to(target)

            with (
                patch.dict(os.environ, {}, clear=False),
                self.assertRaisesRegex(ValueError, "without symlinks"),
            ):
                os.environ.pop("SYNTHETIC_SYMLINK_SECRET", None)
                load_env(link)

            self.assertNotIn("SYNTHETIC_SYMLINK_SECRET", os.environ)

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions are required")
    def test_empty_process_value_does_not_hide_a_later_private_file_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("SYNTHETIC_LOGIN=file-user\nSYNTHETIC_PASSWORD=file-secret\n")
            path.chmod(0o600)
            with patch.dict(os.environ, {"SYNTHETIC_LOGIN": "", "SYNTHETIC_PASSWORD": ""}, clear=False):
                self.assertEqual(private_setting("SYNTHETIC_LOGIN", path), "file-user")
                self.assertEqual(private_setting("SYNTHETIC_PASSWORD", path), "file-secret")
            with patch.dict(os.environ, {"SYNTHETIC_LOGIN": "env-user"}, clear=False):
                self.assertEqual(private_setting("SYNTHETIC_LOGIN", path), "env-user")

    @unittest.skipUnless(os.name == "posix", "POSIX FIFO handling is required")
    def test_secret_environment_file_rejects_fifo_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            os.mkfifo(path, 0o600)
            with self.assertRaisesRegex(ValueError, "regular file"):
                load_env(path)


if __name__ == "__main__":
    unittest.main()
