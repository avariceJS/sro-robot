from __future__ import annotations

import unittest
from pathlib import Path

import yaml


class DevelopmentComposeSecurityTests(unittest.TestCase):
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
