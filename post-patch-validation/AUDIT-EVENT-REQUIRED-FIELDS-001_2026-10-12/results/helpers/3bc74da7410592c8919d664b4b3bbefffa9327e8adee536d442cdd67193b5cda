from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

POSTGRES_IMAGE = (
    "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
)
DOCKER = "/usr/local/bin/docker"
REQUIRED_KINDS = (
    "ocr_attempts_exhausted",
    "ocr_claimed",
    "ocr_claim_completed",
    "ocr_claim_failed",
    "operator_action_not_applied",
    "task_claimed",
    "fields_saved",
    "approved",
    "local_validation_requested",
    "writing_started",
    "duplicate",
    "local_validation_completed",
    "preview_started",
    "live_save_blocked",
)


def run_suite(mode: str, checkout: Path) -> int:
    python = os.environ["SRO_ROBOT_PYTHON"]
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join(
        ["/usr/local/bin", "/bin", "/usr/bin", environment.get("PATH", "")]
    )
    modules = (
        ["tests.test_task_event_duplicate_privacy", "tests.test_approval"]
        if mode == "regression"
        else ["discover", "-s", "tests"]
    )
    return subprocess.run(
        [python, "-m", "unittest", *modules],
        cwd=checkout,
        env=environment,
        check=False,
    ).returncode


def start_database(container: str) -> str:
    subprocess.run(
        [
            DOCKER,
            "run",
            "--rm",
            "-d",
            "--name",
            container,
            "-p",
            "127.0.0.1::5432",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            POSTGRES_IMAGE,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    for _ in range(60):
        ready = subprocess.run(
            [DOCKER, "exec", container, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
        if ready.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("disposable PostgreSQL did not become ready")
    port_result = subprocess.run(
        [DOCKER, "port", container, "5432/tcp"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    port = port_result.stdout.strip().rsplit(":", 1)[1]
    return f"postgresql+psycopg://postgres@127.0.0.1:{port}/postgres"


def with_database(mode: str, checkout: Path) -> int:
    container = f"sro-ppv-audit-fields-{uuid.uuid4().hex[:12]}"
    upload_root = tempfile.TemporaryDirectory(prefix="sro-ppv-audit-fields-upload-")
    engine = None
    try:
        database_url = start_database(container)
        os.environ.update(
            {
                "APP_DEBUG": "1",
                "DATABASE_URL": database_url,
                "MIGRATION_DATABASE_URL": database_url,
                "REDIS_URL": "redis://127.0.0.1:1/0",
                "SRO_UPLOAD_DIR": upload_root.name,
            }
        )
        sys.path.insert(0, str(checkout))
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import create_engine, text

        command.upgrade(Config(str(checkout / "alembic.ini")), "head")
        engine = create_engine(database_url, connect_args={"connect_timeout": 5})

        if mode == "valid_approved":
            payload = {"values_sha256": "a" * 64}
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO public.event (kind, message, payload) "
                        "VALUES ('approved', 'ignored', CAST(:payload AS jsonb))"
                    ),
                    {"payload": __import__("json").dumps(payload)},
                )
                row = connection.execute(
                    text("SELECT message, payload FROM public.event WHERE kind='approved'")
                ).one()
            assert row[0] == "Оператор подтвердил поля"
            assert row[1]["values_sha256"] == "a" * 64
            print("valid_approved_payload=accepted")
            return 0

        if mode == "missing_approved":
            rejected = False
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO public.event (kind, message, payload) "
                            "VALUES ('approved', 'ignored', '{}'::jsonb)"
                        )
                    )
            except Exception as exc:
                rejected = getattr(getattr(exc, "orig", None), "sqlstate", None) == "23514"
            print("PPV_REACHED", flush=True)
            print(f"missing_approved_payload=rejected:{rejected}")
            assert rejected, "database accepted approved event without its value digest"
            return 0

        if mode == "missing_required":
            rejected = []
            for kind in REQUIRED_KINDS:
                try:
                    with engine.begin() as connection:
                        connection.execute(
                            text(
                                "INSERT INTO public.event (kind, message, payload) "
                                "VALUES (:kind, 'ignored', '{}'::jsonb)"
                            ),
                            {"kind": kind},
                        )
                except Exception as exc:
                    rejected.append(
                        getattr(getattr(exc, "orig", None), "sqlstate", None) == "23514"
                    )
                else:
                    rejected.append(False)
            print("PPV_REACHED", flush=True)
            print(f"mandatory_payload_kinds={sum(rejected)}/{len(REQUIRED_KINDS)}")
            assert all(rejected), "one or more event kinds accepted missing required payload fields"
            return 0

        if mode == "security":
            rejected = False
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO public.event (kind, message, payload) "
                            "VALUES ('approved', 'private', '{\"extra\":\"synthetic\"}'::jsonb)"
                        )
                    )
            except Exception as exc:
                rejected = getattr(getattr(exc, "orig", None), "sqlstate", None) == "23514"
            assert rejected
            print("unsupported_payload_field=blocked")
            return 0

        raise ValueError(f"unsupported database check mode: {mode}")
    finally:
        if engine is not None:
            engine.dispose()
        subprocess.run(
            [DOCKER, "rm", "-f", container],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        upload_root.cleanup()


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: audit_payload_contract.py MODE")
    mode = sys.argv[1]
    checkout = Path(os.environ["PPV_CHECKOUT"]).resolve()
    if mode in {"regression", "suite"}:
        return run_suite(mode, checkout)
    return with_database(mode, checkout)


if __name__ == "__main__":
    raise SystemExit(main())
