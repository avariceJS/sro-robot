from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "ops" / "release_database.py"

_spec = importlib.util.spec_from_file_location("release_database_under_test", MODULE_PATH)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Could not load release_database.py")
release_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_database)


class ReleaseDatabaseSafetyTests(unittest.TestCase):
    def test_restore_rejects_tampered_dump_before_any_restore_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-db-tamper-") as temporary:
            directory = Path(temporary)
            dump = directory / "rollback.dump"
            dump.write_bytes(b"synthetic database dump")
            (directory / "verified.json").write_text(
                json.dumps(
                    {
                        "verified": True,
                        "dump_sha256": "0" * 64,
                        "signature": {"tables": {}, "sequences": {}},
                    }
                )
            )

            with (
                patch.object(release_database, "run") as run,
                patch.object(release_database, "database_name") as database_name,
                patch.object(release_database, "query") as query,
                self.assertRaisesRegex(RuntimeError, "proof or hash is invalid"),
            ):
                release_database.restore("synthetic-container", directory)

            run.assert_not_called()
            database_name.assert_not_called()
            query.assert_not_called()

    def test_quiescence_rejects_absent_maintenance_marker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-db-quiescence-") as temporary:
            root = Path(temporary)
            backups = root / "backups"
            backups.mkdir()
            directory = backups / "checkpoint"
            with (
                patch.object(release_database.os, "geteuid", return_value=0),
                patch.object(release_database, "MAINTENANCE", root / "maintenance"),
                patch.object(release_database, "BACKUPS", backups),
                patch.object(release_database, "run") as run,
                self.assertRaisesRegex(RuntimeError, "Root and active ingress maintenance"),
            ):
                release_database.require_quiescence(directory)
            run.assert_not_called()

    def test_quiescence_rejects_non_root_even_with_maintenance_marker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-db-quiescence-") as temporary:
            root = Path(temporary)
            backups = root / "backups"
            backups.mkdir()
            maintenance = root / "maintenance"
            maintenance.write_text("synthetic maintenance marker\n")
            directory = backups / "checkpoint"
            with (
                patch.object(release_database.os, "geteuid", return_value=1001),
                patch.object(release_database, "MAINTENANCE", maintenance),
                patch.object(release_database, "BACKUPS", backups),
                patch.object(release_database, "run") as run,
                self.assertRaisesRegex(RuntimeError, "Root and active ingress maintenance"),
            ):
                release_database.require_quiescence(directory)
            run.assert_not_called()

    def test_quiescence_rejects_active_writer_units(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-db-quiescence-") as temporary:
            root = Path(temporary)
            backups = root / "backups"
            backups.mkdir()
            maintenance = root / "maintenance"
            maintenance.write_text("synthetic maintenance marker\n")
            directory = backups / "checkpoint"
            with (
                patch.object(release_database.os, "geteuid", return_value=0),
                patch.object(release_database, "MAINTENANCE", maintenance),
                patch.object(release_database, "BACKUPS", backups),
                patch.object(
                    release_database,
                    "run",
                    return_value=b"sro-robot-web.service loaded active running\n",
                ) as run,
                self.assertRaisesRegex(RuntimeError, "Application writers must be stopped"),
            ):
                release_database.require_quiescence(directory)
            run.assert_called_once()

    def test_snapshot_does_not_mark_checkpoint_verified_if_source_changes(self) -> None:
        before = {"tables": {"synthetic": {"rows": 1, "sha256": "a"}}, "sequences": {}}
        after = {"tables": {"synthetic": {"rows": 2, "sha256": "b"}}, "sequences": {}}

        def fake_run(_args, *, stdin=None, stdout=None):
            if stdout is not None:
                stdout.write(b"synthetic database dump")
            return b""

        with tempfile.TemporaryDirectory(prefix="release-db-snapshot-") as temporary:
            directory = Path(temporary) / "checkpoint"
            with (
                patch.object(release_database, "database_name", return_value="synthetic_db"),
                patch.object(release_database, "signature", side_effect=[before, after]),
                patch.object(release_database, "run", side_effect=fake_run),
                patch.object(release_database, "verify_dump") as verify_dump,
                self.assertRaisesRegex(RuntimeError, "Database changed during checkpoint"),
            ):
                release_database.snapshot("synthetic-container", directory)

            self.assertTrue((directory / "rollback.dump").is_file())
            self.assertFalse((directory / "verified.json").exists())
            verify_dump.assert_not_called()

    def test_verify_dump_removes_temporary_container_after_restore_failure(self) -> None:
        completed = SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory(prefix="release-db-restore-proof-") as temporary:
            dump = Path(temporary) / "synthetic.dump"
            dump.write_bytes(b"synthetic database dump")
            with (
                patch.object(release_database.uuid, "uuid4", return_value=SimpleNamespace(hex="0123456789abcdef")),
                patch.object(
                    release_database,
                    "run",
                    side_effect=[
                        b"synthetic-image",
                        b"synthetic-container-id",
                        RuntimeError("synthetic restore failure"),
                    ],
                ) as run,
                patch.object(release_database, "database_name", return_value="synthetic_db"),
                patch.object(release_database.subprocess, "run", return_value=completed) as subprocess_run,
                self.assertRaisesRegex(RuntimeError, "synthetic restore failure"),
            ):
                release_database.verify_dump("synthetic-container", dump, {})

            self.assertEqual(run.call_count, 3)
            self.assertEqual(run.call_args_list[1].args[0][:3], ["docker", "run", "--rm"])
            self.assertIn("pg_restore", run.call_args_list[2].args[0])
            self.assertEqual(
                subprocess_run.call_args.args[0],
                ["docker", "rm", "-f", "sro-release-proof-0123456789ab"],
            )


if __name__ == "__main__":
    unittest.main()
