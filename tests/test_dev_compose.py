from __future__ import annotations

import unittest
from pathlib import Path

import yaml


class DevelopmentComposeSecurityTests(unittest.TestCase):
    def test_stateful_dev_images_are_pinned_by_digest(self) -> None:
        compose_file = Path(__file__).resolve().parents[1] / "deploy/docker-compose.dev.yml"
        compose = yaml.safe_load(compose_file.read_text())

        self.assertEqual(
            compose["services"]["db"]["image"],
            "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea",
        )
        self.assertEqual(
            compose["services"]["redis"]["image"],
            "redis@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499",
        )

    def test_disposable_drills_use_the_compose_postgres_digest(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        expected = (
            "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
        )
        for relative_path in (
            "scripts/dev/postgres-restore-drill.sh",
            "scripts/dev/postgres-runtime-role-smoke.sh",
            "scripts/dev/postgres-existing-db-adoption-drill.py",
            "scripts/dev/lease_claim_load_smoke.py",
            "tests/postgres_safety.py",
        ):
            with self.subTest(path=relative_path):
                self.assertIn(expected, (repo_root / relative_path).read_text(encoding="utf-8"))

    def test_restore_drill_uses_the_compose_redis_digest(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        expected = (
            "redis@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499"
        )
        drill = (repo_root / "scripts/dev/postgres-restore-drill.sh").read_text(encoding="utf-8")
        self.assertIn(expected, drill)

    def test_stateful_dev_services_bind_only_to_ipv4_loopback(self) -> None:
        compose_file = Path(__file__).resolve().parents[1] / "deploy/docker-compose.dev.yml"
        compose = yaml.safe_load(compose_file.read_text())

        self.assertEqual(compose["services"]["db"]["ports"], ["127.0.0.1:55432:5432"])
        self.assertEqual(compose["services"]["redis"]["ports"], ["127.0.0.1:56379:6379"])

    def test_dev_redis_queue_uses_persistent_append_only_storage(self) -> None:
        compose_file = Path(__file__).resolve().parents[1] / "deploy/docker-compose.dev.yml"
        compose = yaml.safe_load(compose_file.read_text())
        redis = compose["services"]["redis"]

        self.assertIn("--appendonly", redis["command"])
        self.assertIn("yes", redis["command"])
        self.assertIn("--appendfsync", redis["command"])
        self.assertIn("everysec", redis["command"])
        self.assertEqual(redis["volumes"], ["redisdata:/data"])
        self.assertIn("redisdata", compose["volumes"])


if __name__ == "__main__":
    unittest.main()
