from __future__ import annotations

import asyncio
import hashlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from starlette.requests import Request
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.testclient import TestClient

from app.api.deps import current_user
from app.db.base import get_session
from app.db.models import IntakeSource
from app.main import app
from app.api import routes_tasks


def _request(request_id: str) -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/upload",
            "raw_path": b"/upload",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("robot.example", 443),
            "state": {"request_id": request_id},
        }
    )


class TaskRequestCorrelationTests(unittest.TestCase):
    def test_upload_http_request_id_reaches_creation_event(self) -> None:
        db = MagicMock()
        document = SimpleNamespace(id=3)
        task = SimpleNamespace(id=8)
        operation = SimpleNamespace(id=5)

        def fake_session():
            yield db

        app.dependency_overrides[get_session] = fake_session
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=23)

        with (
            patch.object(
                routes_tasks.tasks,
                "begin_intake_operation",
                return_value=(operation, True),
            ) as begin_operation,
            patch.object(routes_tasks.tasks, "store_document", return_value=document),
            patch.object(
                routes_tasks.tasks,
                "create_intake_item",
                return_value=SimpleNamespace(id=72),
            ) as create_item,
            patch.object(routes_tasks.tasks, "create_task", return_value=task) as create_task,
            patch.object(routes_tasks.outbox, "create_intake_intent", return_value=101),
            patch.object(routes_tasks.outbox, "dispatch_required") as dispatch,
        ):
            try:
                with TestClient(app, base_url="http://testserver") as client:
                    response = client.post(
                        "/upload",
                        files={"files": ("private.pdf", io.BytesIO(b"synthetic local test file"), "application/pdf")},
                        data={"idempotency_key": "a" * 48},
                        headers={
                            "Origin": "http://testserver",
                            "X-Request-ID": "d" * 32,
                        },
                        follow_redirects=False,
                    )
            finally:
                app.dependency_overrides.pop(get_session, None)
                app.dependency_overrides.pop(current_user, None)

        self.assertEqual(response.status_code, 303)
        request_id = response.headers["x-request-id"]
        self.assertRegex(request_id, r"^[0-9a-f]{32}$")
        self.assertNotEqual(request_id, "d" * 32)
        begin_operation.assert_called_once_with(
            db,
            user_id=23,
            idempotency_key="a" * 48,
            manifest_sha256=routes_tasks.tasks.intake_manifest(
                IntakeSource.upload.value,
                [hashlib.sha256(b"synthetic local test file").hexdigest()],
            ),
            source_metadata_sha256=routes_tasks.tasks.intake_source_metadata_manifest(
                ["private.pdf"]
            ),
        )
        create_task.assert_called_once_with(
            db,
            document,
            user_id=23,
            request_id=request_id,
            intake_operation_id=operation.id,
            intake_item_id=72,
        )
        create_item.assert_called_once_with(
            db,
            operation_id=operation.id,
            item_index=0,
            document=document,
            filename="private.pdf",
            source=IntakeSource.upload,
            received_by_id=23,
        )
        db.commit.assert_called_once_with()
        dispatch.assert_called_once_with(db, 101)

    def test_oversized_upload_is_rejected_before_persistence_or_enqueue(self) -> None:
        db = MagicMock()

        def fake_session():
            yield db

        app.dependency_overrides[get_session] = fake_session
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=23)

        with (
            patch.object(routes_tasks.settings, "upload_max_file_bytes", 4),
            patch.object(routes_tasks.settings, "upload_max_batch_bytes", 8),
            patch.object(routes_tasks.tasks, "store_document") as store_document,
            patch.object(routes_tasks.tasks, "create_task") as create_task,
            patch.object(routes_tasks.outbox, "create_intake_intent") as create_intent,
            patch.object(routes_tasks.outbox, "dispatch_required") as dispatch,
        ):
            try:
                with TestClient(app, base_url="http://testserver") as client:
                    response = client.post(
                        "/upload",
                        files={"files": ("private.pdf", b"12345", "application/pdf")},
                        data={"idempotency_key": "a" * 48},
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False,
                    )
            finally:
                app.dependency_overrides.pop(get_session, None)
                app.dependency_overrides.pop(current_user, None)

        self.assertEqual(response.status_code, 413)
        store_document.assert_not_called()
        create_task.assert_not_called()
        create_intent.assert_not_called()
        dispatch.assert_not_called()
        db.commit.assert_not_called()

    def test_oversized_source_filename_is_rejected_before_operation_or_file_write(self) -> None:
        db = MagicMock()

        def fake_session():
            yield db

        app.dependency_overrides[get_session] = fake_session
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=23)

        with (
            patch.object(routes_tasks.tasks, "begin_intake_operation") as begin_operation,
            patch.object(routes_tasks.tasks, "store_document") as store_document,
            patch.object(routes_tasks.tasks, "create_intake_item") as create_item,
            patch.object(routes_tasks.tasks, "create_task") as create_task,
            patch.object(routes_tasks.outbox, "create_intake_intent") as create_intent,
            patch.object(routes_tasks.outbox, "dispatch_required") as dispatch,
        ):
            try:
                with TestClient(app, base_url="http://testserver") as client:
                    response = client.post(
                        "/upload",
                        files={
                            "files": (
                                "f" * 252 + ".pdf",
                                b"synthetic filename boundary",
                                "application/pdf",
                            )
                        },
                        data={"idempotency_key": "a" * 48},
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False,
                    )
            finally:
                app.dependency_overrides.pop(get_session, None)
                app.dependency_overrides.pop(current_user, None)

        self.assertEqual(response.status_code, 422)
        self.assertIn("Имя файла превышает допустимую длину", response.text)
        self.assertNotIn("f" * 32, response.text)
        begin_operation.assert_not_called()
        store_document.assert_not_called()
        create_item.assert_not_called()
        create_task.assert_not_called()
        create_intent.assert_not_called()
        dispatch.assert_not_called()
        db.commit.assert_not_called()

    def test_nul_source_filename_is_rejected_before_operation_or_file_write(self) -> None:
        db = MagicMock()
        upload = StarletteUploadFile(
            file=io.BytesIO(b"synthetic nul filename"),
            filename="private\x00document.pdf",
        )

        class FakeForm:
            def get(self, key, default=None):
                return {"idempotency_key": "b" * 48}.get(key, default)

            def getlist(self, key):
                return [upload] if key == "files" else []

        class FormContext:
            async def __aenter__(self):
                return FakeForm()

            async def __aexit__(self, *_exc_info):
                return None

        request = SimpleNamespace(
            scope={"state": {"request_id": "a" * 32}},
            form=lambda **_kwargs: FormContext(),
        )

        with (
            patch.object(routes_tasks.tasks, "begin_intake_operation") as begin_operation,
            patch.object(routes_tasks.tasks, "store_document") as store_document,
            patch.object(routes_tasks.tasks, "create_intake_item") as create_item,
            patch.object(routes_tasks.tasks, "create_task") as create_task,
            patch.object(routes_tasks.outbox, "create_intake_intent") as create_intent,
            patch.object(routes_tasks.outbox, "dispatch_required") as dispatch,
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(
                    routes_tasks.upload_documents(
                        request,
                        SimpleNamespace(id=23),
                        db,
                    )
                )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(
            raised.exception.detail,
            "Имя файла содержит недопустимые символы",
        )
        begin_operation.assert_not_called()
        store_document.assert_not_called()
        create_item.assert_not_called()
        create_task.assert_not_called()
        create_intent.assert_not_called()
        dispatch.assert_not_called()
        db.commit.assert_not_called()

    def test_unavailable_existing_document_returns_safe_service_error(self) -> None:
        db = MagicMock()
        operation = SimpleNamespace(id=5)

        def fake_session():
            yield db

        app.dependency_overrides[get_session] = fake_session
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=23)

        with (
            patch.object(
                routes_tasks.tasks,
                "begin_intake_operation",
                return_value=(operation, True),
            ),
            patch.object(
                routes_tasks.tasks,
                "store_document",
                side_effect=routes_tasks.tasks.ExistingDocumentUnavailable,
            ),
            patch.object(routes_tasks.tasks, "create_task") as create_task,
            patch.object(routes_tasks.outbox, "create_intake_intent") as create_intent,
            patch.object(routes_tasks.outbox, "dispatch_required") as dispatch,
        ):
            try:
                with TestClient(app, base_url="http://testserver") as client:
                    response = client.post(
                        "/upload",
                        files={"files": ("private.pdf", b"synthetic retry", "application/pdf")},
                        data={"idempotency_key": "e" * 48},
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False,
                    )
            finally:
                app.dependency_overrides.pop(get_session, None)
                app.dependency_overrides.pop(current_user, None)

        self.assertEqual(response.status_code, 503)
        self.assertIn("Хранилище документа недоступно", response.text)
        self.assertNotIn("synthetic", response.text)
        create_task.assert_not_called()
        create_intent.assert_not_called()
        dispatch.assert_not_called()
        db.commit.assert_not_called()

    def test_upload_file_count_is_rejected_before_persistence(self) -> None:
        db = MagicMock()

        def fake_session():
            yield db

        app.dependency_overrides[get_session] = fake_session
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=23)

        with (
            patch.object(routes_tasks.settings, "upload_max_files", 1),
            patch.object(routes_tasks.tasks, "store_document") as store_document,
        ):
            try:
                with TestClient(app, base_url="http://testserver") as client:
                    response = client.post(
                        "/upload",
                        files=[
                            ("files", ("one.pdf", b"1", "application/pdf")),
                            ("files", ("two.pdf", b"2", "application/pdf")),
                        ],
                        data={"idempotency_key": "a" * 48},
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False,
                    )
            finally:
                app.dependency_overrides.pop(get_session, None)
                app.dependency_overrides.pop(current_user, None)

        self.assertEqual(response.status_code, 413)
        store_document.assert_not_called()
        db.commit.assert_not_called()

    def test_upload_batch_size_is_rejected_before_any_file_is_persisted(self) -> None:
        db = MagicMock()

        def fake_session():
            yield db

        app.dependency_overrides[get_session] = fake_session
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=23)

        with (
            patch.object(routes_tasks.settings, "upload_max_files", 2),
            patch.object(routes_tasks.settings, "upload_max_file_bytes", 8),
            patch.object(routes_tasks.settings, "upload_max_batch_bytes", 6),
            patch.object(routes_tasks.tasks, "store_document") as store_document,
        ):
            try:
                with TestClient(app, base_url="http://testserver") as client:
                    response = client.post(
                        "/upload",
                        files=[
                            ("files", ("one.pdf", b"1234", "application/pdf")),
                            ("files", ("two.pdf", b"5678", "application/pdf")),
                        ],
                        data={"idempotency_key": "a" * 48},
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False,
                    )
            finally:
                app.dependency_overrides.pop(get_session, None)
                app.dependency_overrides.pop(current_user, None)

        self.assertEqual(response.status_code, 413)
        store_document.assert_not_called()
        db.commit.assert_not_called()

    def test_empty_upload_is_rejected_before_persistence(self) -> None:
        db = MagicMock()

        def fake_session():
            yield db

        app.dependency_overrides[get_session] = fake_session
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=23)

        with patch.object(routes_tasks.tasks, "store_document") as store_document:
            try:
                with TestClient(app, base_url="http://testserver") as client:
                    response = client.post(
                        "/upload",
                        files={"files": ("empty.pdf", b"", "application/pdf")},
                        data={"idempotency_key": "a" * 48},
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False,
                    )
            finally:
                app.dependency_overrides.pop(get_session, None)
                app.dependency_overrides.pop(current_user, None)

        self.assertEqual(response.status_code, 422)
        store_document.assert_not_called()
        db.commit.assert_not_called()

    def test_upload_replay_returns_existing_task_without_creating_another(self) -> None:
        db = MagicMock()
        operation = SimpleNamespace(id=5)
        existing_task = SimpleNamespace(id=81)

        def fake_session():
            yield db

        app.dependency_overrides[get_session] = fake_session
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=23)

        with (
            patch.object(
                routes_tasks.tasks,
                "begin_intake_operation",
                return_value=(operation, False),
            ),
            patch.object(routes_tasks.tasks, "operation_tasks", return_value=[existing_task]),
            patch.object(routes_tasks.tasks, "log_intake_replay") as log_replay,
            patch.object(routes_tasks.tasks, "store_document") as store_document,
            patch.object(routes_tasks.tasks, "create_task") as create_task,
            patch.object(routes_tasks.outbox, "create_intake_intent", return_value=102),
            patch.object(routes_tasks.outbox, "dispatch_required") as dispatch,
        ):
            try:
                with TestClient(app, base_url="http://testserver") as client:
                    response = client.post(
                        "/upload",
                        files={"files": ("private.pdf", b"synthetic retry", "application/pdf")},
                        data={"idempotency_key": "e" * 48},
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False,
                    )
            finally:
                app.dependency_overrides.pop(get_session, None)
                app.dependency_overrides.pop(current_user, None)

        self.assertEqual(response.status_code, 303)
        store_document.assert_not_called()
        create_task.assert_not_called()
        log_replay.assert_called_once_with(db, [existing_task], 23, response.headers["x-request-id"])
        db.commit.assert_called_once_with()
        dispatch.assert_called_once_with(db, 102)

    def test_upload_queue_failure_returns_retryable_response_and_same_key_replays(self) -> None:
        db = MagicMock()
        document = SimpleNamespace(id=3)
        task = SimpleNamespace(id=81)
        operation = SimpleNamespace(id=5)

        def fake_session():
            yield db

        app.dependency_overrides[get_session] = fake_session
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=23)

        with (
            patch.object(
                routes_tasks.tasks,
                "begin_intake_operation",
                side_effect=[(operation, True), (operation, False)],
            ),
            patch.object(routes_tasks.tasks, "store_document", return_value=document) as store_document,
            patch.object(routes_tasks.tasks, "create_task", return_value=task) as create_task,
            patch.object(routes_tasks.tasks, "operation_tasks", return_value=[task]),
            patch.object(routes_tasks.tasks, "log_intake_replay") as log_replay,
            patch.object(routes_tasks.outbox, "create_intake_intent", return_value=103),
            patch.object(
                routes_tasks.outbox,
                "dispatch_required",
                side_effect=[routes_tasks.QueueUnavailable("unavailable"), None],
            ) as dispatch,
        ):
            try:
                with TestClient(app, base_url="http://testserver") as client:
                    first = client.post(
                        "/upload",
                        files={"files": ("private.pdf", io.BytesIO(b"synthetic retry"), "application/pdf")},
                        data={"idempotency_key": "c" * 48},
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False,
                    )
                    second = client.post(
                        "/upload",
                        files={"files": ("private.pdf", io.BytesIO(b"synthetic retry"), "application/pdf")},
                        data={"idempotency_key": "c" * 48},
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False,
                    )
            finally:
                app.dependency_overrides.pop(get_session, None)
                app.dependency_overrides.pop(current_user, None)

        self.assertEqual(first.status_code, 503)
        self.assertEqual(first.headers["retry-after"], "5")
        self.assertIn("задача могла быть поставлена", first.text)
        self.assertNotIn("unavailable", first.text)
        self.assertEqual(second.status_code, 303)
        self.assertEqual(second.headers["location"], "/")
        self.assertEqual(db.commit.call_count, 2)
        store_document.assert_called_once()
        create_task.assert_called_once()
        log_replay.assert_called_once()
        self.assertEqual(dispatch.call_count, 2)

    def test_upload_idempotency_key_conflict_does_not_persist_input(self) -> None:
        db = MagicMock()

        def fake_session():
            yield db

        app.dependency_overrides[get_session] = fake_session
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=23)

        with (
            patch.object(
                routes_tasks.tasks,
                "begin_intake_operation",
                side_effect=routes_tasks.tasks.IntakeOperationConflict("changed"),
            ) as begin_operation,
            patch.object(routes_tasks.tasks, "store_document") as store_document,
            patch.object(routes_tasks.tasks, "create_task") as create_task,
            patch.object(routes_tasks.outbox, "create_intake_intent") as create_intent,
            patch.object(routes_tasks.outbox, "dispatch_required") as dispatch,
        ):
            try:
                with TestClient(app, base_url="http://testserver") as client:
                    response = client.post(
                        "/upload",
                        files={"files": ("private.pdf", b"changed payload", "application/pdf")},
                        data={"idempotency_key": "f" * 48},
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False,
                    )
            finally:
                app.dependency_overrides.pop(get_session, None)
                app.dependency_overrides.pop(current_user, None)

        self.assertEqual(response.status_code, 409)
        begin_operation.assert_called_once_with(
            db,
            user_id=23,
            idempotency_key="f" * 48,
            manifest_sha256=routes_tasks.tasks.intake_manifest(
                IntakeSource.upload.value,
                [hashlib.sha256(b"changed payload").hexdigest()],
            ),
            source_metadata_sha256=routes_tasks.tasks.intake_source_metadata_manifest(
                ["private.pdf"]
            ),
        )
        store_document.assert_not_called()
        create_task.assert_not_called()
        create_intent.assert_not_called()
        dispatch.assert_not_called()
        db.commit.assert_not_called()

    def test_unqualified_kad_acquisition_is_rejected_before_persistence_or_enqueue(self) -> None:
        db = MagicMock()

        def fake_session():
            yield db

        app.dependency_overrides[get_session] = fake_session
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=23)

        with (
            patch.object(
                routes_tasks.tasks,
                "begin_intake_operation",
            ) as begin_operation,
            patch.object(routes_tasks.tasks, "create_kad_task") as create_kad_task,
            patch.object(routes_tasks.outbox, "create_intake_intent") as create_intent,
            patch.object(routes_tasks.outbox, "dispatch_required") as dispatch,
        ):
            try:
                with TestClient(app, base_url="http://testserver") as client:
                    response = client.post(
                        "/case-search",
                        data={
                            "case_number": "А99-12345/2099",
                            "idempotency_key": "b" * 48,
                        },
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False,
                    )
            finally:
                app.dependency_overrides.pop(get_session, None)
                app.dependency_overrides.pop(current_user, None)

        self.assertEqual(response.status_code, 503)
        self.assertIn("правила выбора акта", response.text)
        begin_operation.assert_not_called()
        create_kad_task.assert_not_called()
        create_intent.assert_not_called()
        dispatch.assert_not_called()
        db.commit.assert_not_called()

    def test_invalid_or_untrusted_request_state_is_not_propagated(self) -> None:
        for value in ("user-controlled", "b" * 31, "B" * 32, None):
            with self.subTest(value=value):
                request = _request(value) if value is not None else _request("c" * 32)
                if value is None:
                    request.scope["state"].pop("request_id")
                else:
                    request.scope["state"]["request_id"] = value
                self.assertIsNone(routes_tasks._server_request_id(request))


if __name__ == "__main__":
    unittest.main()
