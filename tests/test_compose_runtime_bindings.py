from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "check_compose_runtime_bindings.py"
SPEC = importlib.util.spec_from_file_location("compose_runtime_bindings", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


class ComposeRuntimeBindingTests(unittest.TestCase):
    def _inspect_line(
        self,
        *,
        service: str,
        port_binding: str,
        running: bool = True,
        project: str = "deploy",
        config_file: str | None = None,
        image: str | None = None,
        command: list[str] | None = None,
    ) -> str:
        config_file = config_file or str(CHECKER.COMPOSE_FILE.resolve())
        if image is None:
            image = (
                "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
                if service == "db"
                else "redis@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499"
            )
        container_port = "5432/tcp" if service == "db" else "6379/tcp"
        if command is None:
            command = CHECKER._expected_bindings(CHECKER.COMPOSE_FILE)[service][4]
        return "\t".join(
            (
                f"/{project}-{service}-1",
                image,
                str(running).lower(),
                project,
                service,
                config_file,
                f'{{"{container_port}":[{port_binding}]}}',
                json.dumps(command),
            )
        )

    def _compose_result(self, stdout: str):
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    def test_accepts_running_services_with_the_expected_loopback_bindings(self) -> None:
        records = {
            "db": self._inspect_line(
                service="db",
                port_binding='{"HostIp":"127.0.0.1","HostPort":"55432"}',
            ),
            "redis": self._inspect_line(
                service="redis",
                port_binding='{"HostIp":"127.0.0.1","HostPort":"56379"}',
            ),
        }
        with patch.object(
            CHECKER,
            "_run",
            side_effect=[
                self._compose_result("db-id\n"),
                self._compose_result(records["db"]),
                self._compose_result("redis-id\n"),
                self._compose_result(records["redis"]),
            ],
        ):
            code, diagnostics = CHECKER.check_runtime_bindings()

        self.assertEqual(code, 0)
        self.assertEqual(len(diagnostics), 2)
        self.assertTrue(all("loopback verified" in item for item in diagnostics))

    def test_rejects_legacy_wildcard_and_ipv6_wildcard_bindings(self) -> None:
        records = {
            "db": self._inspect_line(
                service="db",
                port_binding='{"HostIp":"","HostPort":"55432"}',
            ),
            "redis": self._inspect_line(
                service="redis",
                port_binding='{"HostIp":"::","HostPort":"56379"}',
            ),
        }
        with patch.object(
            CHECKER,
            "_run",
            side_effect=[
                self._compose_result("db-id\n"),
                self._compose_result(records["db"]),
                self._compose_result("redis-id\n"),
                self._compose_result(records["redis"]),
            ],
        ):
            code, diagnostics = CHECKER.check_runtime_bindings()

        self.assertEqual(code, 1)
        self.assertTrue(all("runtime=mismatch" in item for item in diagnostics))

    def test_rejects_wrong_compose_identity_and_extra_published_ports(self) -> None:
        db_record = self._inspect_line(
            service="db",
            port_binding='{"HostIp":"127.0.0.1","HostPort":"55432"}',
            project="other-project",
        )
        redis_record = self._inspect_line(
            service="redis",
            port_binding='{"HostIp":"127.0.0.1","HostPort":"56379"}',
        ).replace(
            '{"6379/tcp":[{"HostIp":"127.0.0.1","HostPort":"56379"}]}',
            '{"6379/tcp":[{"HostIp":"127.0.0.1","HostPort":"56379"}],"6380/tcp":[{"HostIp":"0.0.0.0","HostPort":"6380"}]}',
        )
        with patch.object(
            CHECKER,
            "_run",
            side_effect=[
                self._compose_result("db-id\n"),
                self._compose_result(db_record),
                self._compose_result("redis-id\n"),
                self._compose_result(redis_record),
            ],
        ):
            code, diagnostics = CHECKER.check_runtime_bindings()

        self.assertEqual(code, 1)
        self.assertTrue(all("runtime=mismatch" in item for item in diagnostics))

    def test_reports_missing_service_without_inspecting_any_container(self) -> None:
        with patch.object(CHECKER, "_run", return_value=self._compose_result("")) as run:
            code, diagnostics = CHECKER.check_runtime_bindings()

        self.assertEqual(code, 1)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all("running_container_count=0" in item for item in diagnostics))

    def test_missing_port_bindings_are_reported_as_mismatch(self) -> None:
        records = {
            "db": self._inspect_line(service="db", port_binding=""),
            "redis": self._inspect_line(
                service="redis",
                port_binding='{"HostIp":"127.0.0.1","HostPort":"56379"}',
            ),
        }
        records["db"] = records["db"].replace('{"5432/tcp":[]}', "null")
        with patch.object(
            CHECKER,
            "_run",
            side_effect=[
                self._compose_result("db-id\n"),
                self._compose_result(records["db"]),
                self._compose_result("redis-id\n"),
                self._compose_result(records["redis"]),
            ],
        ):
            code, diagnostics = CHECKER.check_runtime_bindings()

        self.assertEqual(code, 1)
        self.assertIn(
            "service=db runtime=mismatch expected_image=postgres@sha256:"
            "721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea "
            "expected_binding=127.0.0.1:55432",
            diagnostics,
        )

    def test_rejects_runtime_image_that_differs_from_pinned_compose_digest(self) -> None:
        db_record = self._inspect_line(
            service="db",
            port_binding='{"HostIp":"127.0.0.1","HostPort":"55432"}',
            image="postgres:16-alpine",
        )
        redis_record = self._inspect_line(
            service="redis",
            port_binding='{"HostIp":"127.0.0.1","HostPort":"56379"}',
        )
        with patch.object(
            CHECKER,
            "_run",
            side_effect=[
                self._compose_result("db-id\n"),
                self._compose_result(db_record),
                self._compose_result("redis-id\n"),
                self._compose_result(redis_record),
            ],
        ):
            code, diagnostics = CHECKER.check_runtime_bindings()

        self.assertEqual(code, 1)
        self.assertTrue(any("service=db runtime=mismatch" in item for item in diagnostics))

    def test_rejects_redis_without_compose_aof_command(self) -> None:
        db_record = self._inspect_line(
            service="db",
            port_binding='{"HostIp":"127.0.0.1","HostPort":"55432"}',
        )
        redis_record = self._inspect_line(
            service="redis",
            port_binding='{"HostIp":"127.0.0.1","HostPort":"56379"}',
            command=["redis-server"],
        )
        with patch.object(
            CHECKER,
            "_run",
            side_effect=[
                self._compose_result("db-id\n"),
                self._compose_result(db_record),
                self._compose_result("redis-id\n"),
                self._compose_result(redis_record),
            ],
        ):
            code, diagnostics = CHECKER.check_runtime_bindings()

        self.assertEqual(code, 1)
        self.assertIn("service=redis runtime=mismatch", diagnostics[1])
        self.assertIn("command=mismatch", diagnostics[1])

    def test_accepts_redis_when_command_matches_compose_aof_policy(self) -> None:
        records = {
            "db": self._inspect_line(
                service="db",
                port_binding='{"HostIp":"127.0.0.1","HostPort":"55432"}',
            ),
            "redis": self._inspect_line(
                service="redis",
                port_binding='{"HostIp":"127.0.0.1","HostPort":"56379"}',
            ),
        }
        with patch.object(
            CHECKER,
            "_run",
            side_effect=[
                self._compose_result("db-id\n"),
                self._compose_result(records["db"]),
                self._compose_result("redis-id\n"),
                self._compose_result(records["redis"]),
            ],
        ):
            code, diagnostics = CHECKER.check_runtime_bindings()

        self.assertEqual(code, 0)
        self.assertIn("service=redis binding=loopback verified command=verified", diagnostics)

    def test_refuses_compose_image_without_immutable_digest(self) -> None:
        compose_text = """
services:
  db:
    image: postgres:16-alpine
    ports:
      - "127.0.0.1:55432:5432"
  redis:
    image: redis@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499
    ports:
      - "127.0.0.1:56379:6379"
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            compose_file = Path(temporary_directory) / "compose.yml"
            compose_file.write_text(compose_text, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "not pinned by digest"):
                CHECKER._expected_bindings(compose_file)


if __name__ == "__main__":
    unittest.main()
