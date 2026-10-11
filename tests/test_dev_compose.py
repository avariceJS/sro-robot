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
            "scripts/dev/lease_claim_load_safety.py",
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

    def test_restore_drill_exercises_separate_claim_worker_processes(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        drill = (repo_root / "scripts/dev/postgres-restore-drill.sh").read_text(encoding="utf-8")
        self.assertIn("SRO_LEASE_LOAD_MODE=process", drill)
        self.assertIn(
            "lease_claim_process_worker.py",
            drill + (repo_root / "scripts/dev/lease_claim_load_smoke.py").read_text(encoding="utf-8"),
        )

    def test_restore_drill_runs_isolated_rq_lease_worker_smoke(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        drill = (repo_root / "scripts/dev/postgres-restore-drill.sh").read_text(encoding="utf-8")
        self.assertIn("scripts/dev/rq_lease_worker_smoke.py", drill)
        self.assertIn('SRO_DRILL_REDIS_CONTAINER_NAME="$redis_container_name"', drill)
        smoke = (repo_root / "scripts/dev/rq_lease_worker_smoke.py").read_text(encoding="utf-8")
        self.assertIn('queue_name = f"lease-smoke-', smoke)
        self.assertIn("_verify_in_fresh_process", smoke)

    def test_restore_drill_runs_isolated_sro_failure_privacy_smoke(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        drill = (repo_root / "scripts/dev/postgres-restore-drill.sh").read_text(encoding="utf-8")
        smoke_path = "scripts/dev/rq_sro_error_privacy_smoke.py"
        self.assertIn(smoke_path, drill)
        self.assertIn('SRO_DISPOSABLE_DRILL=1', drill)
        self.assertIn('SRO_DRILL_REDIS_CONTAINER_NAME="$redis_container_name"', drill)
        smoke = (repo_root / smoke_path).read_text(encoding="utf-8")
        self.assertIn("verify_disposable_redis(settings.redis_url)", smoke)
        self.assertIn('queue_name = f"sro-error-privacy-', smoke)
        self.assertIn("FailedJobRegistry", smoke)

    def test_stateful_dev_services_bind_only_to_ipv4_loopback(self) -> None:
        compose_file = Path(__file__).resolve().parents[1] / "deploy/docker-compose.dev.yml"
        compose = yaml.safe_load(compose_file.read_text())

        self.assertEqual(compose["services"]["db"]["ports"], ["127.0.0.1:55432:5432"])
        self.assertEqual(compose["services"]["redis"]["ports"], ["127.0.0.1:56379:6379"])

    def test_dev_database_and_redis_passwords_are_required_from_environment(self) -> None:
        compose_file = Path(__file__).resolve().parents[1] / "deploy/docker-compose.dev.yml"
        compose = yaml.safe_load(compose_file.read_text())

        db_environment = compose["services"]["db"]["environment"]
        redis = compose["services"]["redis"]
        self.assertIn("POSTGRES_PASSWORD", db_environment)
        self.assertIn("${POSTGRES_PASSWORD:?", db_environment["POSTGRES_PASSWORD"])
        self.assertNotIn("sro_dev", db_environment["POSTGRES_PASSWORD"])
        self.assertIn("${REDIS_PASSWORD:?", redis["environment"]["REDIS_PASSWORD"])
        command = " ".join(redis["command"])
        self.assertIn("requirepass", command)
        self.assertIn("$$REDIS_PASSWORD", command)
        self.assertIn("*[!a-fA-F0-9]*", command)
        self.assertIn("-ge 32", command)
        self.assertLess(command.index("unset REDIS_PASSWORD"), command.index("exec redis-server"))
        self.assertIn("REDISCLI_AUTH", redis["healthcheck"]["test"][1])

    def test_dev_redis_queue_uses_persistent_append_only_storage(self) -> None:
        compose_file = Path(__file__).resolve().parents[1] / "deploy/docker-compose.dev.yml"
        compose = yaml.safe_load(compose_file.read_text())
        redis = compose["services"]["redis"]

        command = " ".join(redis["command"])
        self.assertIn("--appendonly yes", command)
        self.assertIn("--appendfsync everysec", command)
        self.assertEqual(redis["volumes"], ["redisdata:/data"])
        self.assertIn("redisdata", compose["volumes"])


if __name__ == "__main__":
    unittest.main()
