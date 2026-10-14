from __future__ import annotations

import tempfile
import tarfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.ops import backup_vps


class CloudBackupTests(unittest.TestCase):
    def test_backup_is_atomic_private_and_integrity_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_root = root / "app"
            document_root = app_root / "storage" / "documents"
            backup_root = root / "backups"
            document_root.mkdir(parents=True)
            backup_root.mkdir(mode=0o700)
            (document_root / "synthetic.pdf").write_bytes(b"synthetic")

            def fake_run(args, check=True, stdout=None, stderr=None, env=None, **kwargs):
                command = [str(part) for part in args]
                if command[:2] == ["docker", "inspect"]:
                    return SimpleNamespace(stdout="healthy\n")
                if command[:3] == ["docker", "exec", "deploy-db-1"]:
                    stdout.write(b"synthetic postgres archive")
                elif command[:3] == ["docker", "cp", "deploy-redis-1:/tmp/sro-robot-backup.rdb"]:
                    Path(command[3]).write_bytes(b"synthetic redis archive")
                elif command[:2] == ["tar", "--create"]:
                    with tarfile.open(command[4], "w:gz") as archive:
                        archive.add(document_root, arcname="documents")
                return SimpleNamespace(stdout="")

            with (
                patch.object(backup_vps, "APP_ROOT", app_root),
                patch.object(backup_vps, "DOCUMENT_ROOT", document_root),
                patch.object(backup_vps, "BACKUP_ROOT", backup_root),
                patch.object(backup_vps.os, "geteuid", return_value=0),
                patch.object(backup_vps.os, "environ", {"REDIS_PASSWORD": "synthetic"}),
                patch.object(backup_vps.subprocess, "run", side_effect=fake_run),
            ):
                result = backup_vps.create_backup()

            self.assertRegex(result.name, r"^daily-\d{8}T\d{6}Z$")
            self.assertEqual(set(path.name for path in result.iterdir()), {
                "postgres.dump", "redis.rdb", "documents.tar.gz", "SHA256SUMS"
            })
            self.assertEqual(result.stat().st_mode & 0o777, 0o700)
            self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in result.iterdir()))
            manifest = (result / "SHA256SUMS").read_text(encoding="ascii")
            self.assertIn("postgres.dump", manifest)
            self.assertIn("redis.rdb", manifest)
            self.assertIn("documents.tar.gz", manifest)
            self.assertFalse(any(path.name.startswith(".sro-backup-") for path in backup_root.iterdir()))

    def test_backup_refuses_group_or_world_accessible_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_root = root / "app"
            document_root = app_root / "storage" / "documents"
            backup_root = root / "backups"
            document_root.mkdir(parents=True)
            backup_root.mkdir(mode=0o755)
            backup_root.chmod(0o755)
            with (
                patch.object(backup_vps, "APP_ROOT", app_root),
                patch.object(backup_vps, "DOCUMENT_ROOT", document_root),
                patch.object(backup_vps, "BACKUP_ROOT", backup_root),
                patch.object(backup_vps.os, "geteuid", return_value=0),
                patch.object(backup_vps, "check_containers") as check_containers,
            ):
                with self.assertRaisesRegex(RuntimeError, "private"):
                    backup_vps.create_backup()
            check_containers.assert_not_called()


if __name__ == "__main__":
    unittest.main()
