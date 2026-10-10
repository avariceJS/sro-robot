from __future__ import annotations

import importlib.util
from pathlib import Path
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
    ) -> str:
        config_file = config_file or str(CHECKER.COMPOSE_FILE.resolve())
        image = "postgres:16-alpine" if service == "db" else "redis:7-alpine"
        container_port = "5432/tcp" if service == "db" else "6379/tcp"
        return "\t".join(
            (
                f"/{project}-{service}-1",
                image,
                str(running).lower(),
                project,
                service,
                config_file,
                f'{{"{container_port}":[{port_binding}]}}',
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
        self.assertTrue(all("binding=mismatch" in item for item in diagnostics))

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
        self.assertTrue(all("binding=mismatch" in item for item in diagnostics))

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
        self.assertIn("service=db binding=mismatch expected=127.0.0.1:55432", diagnostics)


if __name__ == "__main__":
    unittest.main()
