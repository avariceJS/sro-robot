from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

POSTGRES_IMAGE = "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
DOCKER = "/usr/local/bin/docker"


def run_suite(mode: str, checkout: Path) -> int:
    python = os.environ["SRO_ROBOT_PYTHON"]
    modules = (
        [
            "tests.test_approval",
            "tests.test_task_event_duplicate_privacy.DuplicateEventPrivacyTests.test_duplicate_event_records_count_without_document_names",
            "tests.test_task_event_duplicate_privacy.DuplicateEventPrivacyTests.test_dynamic_failure_text_is_replaced_by_fixed_audit_message",
            "tests.test_task_event_duplicate_privacy.DuplicateEventPrivacyTests.test_audit_contract_rejects_unreviewed_kinds_and_payload_fields",
        ]
        if mode == "regression"
        else ["discover", "-s", "tests"]
    )
    return subprocess.run(
        [python, "-m", "unittest", *modules],
        cwd=checkout,
        env=os.environ.copy(),
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
    port = subprocess.run(
        [DOCKER, "port", container, "5432/tcp"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip().rsplit(":", 1)[1]
    return f"postgresql+psycopg://postgres@127.0.0.1:{port}/postgres"


def with_database(mode: str, checkout: Path) -> int:
    container = f"sro-ppv-audit-task-{uuid.uuid4().hex[:12]}"
    upload_root = tempfile.TemporaryDirectory(prefix="sro-ppv-audit-task-upload-")
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
        from app.db.base import session_scope
        from app.db.models import Document, IntakeSource, Task, TaskStatus, User

        command.upgrade(Config(str(checkout / "alembic.ini")), "head")
        engine = create_engine(database_url, connect_args={"connect_timeout": 5})
        with session_scope() as db:
            user = User(
                login=f"synthetic-{uuid.uuid4().hex}",
                full_name="Synthetic PPV Actor",
                password_hash="synthetic-only",
                sro_login="synthetic",
                is_active=True,
            )
            db.add(user)
            db.flush()
            document = Document(
                filename="synthetic.pdf",
                stored_path="/tmp/synthetic.pdf",
                content_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                source=IntakeSource.upload,
                uploaded_by_id=user.id,
            )
            db.add(document)
            db.flush()
            task = Task(
                document_id=document.id,
                scenario="person",
                profile="default",
                status=TaskStatus.needs_review,
                recognized={},
                values={},
                case_number="",
                assignee_id=user.id,
            )
            db.add(task)
            db.flush()
            user_id, task_id = user.id, task.id

        if mode in {"missing_recognized", "missing_ocr_claim", "valid_recognized", "permitted_events"}:
            if mode == "permitted_events":
                statements = (
                    (
                        "INSERT INTO public.event (task_id, user_id, kind, message, payload) "
                        "VALUES (:task_id, NULL, 'approval_invalidated', 'synthetic PPV', '{}'::jsonb)",
                        {"task_id": task_id},
                    ),
                    (
                        "INSERT INTO public.event (task_id, user_id, kind, message, payload) "
                        "VALUES (NULL, NULL, 'session_login_failed', 'synthetic PPV', '{}'::jsonb)",
                        {},
                    ),
                )
                with engine.begin() as connection:
                    for statement, parameters in statements:
                        connection.execute(text(statement), parameters)
                print("permitted_task_event_and_global_session=accepted")
                return 0

            kind = "ocr_claimed" if mode == "missing_ocr_claim" else "recognized"
            payload = {"attempt": 1, "generation": 1} if kind == "ocr_claimed" else {}
            task_value = None if mode in {"missing_recognized", "missing_ocr_claim"} else task_id
            rejected = False
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO public.event (task_id, user_id, kind, message, payload) "
                            "VALUES (:task_id, NULL, :kind, 'synthetic PPV', CAST(:payload AS jsonb))"
                        ),
                        {
                            "task_id": task_value,
                            "kind": kind,
                            "payload": json.dumps(payload),
                        },
                    )
            except Exception as exc:
                rejected = getattr(getattr(exc, "orig", None), "sqlstate", None) == "23514"

            if mode in {"missing_recognized", "missing_ocr_claim"}:
                print("PPV_REACHED", flush=True)
                print(f"{mode}_rejected={rejected}")
                assert rejected, f"database accepted task-scoped {kind} without task_id"
            else:
                assert not rejected
                print("valid_task_bound_recognized_event=accepted")
            return 0

        raise ValueError(f"unsupported mode: {mode}")
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
        raise SystemExit("usage: audit_task_context.py MODE")
    mode = sys.argv[1]
    checkout = Path(os.environ["PPV_CHECKOUT"]).resolve()
    if mode in {"regression", "suite"}:
        return run_suite(mode, checkout)
    return with_database(mode, checkout)


if __name__ == "__main__":
    raise SystemExit(main())
