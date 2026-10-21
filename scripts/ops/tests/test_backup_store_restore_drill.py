from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_store_restore_drill as drill


class RestoreDrillTests(unittest.TestCase):
    def make_backup(self, root: Path) -> None:
        blobs = {
            "postgres.dump": b"pg archive bytes",
            "redis.rdb": b"redis snapshot bytes",
            "documents.tar.gz": b"documents archive bytes",
        }
        for name, blob in blobs.items():
            target = root / name
            target.write_bytes(blob)
            target.chmod(0o600)
        manifest = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in blobs.items())
        (root / "SHA256SUMS").write_text(manifest, encoding="ascii")
        (root / "SHA256SUMS").chmod(0o600)
        root.chmod(0o700)

    def make_tar(self, path: Path, members: list[tuple[str, bytes | None, str]]) -> None:
        with tarfile.open(path, "w:gz") as archive:
            for name, content, kind in members:
                info = tarfile.TarInfo(name)
                if kind == "dir":
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    archive.addfile(info)
                elif kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "../../outside"
                    archive.addfile(info)
                else:
                    assert content is not None
                    info.size = len(content)
                    archive.addfile(info, __import__("io").BytesIO(content))

    def test_manifest_and_backup_hashes_validate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "backup"
            root.mkdir(mode=0o700)
            self.make_backup(root)
            files = drill.validate_backup_root(root, require_root=False)
            self.assertEqual(set(files), drill.EXPECTED)
            (root / "redis.rdb").write_bytes(b"modified")
            with self.assertRaisesRegex(drill.SafeFailure, "artifact_hash_mismatch"):
                drill.validate_backup_root(root, require_root=False)

    def test_manifest_rejects_duplicate_and_extra_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "SHA256SUMS"
            digest = "0" * 64
            path.write_text(f"{digest}  postgres.dump\n{digest}  postgres.dump\n", encoding="ascii")
            with self.assertRaisesRegex(drill.SafeFailure, "manifest_duplicate"):
                drill.parse_manifest(path)
            path.write_text("".join(f"{digest}  {name}\n" for name in (*sorted(drill.EXPECTED), "extra")), encoding="ascii")
            with self.assertRaisesRegex(drill.SafeFailure, "manifest_entries"):
                drill.parse_manifest(path)

    def test_archive_restore_counts_bytes_and_readback_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            archive = base / "docs.tar.gz"
            self.make_tar(archive, [
                ("documents", None, "dir"),
                ("documents/case-a/file.bin", b"synthetic document bytes", "file"),
            ])
            metadata = drill.inspect_tar(archive)
            destination = base / "restored"
            count, total, aggregate = drill.extract_and_verify(archive, destination, metadata)
            restored = destination / "case-a" / "file.bin"
            self.assertEqual((count, total), (1, len(b"synthetic document bytes")))
            self.assertEqual(len(aggregate), 64)
            self.assertEqual(restored.read_bytes(), b"synthetic document bytes")
            self.assertEqual(restored.stat().st_mode & 0o777, 0o600)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o700)

    def test_archive_rejects_traversal_and_symlinks(self) -> None:
        for name, kind in (("documents/../outside", "file"), ("documents/link", "symlink")):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "docs.tar.gz"
                content = b"x" if kind == "file" else None
                self.make_tar(archive, [(name, content, kind)])
                with self.assertRaises(drill.SafeFailure):
                    drill.inspect_tar(archive)

    def test_archive_rejects_duplicate_names_and_member_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "docs.tar.gz"
            self.make_tar(archive, [("documents/a", b"one", "file"), ("documents/a", b"two", "file")])
            with self.assertRaisesRegex(drill.SafeFailure, "archive_duplicate_path"):
                drill.inspect_tar(archive)

    def test_mode_and_type_checks_reject_symlinked_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "backup"
            root.mkdir(mode=0o700)
            self.make_backup(root)
            real = root / "redis.rdb"
            replacement = root / "redis-real"
            real.rename(replacement)
            real.symlink_to(replacement)
            with self.assertRaisesRegex(drill.SafeFailure, "artifact_mode_or_type"):
                drill.validate_backup_root(root, require_root=False)

    def test_keyspace_parser_accepts_newer_redis_info_fields(self) -> None:
        info = "# Keyspace\r\ndb0:keys=2,expires=0,avg_ttl=0,subexpiry=0\r\ndb3:keys=1,expires=0,avg_ttl=-1\r\n"
        self.assertEqual(drill.parse_keyspace_rows(info), [(0, 2), (3, 1)])

    def run_cleanup_case(self, *, create_partial: bool, ps_returncode: int, ps_stdout: str) -> tuple[str, mock.Mock]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        rdb = base / "synthetic.rdb"
        rdb.write_bytes(b"synthetic redis backup")
        evidence = base / "evidence"
        evidence.mkdir(mode=0o700)
        container_name = "sro-backup-drill-0123456789abcdef"
        fixed_uuid = mock.Mock()
        fixed_uuid.hex = "0123456789abcdef0123456789abcdef"

        def run_command(args: list[str], *unused: object, **unused_keywords: object) -> subprocess.CompletedProcess[str]:
            if args[1] == "inspect":
                return subprocess.CompletedProcess(args, 0, stdout="sha256:" + "a" * 64 + "\n", stderr="")
            if args[1] == "info":
                return subprocess.CompletedProcess(args, 0, stdout=str(base), stderr="")
            if args[1] == "create" and create_partial:
                raise drill.SafeFailure("docker_command_failed")
            if args[1] == "create":
                return subprocess.CompletedProcess(args, 0, stdout="synthetic-container-id\n", stderr="")
            if args[1] == "cp":
                raise drill.SafeFailure("docker_command_failed")
            raise AssertionError(f"unexpected docker command: {args[1]}")

        def docker_process(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            operation = args[1]
            if operation == "logs":
                return subprocess.CompletedProcess(args, 0, stdout="synthetic log", stderr="")
            if operation == "rm":
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="synthetic rm response lost")
            if operation == "ps":
                return subprocess.CompletedProcess(args, ps_returncode, stdout=ps_stdout, stderr="")
            raise AssertionError(f"unexpected cleanup command: {operation}")

        with mock.patch.object(drill.uuid, "uuid4", return_value=fixed_uuid), \
             mock.patch.object(drill, "run_command", side_effect=run_command), \
             mock.patch.object(drill.subprocess, "run", side_effect=docker_process) as process:
            if create_partial:
                with self.assertRaisesRegex(drill.SafeFailure, "docker_command_failed"):
                    drill.redis_restore(rdb, "docker", 512, evidence)
            else:
                with self.assertRaisesRegex(drill.SafeFailure, "redis_container_cleanup_unverified"):
                    drill.redis_restore(rdb, "docker", 512, evidence)
        return container_name, process

    def test_partial_create_registers_name_and_confirms_cleanup(self) -> None:
        name, process = self.run_cleanup_case(create_partial=True, ps_returncode=0, ps_stdout="")
        self.assertEqual([call.args[0][1] for call in process.call_args_list], ["logs", "rm", "ps"])
        ps_args = process.call_args_list[-1].args[0]
        self.assertIn(f"name=^/{name}$", ps_args)
        self.assertEqual(ps_args[-1], "{{.Names}}")

    def test_cleanup_fails_closed_if_ps_readback_fails_or_name_remains(self) -> None:
        cases = ((1, "", "daemon readback failed"), (0, "sro-backup-drill-0123456789abcdef\n", "container remains"))
        for returncode, output, reason in cases:
            with self.subTest(reason=reason):
                name, process = self.run_cleanup_case(create_partial=False, ps_returncode=returncode, ps_stdout=output)
                self.assertEqual(process.call_args_list[-1].args[0][1], "ps")
                self.assertIn(f"name=^/{name}$", process.call_args_list[-1].args[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
