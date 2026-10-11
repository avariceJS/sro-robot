from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace


ROOT = Path.cwd()
SCRIPT = ROOT / "scripts" / "dev" / "check_compose_runtime_bindings.py"
SPEC = importlib.util.spec_from_file_location("compose_runtime_bindings", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("checker module unavailable")
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)

POSTGRES_IMAGE = "postgres@sha256:" + "1" * 64
REDIS_IMAGE = "redis@sha256:" + "2" * 64
EXPECTED_REDIS_COMMAND = [
    "redis-server",
    "--appendonly",
    "yes",
    "--appendfsync",
    "everysec",
]


def run_case(scenario: str) -> tuple[int, list[str]]:
    scratch = Path(os.environ["PPV_SCRATCH"])
    with tempfile.TemporaryDirectory(dir=scratch) as temp:
        compose_file = Path(temp) / "compose.yml"
        compose_file.write_text(
            "services:\n"
            "  db:\n"
            f"    image: {POSTGRES_IMAGE}\n"
            "    ports: [\"127.0.0.1:55432:5432\"]\n"
            "  redis:\n"
            f"    image: {REDIS_IMAGE}\n"
            "    ports: [\"127.0.0.1:56379:6379\"]\n"
            "    command: [\"redis-server\", \"--appendonly\", \"yes\", "
            "\"--appendfsync\", \"everysec\"]\n",
            encoding="utf-8",
        )
        redis_command = {
            "matching": EXPECTED_REDIS_COMMAND,
            "missing": ["redis-server"],
            "disabled": ["redis-server", "--appendonly", "no"],
        }[scenario]

        def fake_run(command: list[str]) -> SimpleNamespace:
            if command[:3] == ["docker", "compose", "-f"]:
                service = command[-1]
                return SimpleNamespace(returncode=0, stdout=f"synthetic-{service}\n", stderr="")
            if command[:2] != ["docker", "inspect"]:
                raise AssertionError("unexpected Docker command shape")
            template = command[3]
            service = "db" if "synthetic-db" in command else "redis"
            container_port = "5432/tcp" if service == "db" else "6379/tcp"
            host_port = "55432" if service == "db" else "56379"
            image = POSTGRES_IMAGE if service == "db" else REDIS_IMAGE
            runtime_command = ["postgres"] if service == "db" else redis_command
            fields = [
                f"/deploy-{service}-SYNTHETIC-ID",
                image,
                "true",
                "deploy",
                service,
                str(compose_file.resolve()),
                json.dumps({container_port: [{"HostIp": "127.0.0.1", "HostPort": host_port}]}),
            ]
            if ".Config.Cmd" in template:
                fields.append(json.dumps(runtime_command))
            return SimpleNamespace(returncode=0, stdout="\t".join(fields), stderr="")

        CHECKER._run = fake_run
        return CHECKER.check_runtime_bindings(compose_file)


def main() -> int:
    mode = sys.argv[1]
    scenario = sys.argv[2]
    code, diagnostics = run_case(scenario)
    if mode == "exploit":
        print("PPV_REACHED", flush=True)
        assert code == 1 and any("command=mismatch" in item for item in diagnostics)
    elif mode == "variant":
        print("PPV_REACHED", flush=True)
        assert code == 1 and any("command=mismatch" in item for item in diagnostics)
    elif mode == "control":
        assert code == 0 and len(diagnostics) == 2
    elif mode == "behavior":
        assert code == 0
        print(json.dumps({"status": code, "db_diagnostic": diagnostics[0]}, sort_keys=True))
    elif mode == "security":
        rendered = "\n".join(diagnostics)
        assert "SYNTHETIC-ID" not in rendered
        assert str(Path(os.environ["PPV_SCRATCH"])) not in rendered
        assert all("command=mismatch" not in item for item in diagnostics if "service=db" in item)
    else:
        raise ValueError("unsupported validation mode")
    print(f"{mode}_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
