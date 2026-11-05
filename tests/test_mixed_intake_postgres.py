"""Exact immutable batch membership with synthetic rows in disposable PostgreSQL."""

from __future__ import annotations

import os
import unittest

from app.db.base import session_scope
from app.db.models import (
    Document,
    IntakeItem,
    IntakeOperation,
    IntakeSource,
    Task,
    TaskStatus,
    UploadTransportChunkReceipt,
    User,
)
from app.services import outbox, tasks, upload_transport
from app.services.mixed_intake import batch_registry_groups
from tests.postgres_safety import assert_disposable_postgres


@unittest.skipUnless(os.environ.get("SRO_TEST_POSTGRES") == "1", "requires disposable PostgreSQL")
class MixedIntakePostgresTests(unittest.TestCase):
    def test_same_bytes_twice_and_unrelated_operation_stay_distinct(self) -> None:
        assert_disposable_postgres()
        digest = os.urandom(32).hex()
        filename = "synthetic-mixed.heic"
        with session_scope() as db:
            user = User(login=f"mixed-{os.urandom(8).hex()}", full_name="Synthetic staff",
                        password_hash="synthetic-only", sro_login="synthetic")
            db.add(user)
            db.flush()
            files = [
                {"filename": filename, "size": 1, "sha256": digest}
                for _ in range(3)
            ]
            batch, created = upload_transport.register_batch(
                db, creator_id=user.id, request_key=os.urandom(24).hex(),
                files_per_chunk=2, files=files,
            )
            self.assertTrue(created)
            batch_id = batch.id
            document = Document(
                filename=filename, stored_path=f"/synthetic/{digest}.heic",
                content_hash=digest, source=IntakeSource.batch, uploaded_by_id=user.id,
            )
            db.add(document)
            db.flush()

            def operation_with_tasks(count: int) -> tuple[IntakeOperation, list[Task]]:
                source = IntakeSource.batch if count > 1 else IntakeSource.upload
                operation = IntakeOperation(
                    user_id=user.id, idempotency_key=os.urandom(24).hex(),
                    manifest_sha256=tasks.intake_manifest(source.value, [digest] * count),
                    source_metadata_sha256=tasks.intake_source_metadata_manifest([filename] * count),
                )
                db.add(operation)
                db.flush()
                task_rows = []
                for index in range(count):
                    item = IntakeItem(
                        operation_id=operation.id, item_index=index,
                        document_id=document.id, filename=filename, source=source,
                        received_by_id=user.id,
                    )
                    db.add(item)
                    db.flush()
                    recognized = (
                        {"last_name": {"value": "Тестова"}, "first_name": {"value": "Анна"}}
                        if index == 0 else
                        {"case_number": {"value": "А40-123/2026"},
                         "_applicant_evidence_v1": {
                             "role_source": "applicant_role:v1",
                             "name": "ООО Заявитель",
                             "case_number": "А40-123/2026",
                         }}
                    )
                    task = Task(
                        document_id=document.id, intake_operation_id=operation.id,
                        intake_item_id=item.id, assignee_id=user.id,
                        scenario="person_card", profile="court_ruling",
                        status=TaskStatus.needs_review, recognized=recognized, values={},
                    )
                    db.add(task)
                    db.flush()
                    outbox.create_intake_intent(
                        db, task_id=task.id, operation_id=operation.id, job_kind="ocr",
                    )
                    task_rows.append(task)
                return operation, task_rows

            accepted, accepted_tasks = operation_with_tasks(2)
            unrelated, unrelated_tasks = operation_with_tasks(1)
            self.assertNotEqual(accepted.id, unrelated.id)
            db.add(UploadTransportChunkReceipt(
                batch_id=batch.id, chunk_index=0,
                operation_id=accepted.id, accepted_by_id=user.id,
                batch_manifest_sha256=batch.manifest_sha256,
                operation_manifest_sha256=accepted.manifest_sha256,
                source_metadata_sha256=accepted.source_metadata_sha256,
            ))
            self.accepted_ids = [task.id for task in accepted_tasks]
            self.unrelated_id = unrelated_tasks[0].id

        with session_scope() as db:
            grouped = batch_registry_groups(db, batch_id)
            self.assertEqual(grouped["total"], 3)
            self.assertEqual(grouped["counts"]["persons"], 1)
            self.assertEqual(grouped["counts"]["assignments"], 1)
            self.assertEqual(grouped["counts"]["pending"], 1)
            self.assertEqual(grouped["groups"]["persons"][0]["task_id"], self.accepted_ids[0])
            self.assertEqual(grouped["groups"]["assignments"][0]["task_id"], self.accepted_ids[1])
            self.assertNotIn(
                self.unrelated_id,
                [row["task_id"] for rows in grouped["groups"].values() for row in rows],
            )


if __name__ == "__main__":
    unittest.main()
