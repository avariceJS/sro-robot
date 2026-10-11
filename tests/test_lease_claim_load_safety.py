from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "lease_claim_load_smoke.py"
WORKER_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "lease_claim_process_worker.py"


class LeaseClaimLoadSafetyTests(unittest.TestCase):
    def run_helper(
        self,
        *,
        disposable_marker: bool,
        database_url: str,
        drill_container: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as upload_dir:
            environment = {
                **os.environ,
                "APP_DEBUG": "1",
                "DATABASE_URL": database_url,
                "MIGRATION_DATABASE_URL": database_url,
                "SRO_UPLOAD_DIR": upload_dir,
            }
            if disposable_marker:
                environment["SRO_DISPOSABLE_DRILL"] = "1"
            else:
                environment.pop("SRO_DISPOSABLE_DRILL", None)
            environment.pop("SRO_DRILL_CONTAINER_NAME", None)
            if drill_container is not None:
                environment["SRO_DRILL_CONTAINER_NAME"] = drill_container
            return subprocess.run(
                [sys.executable, str(SCRIPT)],
                capture_output=True,
                text=True,
                timeout=10,
                env=environment,
                check=False,
            )

    def test_refuses_synthetic_writes_without_explicit_drill_marker(self) -> None:
        result = self.run_helper(
            disposable_marker=False,
            database_url="postgresql+psycopg://synthetic:synthetic@127.0.0.1:5432/sro",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("disposable-drill marker", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_refuses_unknown_executor_mode_before_any_synthetic_write(self) -> None:
        with tempfile.TemporaryDirectory() as upload_dir:
            environment = {
                **os.environ,
                "APP_DEBUG": "1",
                "DATABASE_URL": "postgresql+psycopg://synthetic:synthetic@127.0.0.1:5432/sro",
                "MIGRATION_DATABASE_URL": "postgresql+psycopg://synthetic:synthetic@127.0.0.1:5432/sro",
                "REDIS_URL": "redis://synthetic:synthetic@127.0.0.1:1/0",
                "SRO_UPLOAD_DIR": upload_dir,
                "SRO_LEASE_LOAD_MODE": "unverified",
            }
            environment.pop("SRO_DISPOSABLE_DRILL", None)
            environment.pop("SRO_DRILL_CONTAINER_NAME", None)
            result = subprocess.run(
                [sys.executable, str(SCRIPT)],
                capture_output=True,
                text=True,
                timeout=10,
                env=environment,
                check=False,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("SRO_LEASE_LOAD_MODE must be thread or process", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_process_worker_refuses_claim_without_disposable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as upload_dir:
            environment = {
                **os.environ,
                "APP_DEBUG": "1",
                "DATABASE_URL": "postgresql+psycopg://synthetic:synthetic@127.0.0.1:5432/sro",
                "MIGRATION_DATABASE_URL": "postgresql+psycopg://synthetic:synthetic@127.0.0.1:5432/sro",
                "REDIS_URL": "redis://synthetic:synthetic@127.0.0.1:1/0",
                "SRO_UPLOAD_DIR": upload_dir,
            }
            environment.pop("SRO_DISPOSABLE_DRILL", None)
            environment.pop("SRO_DRILL_CONTAINER_NAME", None)
            result = subprocess.run(
                [sys.executable, str(WORKER_SCRIPT)],
                input="[123]",
                capture_output=True,
                text=True,
                timeout=10,
                env=environment,
                check=False,
            )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.strip(), '{"guard_error": "SystemExit"}')
        self.assertEqual(result.stderr, "")

    def test_refuses_non_loopback_postgres_even_with_drill_marker(self) -> None:
        result = self.run_helper(
            disposable_marker=True,
            database_url="postgresql+psycopg://synthetic:synthetic@db.invalid/sro",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("loopback", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_refuses_loopback_database_without_restore_drill_identity(self) -> None:
        result = self.run_helper(
            disposable_marker=True,
            database_url="postgresql+psycopg://synthetic:synthetic@127.0.0.1:55432/sro",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("restore-drill container identity", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_refuses_loopback_database_when_restore_container_is_missing(self) -> None:
        result = self.run_helper(
            disposable_marker=True,
            drill_container="sro-restore-drill-987654321",
            database_url="postgresql+psycopg://synthetic:synthetic@127.0.0.1:55432/sro",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("loopback port is not owned by the restore drill", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_refuses_non_loopback_redis_even_with_disposable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as upload_dir:
            environment = {
                **os.environ,
                "APP_DEBUG": "1",
                "SRO_UPLOAD_DIR": upload_dir,
                "SRO_DISPOSABLE_DRILL": "1",
                "SRO_DRILL_CONTAINER_NAME": "sro-restore-drill-987654321",
                "SRO_DRILL_REDIS_CONTAINER_NAME": "sro-restore-drill-987654321-redis",
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            }
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from scripts.dev.lease_claim_load_safety import "
                    "verify_disposable_redis; "
                    "verify_disposable_redis('redis://redis.invalid:6379/0')",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                env=environment,
                check=False,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("loopback restore-drill container identity", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_refuses_redis_without_explicit_drill_marker(self) -> None:
        with tempfile.TemporaryDirectory() as upload_dir:
            environment = {
                **os.environ,
                "APP_DEBUG": "1",
                "SRO_UPLOAD_DIR": upload_dir,
                "SRO_DRILL_CONTAINER_NAME": "sro-restore-drill-987654321",
                "SRO_DRILL_REDIS_CONTAINER_NAME": "sro-restore-drill-987654321-redis",
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            }
            environment.pop("SRO_DISPOSABLE_DRILL", None)
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from scripts.dev.lease_claim_load_safety import "
                    "verify_disposable_redis; "
                    "verify_disposable_redis('redis://127.0.0.1:56379/0')",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                env=environment,
                check=False,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("disposable-drill marker", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def run_redis_identity_check(self, image: str, published_address: str) -> str:
        from scripts.dev.lease_claim_load_safety import verify_disposable_redis

        environment = {
            "SRO_DISPOSABLE_DRILL": "1",
            "SRO_DRILL_CONTAINER_NAME": "sro-restore-drill-987654321",
            "SRO_DRILL_REDIS_CONTAINER_NAME": "sro-restore-drill-987654321-redis",
        }
        responses = [
            subprocess.CompletedProcess([], 0, f"true|{image}\n", ""),
            subprocess.CompletedProcess([], 0, f"{published_address}\n", ""),
        ]
        with patch.dict(os.environ, environment, clear=False), patch(
            "scripts.dev.lease_claim_load_safety.subprocess.run",
            side_effect=responses,
        ) as run:
            try:
                verify_disposable_redis("redis://127.0.0.1:56379/0")
            except SystemExit as exc:
                self.assertEqual(run.call_count, 2)
                return str(exc)
        self.fail("Redis identity mismatch was accepted")

    def test_refuses_redis_container_with_an_unexpected_image_digest(self) -> None:
        error = self.run_redis_identity_check(
            "redis@sha256:unexpected",
            "127.0.0.1:56379",
        )

        self.assertIn("loopback port is not owned by the restore drill", error)

    def test_refuses_redis_url_on_a_port_not_published_by_drill(self) -> None:
        error = self.run_redis_identity_check(
            "redis@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499",
            "127.0.0.1:56380",
        )

        self.assertIn("loopback port is not owned by the restore drill", error)


if __name__ == "__main__":
    unittest.main()
