#!/usr/bin/env python3
"""Exercise explicit case-package work in loopback Chromium and disposable PostgreSQL."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.parse import parse_qs, unquote, urljoin, urlsplit
from urllib.request import urlopen
import uuid

from playwright.sync_api import sync_playwright
from rq import Worker
from sqlalchemy import func, select
from rq.job import Job

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.db.base import SessionLocal
from app.db.models import (
    AssignmentDraft,
    AssignmentDraftRevision,
    CasePackageTaskEvent,
    DebtorCardDraft,
    Document,
    Event,
    IntakeItem,
    IntakeOperation,
    IntakeSource,
    JobOutbox,
    OcrRetryRun,
    Task,
    TaskStatus,
    TaskStatusHistory,
    User,
)
from app.services import case_packages
from app.services import outbox
from app.services import leases, tasks
from app.settings import settings
from app.workers.queues import ocr_queue
from scripts.dev.bulk_benchmark import make_signed_cookie
from scripts.dev.lease_claim_load_safety import verify_disposable_redis
from tests.postgres_safety import assert_disposable_postgres


def allow_loopback(route) -> None:
    target = urlsplit(route.request.url)
    if target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost", "::1"}:
        route.abort("blockedbyclient")
        return
    route.continue_()


def start_app(base_url: str, log_path: Path) -> subprocess.Popen:
    target = urlsplit(base_url)
    with socket.socket() as probe:
        if probe.connect_ex((target.hostname or "", target.port or 0)) == 0:
            raise SystemExit("Refusing to use a loopback port owned by another process")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(target.port), "--log-level", "warning"],
        cwd=ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log_file.close()
            raise RuntimeError(f"Owned local web process exited ({process.returncode})")
        try:
            with urlopen(base_url + "/healthz", timeout=1) as response:
                if response.status == 200:
                    log_file.close()
                    return process
        except (OSError, URLError):
            time.sleep(0.2)
    stop_app(process)
    log_file.close()
    raise TimeoutError("Owned loopback web process did not become ready")


def stop_app(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def seed_browser_inputs(user_id: int) -> tuple[int, int, int]:
    suffix = uuid.uuid4().hex
    payload = f"synthetic browser case act {suffix}".encode()
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if user is None:
            raise RuntimeError("Synthetic browser user was not persisted")
        document = Document(
            filename=f"browser-document-{suffix}.pdf",
            stored_path=f"/tmp/{suffix}.pdf",
            content_hash=hashlib.sha256(payload).hexdigest(),
            source=IntakeSource.upload,
            uploaded_by_id=user_id,
        )
        db.add(document)
        db.flush()
        operation = IntakeOperation(
            user_id=user_id,
            idempotency_key=uuid.uuid4().hex * 2,
            manifest_sha256=hashlib.sha256(f"manifest-{suffix}".encode()).hexdigest(),
            source_metadata_sha256=None,
        )
        db.add(operation)
        db.flush()
        operation_id = operation.id
        occurrence = IntakeItem(
            operation_id=operation.id,
            item_index=0,
            document_id=document.id,
            filename=f"browser-source-occurrence-{suffix}.pdf",
            source=IntakeSource.upload,
            received_by_id=user_id,
        )
        db.add(occurrence)
        db.flush()
        task = Task(
            document_id=document.id,
            scenario="person_card",
            profile="default",
            status=TaskStatus.needs_review,
            recognized={},
            values={},
            case_number=f"А00-{suffix[:8]}/2026",
            intake_operation_id=operation.id,
            intake_item_id=occurrence.id,
        )
        db.add(task)
        db.flush()
        task_id = task.id
        next_document = Document(
            filename=f"browser-next-document-{suffix}.pdf",
            stored_path=f"/tmp/{suffix}-next.pdf",
            content_hash=hashlib.sha256(f"next-{suffix}".encode()).hexdigest(),
            source=IntakeSource.upload,
            uploaded_by_id=user_id,
        )
        db.add(next_document)
        db.flush()
        next_occurrence = IntakeItem(
            operation_id=operation.id,
            item_index=1,
            document_id=next_document.id,
            filename=f"browser-next-source-{suffix}.pdf",
            source=IntakeSource.upload,
            received_by_id=user_id,
        )
        db.add(next_occurrence)
        db.flush()
        next_task = Task(
            document_id=next_document.id,
            scenario="person_card",
            profile="default",
            status=TaskStatus.needs_review,
            recognized={
                "case_number": {"value": f"А00-{suffix[:8]}/2026", "confidence": 1.0},
                "doc_date": {"value": "21.10.2026", "confidence": 1.0},
                "last_name": {"value": "Синтетическая", "confidence": 1.0},
                "first_name": {"value": "Проверка", "confidence": 1.0},
            },
            values={},
            case_number=f"А00-{suffix[:8]}/2026",
            intake_operation_id=operation.id,
            intake_item_id=next_occurrence.id,
        )
        db.add(next_task)
        db.flush()
        next_task_id = next_task.id
        for index in range(26):
            similar_document = Document(
                filename=f"court-act-{index:02d}-{suffix}.pdf",
                stored_path=f"/tmp/{suffix}-{index:02d}.pdf",
                content_hash=hashlib.sha256(f"similar-{suffix}-{index}".encode()).hexdigest(),
                source=IntakeSource.upload,
                uploaded_by_id=user_id,
            )
            db.add(similar_document)
            db.flush()
            db.add(Task(
                document_id=similar_document.id,
                scenario="person_card",
                profile="default",
                status=TaskStatus.needs_review,
                recognized={},
                values={},
                case_number="А77-1/2026",
            ))
        for index in range(46):
            case_packages.create_once(
                db,
                user_id=user_id,
                request_key=uuid.uuid4().hex * 2,
                title=f"Synthetic browser paging {suffix[:8]} {index:02d}",
                case_number="",
            )
        db.commit()
    return task_id, next_task_id, operation_id


def seed_failed_ocr_retry_parent(user_id: int) -> tuple[int, int, str]:
    """Create a real synthetic upload receipt and a fenced local OCR failure."""
    suffix = uuid.uuid4().hex
    filename = f"synthetic-retry-source-{suffix[:8]}.pdf"
    content = _synthetic_pdf(f"Synthetic OCR retry source {suffix}")
    with SessionLocal() as db:
        operation = IntakeOperation(
            user_id=user_id,
            idempotency_key=uuid.uuid4().hex * 2,
            manifest_sha256=tasks.intake_manifest(
                IntakeSource.upload.value, [hashlib.sha256(content).hexdigest()]
            ),
            source_metadata_sha256=tasks.intake_source_metadata_manifest([filename]),
        )
        db.add(operation)
        db.flush()
        document = tasks.store_document(
            db, filename, content, IntakeSource.upload, user_id
        )
        item = tasks.create_intake_item(
            db,
            operation_id=operation.id,
            item_index=0,
            document=document,
            filename=filename,
            source=IntakeSource.upload,
            received_by_id=user_id,
        )
        task = tasks.create_task(
            db,
            document,
            user_id=user_id,
            scenario="person_card",
            intake_operation_id=operation.id,
            intake_item_id=item.id,
        )
        claim = leases.claim_ocr(db, task.id)
        if claim is None or not leases.fail_ocr(
            db,
            claim,
            "SyntheticTransientOCRFailure",
            employee_message="Synthetic OCR failure for retry browser check",
        ):
            raise AssertionError("Synthetic OCR lease did not reach its confirmed failure state")
        witness_id = db.scalar(
            select(TaskStatusHistory.id)
            .where(TaskStatusHistory.task_id == task.id)
            .order_by(TaskStatusHistory.id.desc())
            .limit(1)
        )
        task_id = task.id
        db.commit()
    return task_id, int(witness_id), filename


def _synthetic_pdf(label: str) -> bytes:
    safe_label = label.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 18 Tf 72 720 Td ({safe_label}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n")
    xref_offset = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    result.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(result)


def _write_synthetic_stored_pdf(content: bytes) -> Path:
    digest = hashlib.sha256(content).hexdigest()
    path = settings.upload_dir / f"{digest}.pdf"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
    return path


def seed_assignment_evidence(
    user_id: int, source_task_id: int, package_id: int, created_paths: list[Path]
) -> tuple[int, int, int]:
    """Create two safe, readable synthetic acts and linked internal drafts."""
    source_bytes = _synthetic_pdf(f"synthetic original court act {uuid.uuid4().hex}")
    second_bytes = _synthetic_pdf(f"synthetic later court act {uuid.uuid4().hex}")
    source_path = _write_synthetic_stored_pdf(source_bytes)
    created_paths.append(source_path)
    second_path = _write_synthetic_stored_pdf(second_bytes)
    created_paths.append(second_path)
    with SessionLocal() as db:
        source_task = db.get(Task, source_task_id)
        source_item = db.get(IntakeItem, source_task.intake_item_id)
        source_document = db.get(Document, source_task.document_id)
        source_document.stored_path = str(source_path)
        source_document.content_hash = hashlib.sha256(source_bytes).hexdigest()

        suffix = uuid.uuid4().hex
        operation = IntakeOperation(
            user_id=user_id,
            idempotency_key=uuid.uuid4().hex * 2,
            manifest_sha256=hashlib.sha256(f"assignment-evidence-{suffix}".encode()).hexdigest(),
        )
        db.add(operation)
        db.flush()
        later_filename = f"renamed-later-act-{suffix[:8]}.pdf"
        later_document = Document(
            filename="synthetic-later-act.pdf",
            stored_path=str(second_path),
            content_hash=hashlib.sha256(second_bytes).hexdigest(),
            source=IntakeSource.upload,
            uploaded_by_id=user_id,
        )
        db.add(later_document)
        db.flush()
        later_item = IntakeItem(
            operation_id=operation.id,
            item_index=0,
            document_id=later_document.id,
            filename=later_filename,
            source=IntakeSource.upload,
            received_by_id=user_id,
        )
        db.add(later_item)
        db.flush()
        later_task = Task(
            document_id=later_document.id,
            scenario="person_card",
            profile="default",
            status=TaskStatus.needs_review,
            recognized={},
            values={},
            case_number=source_task.case_number,
            intake_operation_id=operation.id,
            intake_item_id=later_item.id,
        )
        db.add(later_task)
        db.flush()
        later_task_id = later_task.id
        case_packages.set_task_membership(
            db, package_id=package_id, task_id=later_task_id,
            actor_id=user_id, included=True, expected_event_id=0,
        )
        card = DebtorCardDraft(
            entity_kind="person",
            display_name="Синтетический Должник Для Сверки",
            preparation_state="reviewed",
            values={},
            source_task_id=source_task_id,
            created_by_id=user_id,
            updated_by_id=user_id,
        )
        db.add(card)
        db.flush()
        assignment = AssignmentDraft(
            debtor_card_id=card.id,
            source_task_id=source_task_id,
            case_number=source_task.case_number,
            status="draft",
            values={"comments": "Сохранённый комментарий"},
            created_by_id=user_id,
            updated_by_id=user_id,
        )
        manual_assignment = AssignmentDraft(
            debtor_card_id=card.id,
            source_task_id=None,
            case_number="",
            status="draft",
            values={},
            created_by_id=user_id,
            updated_by_id=user_id,
        )
        db.add_all([assignment, manual_assignment])
        db.flush()
        assignment_id, manual_assignment_id = assignment.id, manual_assignment.id
        db.commit()
    return assignment_id, manual_assignment_id, later_task_id


def seed_history_operations(user_id: int, count: int = 50) -> list[tuple[int, int]]:
    operations = []
    with SessionLocal() as db:
        for index in range(count):
            suffix = uuid.uuid4().hex
            operation = IntakeOperation(
                user_id=user_id,
                idempotency_key=uuid.uuid4().hex * 2,
                manifest_sha256=hashlib.sha256(f"history-{suffix}".encode()).hexdigest(),
            )
            db.add(operation)
            db.flush()
            filename = f"synthetic-recovery-{index:02d}.pdf"
            document = Document(
                filename=filename,
                stored_path=f"/tmp/{suffix}.pdf",
                content_hash=hashlib.sha256(f"history-document-{suffix}".encode()).hexdigest(),
                # Exercise both source orders in the same paginated UI fixture:
                # the first row has a KAD receipt; the newest row has a batch
                # receipt for bytes whose canonical document source is KAD.
                source=(
                    IntakeSource.kad
                    if index in (0, count - 1)
                    else IntakeSource.batch
                ),
                uploaded_by_id=user_id,
            )
            db.add(document)
            db.flush()
            occurrence = IntakeItem(
                operation_id=operation.id,
                item_index=0,
                document_id=document.id,
                filename=filename,
                source=IntakeSource.kad if index == 0 else IntakeSource.batch,
                received_by_id=user_id,
            )
            db.add(occurrence)
            db.flush()
            task = Task(
                document_id=document.id,
                scenario="person_card",
                profile="default",
                status=(TaskStatus.new if index == count - 1 else
                        (TaskStatus.failed if index % 2 else TaskStatus.needs_review)),
                recognized={},
                values={},
                intake_operation_id=operation.id,
                intake_item_id=occurrence.id,
            )
            db.add(task)
            db.flush()
            operations.append((operation.id, task.id))
        db.commit()
    return operations


def simulate_lost_ocr_delivery(task_id: int, operation_id: int) -> None:
    """Enqueue a synthetic OCR job, then remove only that job as a Redis-loss drill."""
    verify_disposable_redis(settings.redis_url)
    with SessionLocal() as db:
        intent_id = outbox.create_intake_intent(
            db, task_id=task_id, operation_id=operation_id, job_kind="ocr"
        )
        db.commit()
    with SessionLocal() as db:
        outbox.dispatch_required(db, intent_id)

    matching = [
        job for job in Job.fetch_many(ocr_queue.get_job_ids(), connection=ocr_queue.connection)
        if job is not None
        and job.func_name == "app.workers.jobs.recognize"
        and job.args == (task_id,)
    ]
    if len(matching) != 1:
        raise AssertionError("Expected exactly one synthetic OCR delivery before Redis-loss drill")
    matching[0].delete(remove_from_queue=True)
    with SessionLocal() as db:
        if db.get(Task, task_id).status != TaskStatus.new:
            raise AssertionError("Redis-loss drill changed the accepted task state")
        if db.get(JobOutbox, intent_id) is not None:
            raise AssertionError("Redis-loss drill unexpectedly retained the confirmed delivery intent")


def seed_browser_membership_history(package_id: int, task_id: int, user_id: int) -> None:
    with SessionLocal() as db:
        expected_event_id = 0
        for _ in range(26):
            case_packages.set_task_membership(
                db,
                package_id=package_id,
                task_id=task_id,
                actor_id=user_id,
                included=True,
                expected_event_id=expected_event_id,
            )
            expected_event_id = db.scalar(
                select(func.max(CasePackageTaskEvent.id)).where(
                    CasePackageTaskEvent.case_package_id == package_id,
                    CasePackageTaskEvent.task_id == task_id,
                )
            ) or 0
            case_packages.set_task_membership(
                db,
                package_id=package_id,
                task_id=task_id,
                actor_id=user_id,
                included=False,
                expected_event_id=expected_event_id,
            )
            expected_event_id = db.scalar(
                select(func.max(CasePackageTaskEvent.id)).where(
                    CasePackageTaskEvent.case_package_id == package_id,
                    CasePackageTaskEvent.task_id == task_id,
                )
            ) or 0
        db.commit()


def create_package_through_ui(page, title: str, case_number: str) -> int:
    page.goto(page.url.split("/case-packages")[0] + "/case-packages", wait_until="networkidle")
    page.get_by_role("link", name="Создать папку").click()
    page.wait_for_url("**/case-packages/new")
    page.locator('input[name="title"]').fill(title)
    page.locator('input[name="case_number"]').fill(case_number)
    page.get_by_role("button", name="Создать папку").click()
    page.wait_for_url("**/case-packages/*?created=1")
    return int(urlsplit(page.url).path.rsplit("/", 1)[1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()

    assert_disposable_postgres()
    if (
        os.environ.get("SRO_CASE_PACKAGE_TEST_DATABASE") != "1"
        or os.environ.get("APP_DEBUG") != "1"
    ):
        raise SystemExit("Refusing browser check outside the marked case-package test database")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    cookie, user_id = make_signed_cookie()
    observer_cookie, _observer_id = make_signed_cookie(
        login=f"synthetic-intake-history-observer-{uuid.uuid4().hex}",
        full_name="Synthetic Intake History Observer",
    )
    task_id, next_review_task_id, review_operation_id = seed_browser_inputs(user_id)
    retry_parent_id, retry_failure_id, retry_filename = seed_failed_ocr_retry_parent(user_id)
    # Fifty upload/batch operations plus one KAD-only occurrence and the
    # original upload exercise both history pages and source filtering.
    recovery_operations = seed_history_operations(user_id, count=51)
    latest_recovery_operation_id, latest_recovery_task_id = recovery_operations[-1]
    simulate_lost_ocr_delivery(latest_recovery_task_id, latest_recovery_operation_id)
    with SessionLocal() as db:
        recovered_operation_id = db.get(Task, task_id).intake_operation_id
    process = start_app(base, args.log)
    evidence_paths: list[Path] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            context.route("**/*", allow_loopback)
            context.add_cookies([{"name": "session", "value": cookie, "url": base}])
            page = context.new_page()
            page.goto(f"{base}/tasks/{retry_parent_id}", wait_until="networkidle")
            if retry_filename not in page.locator("body").inner_text():
                raise AssertionError("Retry UI did not identify the exact original intake filename")
            retry_button = page.get_by_role(
                "button", name="Повторить локальный OCR с исходного документа"
            )
            if retry_button.count() != 1:
                raise AssertionError("Confirmed OCR failure did not expose one explicit retry action")
            retry_button.click()
            page.wait_for_url("**/tasks/*?ocr_retry=queued", timeout=30000)
            retry_child_id = int(urlsplit(page.url).path.rsplit("/", 1)[1])
            if retry_child_id == retry_parent_id:
                raise AssertionError("OCR retry reused the failed parent task")
            child_form = page.locator("#review-form")
            if child_form.count() != 1:
                raise AssertionError("OCR retry child did not open as an independent review task")

            repeated = page.request.post(
                f"{base}/tasks/{retry_parent_id}/retry-ocr",
                form={"failure_history_id": str(retry_failure_id)},
                headers={"Origin": base, "Referer": page.url},
                max_redirects=0,
            )
            repeated_location = repeated.headers.get("location", "")
            if repeated.status != 303 or f"/tasks/{retry_child_id}?" not in repeated_location:
                raise AssertionError(
                    "Retry replay did not return the same durable child task "
                    f"(status={repeated.status}, location={repeated_location!r})"
                )
            with SessionLocal() as db:
                retry_run = db.scalar(
                    select(OcrRetryRun).where(OcrRetryRun.parent_task_id == retry_parent_id)
                )
                parent_after_request = db.get(Task, retry_parent_id)
                retry_child = db.get(Task, retry_child_id)
                retry_child_events = int(db.scalar(
                    select(func.count()).select_from(Event).where(
                        Event.task_id == retry_child_id,
                        Event.kind == "created",
                    )
                ) or 0)
                retry_child_outbox = int(db.scalar(
                    select(func.count()).select_from(JobOutbox).where(
                        JobOutbox.task_id == retry_child_id,
                        JobOutbox.job_kind == "ocr",
                    )
                ) or 0)
            if (retry_run is None or retry_run.child_task_id != retry_child_id
                    or parent_after_request.status != TaskStatus.failed
                    or retry_child.status != TaskStatus.new
                    or retry_child.intake_item_id is not None
                    or retry_child.intake_operation_id is not None
                    or retry_child.recognized or retry_child.values
                    or retry_child.approved_by_id is not None
                    or retry_child_events != 1 or retry_child_outbox != 0):
                raise AssertionError("Retry child or parent changed outside the immutable lineage contract")
            queued_retry_jobs = [
                job for job in Job.fetch_many(ocr_queue.get_job_ids(), connection=ocr_queue.connection)
                if job is not None
                and job.func_name == "app.workers.jobs.recognize"
                and job.args == (retry_child_id,)
            ]
            if len(queued_retry_jobs) != 1:
                raise AssertionError("Retry did not produce exactly one confirmed OCR delivery")
            worker = Worker(
                [ocr_queue],
                name="synthetic-ocr-retry-browser-worker",
                connection=ocr_queue.connection,
            )
            worker.work(burst=True, with_scheduler=False, logging_level="WARNING")
            with SessionLocal() as db:
                completed_retry_child = db.get(Task, retry_child_id)
                parent_after_ocr = db.get(Task, retry_parent_id)
                retry_history = list(db.scalars(
                    select(TaskStatusHistory)
                    .where(TaskStatusHistory.task_id == retry_parent_id)
                    .order_by(TaskStatusHistory.id)
                ))
                completed_events = list(db.scalars(
                    select(Event.kind).where(Event.task_id == retry_child_id)
                ))
            if (completed_retry_child.status != TaskStatus.needs_review
                    or parent_after_ocr.status != TaskStatus.failed
                    or len(retry_history) != 2
                    or "ocr_claim_completed" not in completed_events):
                raise AssertionError("Fresh OCR worker did not successfully parse the original source")
            page.goto(f"{base}/tasks/{retry_child_id}", wait_until="networkidle")
            if page.locator("#review-form").count() != 1:
                raise AssertionError("Successful OCR retry did not open the normal employee review form")

            page.goto(base + "/case-packages", wait_until="networkidle")
            page.get_by_role("link", name="Создать папку").click()
            page.wait_for_url("**/case-packages/new")
            page.locator('input[name="title"]').fill("Синтетическая папка Chromium")
            page.locator('input[name="case_number"]').fill("А00-SYNTHETIC/2026")
            page.get_by_role("button", name="Создать папку").click()
            page.wait_for_url("**/case-packages/*?created=1")
            package_id = int(urlsplit(page.url).path.rsplit("/", 1)[1])
            seed_browser_membership_history(package_id, task_id, user_id)
            page.goto(base + "/case-packages", wait_until="networkidle")
            page.locator('input[name="q"]').fill("А00-SYNTHETIC/2026")
            page.get_by_role("button", name="Найти").click()
            page.wait_for_load_state("networkidle")
            package_link = page.get_by_role("link", name="Синтетическая папка Chromium")
            if package_link.count() != 1:
                raise AssertionError("Case package search did not find the title/number match")
            package_link.click()
            page.wait_for_url(f"**/case-packages/{package_id}")

            with SessionLocal() as db:
                task = db.get(Task, task_id)
                query = task.case_number
                source_filename = task.intake_item.filename
            page.locator('input[name="q"]').fill(query)
            page.get_by_role("button", name="Найти").click()
            page.wait_for_load_state("networkidle")
            candidate_section = page.locator('section[aria-labelledby="add-package-document-heading"]')
            row = candidate_section.locator("tbody tr").filter(has_text=source_filename)
            if row.count() != 1:
                raise AssertionError("Case-package search did not show the synthetic source occurrence")
            stale_add_url = row.locator("form").get_attribute("action")
            if not stale_add_url or source_filename not in row.inner_text():
                raise AssertionError("Candidate row omitted its occurrence filename or add action")
            row.get_by_role("button", name="Добавить").click()
            page.wait_for_load_state("networkidle")
            members_section = page.locator('section[aria-labelledby="package-documents-heading"]')
            if source_filename not in members_section.inner_text():
                raise AssertionError("Explicitly added source did not appear in the package")

            member = members_section.locator("tbody tr").filter(has_text=source_filename)
            member.locator("button", has_text="Убрать").click()
            page.wait_for_load_state("networkidle")
            history_section = page.locator('section[aria-labelledby="package-history-heading"]')
            history_text = history_section.inner_text()
            if "Удалён" not in history_text or source_filename not in history_text:
                raise AssertionError("Package membership history omitted its removal or source filename")
            older_history = page.get_by_role("navigation", name="История состава папки").get_by_role(
                "link", name="Следующие события →"
            )
            if older_history.count() != 1:
                raise AssertionError("More than 50 membership events did not expose history pagination")
            older_history.click()
            page.wait_for_url("**history_page=2")
            if history_section.locator("tbody tr").count() < 1:
                raise AssertionError("The older membership-history page was empty")
            stale_response = page.request.post(
                urljoin(base + "/", stale_add_url),
                headers={"Origin": base, "Referer": page.url},
                max_redirects=0,
            )
            if stale_response.status != 303:
                raise AssertionError("A stale add replay was not redirected as a rejected transition")
            location = stale_response.headers.get("location", "")
            if "Состав папки изменился" not in unquote(location):
                raise AssertionError("Stale replay did not explain that the package had changed")
            with SessionLocal() as db:
                if case_packages.current_membership_events(db, package_id):
                    raise AssertionError("Stale replay restored a removed package member")

            # Restore the explicit membership, then add the same task to a
            # second folder. A third folder deliberately keeps the same case
            # number without an explicit membership event.
            page.goto(base + f"/case-packages/{package_id}", wait_until="networkidle")
            with SessionLocal() as db:
                task = db.get(Task, task_id)
                query = task.case_number
            page.locator('input[name="q"]').fill(query)
            page.get_by_role("button", name="Найти").click()
            page.wait_for_load_state("networkidle")
            candidate_section = page.locator('section[aria-labelledby="add-package-document-heading"]')
            row = candidate_section.locator("tbody tr").filter(has_text=source_filename)
            row.get_by_role("button", name="Добавить").click()
            page.wait_for_load_state("networkidle")
            with SessionLocal() as db:
                if package_id not in {
                    package.id for package in case_packages.active_case_packages_for_task(db, task_id)
                }:
                    raise AssertionError("Re-adding the first package did not persist active membership")
            second_package_id = create_package_through_ui(
                page, "Вторая синтетическая папка Chromium", query
            )
            page.locator('input[name="q"]').fill(query)
            page.get_by_role("button", name="Найти").click()
            page.wait_for_load_state("networkidle")
            second_candidate_section = page.locator('section[aria-labelledby="add-package-document-heading"]')
            second_candidate_row = second_candidate_section.locator("tbody tr").filter(has_text=source_filename)
            second_candidate_row.get_by_role("button", name="Добавить").click()
            page.wait_for_load_state("networkidle")
            with SessionLocal() as db:
                active_ids = {
                    package.id for package in case_packages.active_case_packages_for_task(db, task_id)
                }
                if {package_id, second_package_id} - active_ids:
                    raise AssertionError(
                        "Adding the second package did not persist both active memberships: "
                        f"observed {sorted(active_ids)!r}"
                    )
            unlinked_package_id = create_package_through_ui(
                page, "Совпадающий номер без связи Chromium", query
            )

            assignment_id, manual_assignment_id, later_task_id = seed_assignment_evidence(
                user_id, task_id, second_package_id, evidence_paths
            )
            with SessionLocal() as db:
                assignment_before = db.get(AssignmentDraft, assignment_id)
                assignment_version_before = assignment_before.version
                assignment_comments_before = assignment_before.values.get("comments")
                assignment_source_occurrence_filename = db.get(Task, task_id).intake_item.filename
                revision_count_before = int(db.scalar(
                    select(func.count()).select_from(AssignmentDraftRevision)
                ) or 0)
            page.goto(base + f"/assignments/{assignment_id}", wait_until="networkidle")
            evidence_select = page.locator("#assignment-source-select")
            if evidence_select.count() != 1:
                raise AssertionError("Explicitly linked assignment did not show its source evidence selector")
            source_option_text = evidence_select.locator("option").all_text_contents()
            assignment_source_filename = next(text for text in source_option_text if source_filename in text)
            assignment_later_filename = next(text for text in source_option_text if "renamed-later-act-" in text)
            if len(source_option_text) != 2:
                raise AssertionError(f"Expected exactly two distinct acts in active case packages: {source_option_text!r}")
            assignment_form = page.locator("#assignment-edit-form")
            assignment_form.locator('[name="case_number"]').fill("UNSAVED-CASE-CHECK")
            assignment_form.locator('[name="comments"]').fill("Несохранённая сверка сохраняется при переключении")
            evidence_select.select_option(str(later_task_id))
            if page.locator("#assignment-source-current").inner_text() != assignment_later_filename:
                raise AssertionError("Assignment evidence label did not preserve the exact renamed intake filename")
            if assignment_form.locator('[name="case_number"]').input_value() != "UNSAVED-CASE-CHECK":
                raise AssertionError("Switching evidence discarded an unsaved assignment field")
            if "Несохранённая сверка" not in assignment_form.locator('[name="comments"]').input_value():
                raise AssertionError("Switching evidence discarded unsaved assignment comments")
            preview_path = page.locator("#assignment-source-preview").get_attribute("src")
            download_path = page.locator("#assignment-source-download").get_attribute("href")
            if preview_path != f"/tasks/{later_task_id}/document/preview" or download_path != f"/tasks/{later_task_id}/document":
                raise AssertionError("Evidence switching did not select the exact act preview and download")
            preview_response = page.request.get(base + preview_path)
            if preview_response.status != 200 or not preview_response.body().startswith(b"%PDF-"):
                raise AssertionError("Selected synthetic act did not load through the safe preview endpoint")
            with SessionLocal() as db:
                unchanged_assignment = db.get(AssignmentDraft, assignment_id)
                revision_count_after_preview = int(db.scalar(
                    select(func.count()).select_from(AssignmentDraftRevision)
                ) or 0)
                if (unchanged_assignment.version != assignment_version_before
                        or unchanged_assignment.values.get("comments") != assignment_comments_before
                        or revision_count_after_preview != revision_count_before):
                    raise AssertionError("Viewing or switching acts mutated the saved assignment or its history")
                expected_event_id = db.scalar(
                    select(func.max(CasePackageTaskEvent.id)).where(
                        CasePackageTaskEvent.case_package_id == second_package_id,
                        CasePackageTaskEvent.task_id == later_task_id,
                    )
                ) or 0
                case_packages.set_task_membership(
                    db, package_id=second_package_id, task_id=later_task_id,
                    actor_id=user_id, included=False, expected_event_id=expected_event_id,
                )
                db.commit()
            page.goto(base + f"/assignments/{assignment_id}", wait_until="networkidle")
            refreshed_option_text = page.locator("#assignment-source-select option").all_text_contents()
            if len(refreshed_option_text) != 1 or any("renamed-later-act-" in text for text in refreshed_option_text):
                raise AssertionError("Removed case-package membership remained in assignment evidence after refresh")
            if refreshed_option_text != [assignment_source_filename]:
                raise AssertionError("Assignment source act disappeared when another package member was removed")
            page.goto(base + f"/assignments/{manual_assignment_id}", wait_until="networkidle")
            if page.locator("#assignment-source-select").count() != 0:
                raise AssertionError("Manual assignment borrowed evidence from its debtor card's source task")
            if "Документ карточки должника не используется" not in page.locator("body").inner_text():
                raise AssertionError("Manual assignment did not explain that its debtor-card source is not evidence")

            task_url = base + f"/tasks/{task_id}"
            page.goto(base + f"/case-packages/{package_id}", wait_until="networkidle")
            active_members = page.locator('section[aria-labelledby="package-documents-heading"]')
            active_row = active_members.locator("tbody tr").filter(has_text=assignment_source_occurrence_filename)
            if active_row.count() != 1:
                raise AssertionError("First active membership was missing before removal")
            active_row.get_by_role("button", name="Убрать").click()
            page.wait_for_load_state("networkidle")
            with SessionLocal() as db:
                remaining_ids = {
                    package.id for package in case_packages.active_case_packages_for_task(db, task_id)
                }
                if remaining_ids != {second_package_id}:
                    raise AssertionError(
                        "Removing the first package did not leave exactly the second active membership: "
                        f"observed {sorted(remaining_ids)!r}; expected {[second_package_id]!r}"
                    )
            task_response = page.goto(task_url, wait_until="networkidle")
            if task_response is None or task_response.status != 200:
                server_log = args.log.read_text(encoding="utf-8", errors="replace")[-6000:]
                raise AssertionError(
                    "Task review did not load after membership removal: "
                    f"url={page.url!r}, status={task_response.status if task_response else None}; "
                    f"server log tail: {server_log}"
                )
            backlink_section = page.locator('section[aria-labelledby="task-case-packages-heading"]')
            links = backlink_section.locator("a")
            link_targets = links.evaluate_all(
                "anchors => anchors.map(anchor => { const url = new URL(anchor.href); return `${url.origin}${url.pathname}`; })"
            )
            if link_targets != [base + f"/case-packages/{second_package_id}"]:
                raise AssertionError(
                    "Removing one membership did not hide only that task backlink: "
                    f"expected {[base + f'/case-packages/{second_package_id}']!r}, "
                    f"observed {link_targets!r}, url={page.url!r}, "
                    f"section={backlink_section.inner_text()!r}"
                )

            page.goto(base + f"/case-packages/{package_id}", wait_until="networkidle")
            page.locator('input[name="q"]').fill(query)
            page.get_by_role("button", name="Найти").click()
            page.wait_for_load_state("networkidle")
            candidate_section = page.locator('section[aria-labelledby="add-package-document-heading"]')
            row = candidate_section.locator("tbody tr").filter(has_text=assignment_source_occurrence_filename)
            row.get_by_role("button", name="Добавить").click()
            page.wait_for_load_state("networkidle")
            page.goto(task_url, wait_until="networkidle")
            backlink_section = page.locator('section[aria-labelledby="task-case-packages-heading"]')
            links = backlink_section.locator("a")
            link_targets = links.evaluate_all(
                "anchors => anchors.map(anchor => { const url = new URL(anchor.href); return `${url.origin}${url.pathname}`; })"
            )
            expected_targets = {
                base + f"/case-packages/{package_id}",
                base + f"/case-packages/{second_package_id}",
            }
            if len(link_targets) != 2 or set(link_targets) != expected_targets:
                raise AssertionError("Task review did not show exactly its two active explicit package memberships")
            if f"/case-packages/{unlinked_package_id}" in link_targets:
                raise AssertionError("Case-number coincidence created a task-to-package link")

            observer_context = browser.new_context()
            observer_context.route("**/*", allow_loopback)
            observer_context.add_cookies([{"name": "session", "value": observer_cookie, "url": base}])
            observer_page = observer_context.new_page()
            observer_page.goto(task_url, wait_until="networkidle")
            observer_section = observer_page.locator('section[aria-labelledby="task-case-packages-heading"]')
            observer_targets = observer_section.locator("a").evaluate_all(
                "anchors => anchors.map(anchor => { const url = new URL(anchor.href); return `${url.origin}${url.pathname}`; })"
            )
            if len(observer_targets) != 2 or set(observer_targets) != expected_targets:
                raise AssertionError("A second employee did not see the same active package links")
            observer_context.close()

            # Keep the exact upload scope as staff move from the worklist to
            # review and continue after an explicit save/approval.
            page.goto(f"{base}/debtors/intake?intake={review_operation_id}", wait_until="networkidle")
            scoped_task_link = page.locator(
                f'a[href$="/tasks/{task_id}?intake={review_operation_id}"]'
            )
            if scoped_task_link.count() != 1:
                raise AssertionError("The selected upload scope was not retained on the review link")
            scoped_task_link.click()
            page.wait_for_url(f"**/tasks/{task_id}?intake={review_operation_id}")
            if page.get_by_role("link", name="К проверке выбранной загрузки").count() != 1:
                raise AssertionError("Task review omitted its selected-upload return link")
            page.locator("#case_number").fill("А00-12345/2026")
            page.locator("#doc_date").fill("21.10.2026")
            page.locator("#last_name").fill("Макетова")
            page.locator("#first_name").fill("Анна")
            page.get_by_role("button", name="Сохранить и открыть следующее задание на проверке").click()
            page.wait_for_url(f"**/tasks/{next_review_task_id}?intake={review_operation_id}")
            if page.locator('#review-form input[name="intake"]').input_value() != str(review_operation_id):
                raise AssertionError("Review continuation lost the selected operation ID")
            conflict_context = browser.new_context()
            conflict_context.route("**/*", allow_loopback)
            conflict_context.add_cookies([{"name": "session", "value": observer_cookie, "url": base}])
            conflict_page = conflict_context.new_page()
            conflict_page.goto(
                f"{base}/tasks/{next_review_task_id}?intake={review_operation_id}",
                wait_until="networkidle",
            )
            page.get_by_role("button", name="Подтвердить и открыть следующее задание на проверке").click()
            page.wait_for_url(f"**/tasks/{task_id}?intake={review_operation_id}")
            conflict_page.locator("#case_number").fill("А00-STALE/2026")
            conflict_page.get_by_role("button", name="Сохранить", exact=True).click()
            current_version_link = conflict_page.locator(
                'div.panel.field-warning[role="alert"] a',
            ).filter(has_text="Открыть актуальную версию")
            current_version_link.wait_for(state="visible")
            if current_version_link.count() != 1:
                raise AssertionError("Stale review did not show one current-version recovery link")
            conflict_page.on("dialog", lambda dialog: dialog.accept())
            current_version_link.click()
            conflict_page.wait_for_url(
                f"**/tasks/{next_review_task_id}?intake={review_operation_id}"
            )
            if conflict_page.locator('#review-form input[name="intake"]').input_value() != str(review_operation_id):
                raise AssertionError("Stale-conflict recovery dropped the selected upload scope")
            conflict_context.close()
            with SessionLocal() as db:
                approved_next = db.get(Task, next_review_task_id)
                if approved_next.status != TaskStatus.approved:
                    raise AssertionError("The explicit confirm-and-continue action did not save approval")
                if approved_next.intake_operation_id != review_operation_id:
                    raise AssertionError("Review continuation crossed the accepted upload boundary")

            page.goto(base + "/case-packages", wait_until="networkidle")
            older = page.get_by_role("link", name="Старее →")
            if older.count() != 1:
                raise AssertionError("More than 50 packages did not expose the next list page")
            older.click()
            page.wait_for_url("**/case-packages?page=2")
            if page.locator("tbody tr").count() < 1:
                raise AssertionError("The second case-package list page was empty")
            page.goto(base + f"/case-packages/{package_id}", wait_until="networkidle")
            search = page.locator('input[name="q"]')
            search.fill("А77-1/2026")
            page.get_by_role("button", name="Найти").click()
            page.wait_for_load_state("networkidle")
            task_pages = page.get_by_role("navigation", name="Страницы судебных заданий")
            next_tasks = task_pages.get_by_role("link", name="Следующие задания →")
            if next_tasks.count() != 1:
                raise AssertionError("More than 25 same-case tasks did not expose candidate pagination")
            next_tasks.click()
            page.wait_for_url("**tasks_page=2")
            older_act = (
                page.locator('section[aria-labelledby="add-package-document-heading"]')
                .locator("tbody tr")
                .filter(has_text="court-act-00-")
            )
            if older_act.count() != 1:
                raise AssertionError("The older same-case source act was inaccessible on candidate page 2")
            browser.close()

            # Restart the owned app and recover an exact accepted upload as
            # another employee in a fresh browser context with no local state.
            with SessionLocal() as db:
                counts_before_recovery = (
                    db.scalar(select(func.count()).select_from(Task)),
                    db.scalar(select(func.count()).select_from(IntakeOperation)),
                )
            stop_app(process)
            process = start_app(base, args.log)
            recovery_browser = playwright.chromium.launch(headless=True)
            recovery_context = recovery_browser.new_context()
            recovery_context.route("**/*", allow_loopback)
            recovery_context.add_cookies([{"name": "session", "value": observer_cookie, "url": base}])
            recovery_page = recovery_context.new_page()
            recovery_page.goto(base + "/intake-history", wait_until="networkidle")
            if recovery_page.evaluate("window.localStorage.length") != 0:
                raise AssertionError("Recovery browser unexpectedly retained local storage")
            if recovery_page.locator("tbody tr").count() != 50:
                raise AssertionError("Intake history did not render the first 50-operation page")
            latest_operation_id, latest_task_id = recovery_operations[-1]
            latest_filename = f"synthetic-recovery-{len(recovery_operations) - 1:02d}.pdf"
            latest_row = recovery_page.locator("tbody tr").filter(has_text=latest_filename)
            latest_link = latest_row.get_by_role("link", name="Открыть задания")
            if latest_link.count() != 1:
                raise AssertionError("Intake history omitted the latest synthetic accepted operation")
            if latest_row.get_by_role(
                "button", name="Восстановить доставку OCR (1)"
            ).count() != 1:
                raise AssertionError(
                    "An exact batch receipt could not recover its shared KAD-origin document"
                )
            if recovery_page.locator(
                "tbody tr", has_text="synthetic-recovery-00.pdf"
            ).count() != 0:
                raise AssertionError("A KAD-only occurrence appeared in upload history")
            latest_link.click()
            recovery_page.wait_for_url("**/debtors/intake?intake=*")
            if parse_qs(urlsplit(recovery_page.url).query).get("intake") != [str(latest_operation_id)]:
                raise AssertionError("Intake history opened a different operation than the selected row")
            if recovery_page.locator(
                f'a[href$="/tasks/{latest_task_id}?intake={latest_operation_id}"]'
            ).count() != 1:
                raise AssertionError("Recovered operation dashboard did not show its original task")

            # Recover the selected queued task after the Redis job was removed;
            # this must reuse the existing task and must not repeat the upload.
            recovery_page.goto(base + "/intake-history", wait_until="networkidle")
            recovery_row = recovery_page.locator("tbody tr").filter(has_text=latest_filename)
            recovery_row.get_by_role("button", name="Восстановить доставку OCR (1)").click()
            recovery_page.wait_for_url("**recovery=queued&queued=1&pending=0&started=0")
            if "подтверждена для 1 заданий" not in recovery_page.locator('[role="status"]').inner_text():
                raise AssertionError("Recovery UI did not distinguish a confirmed queue delivery")
            with SessionLocal() as db:
                self_recovered_task = db.get(Task, latest_recovery_task_id)
                if self_recovered_task.status != TaskStatus.new:
                    raise AssertionError("Queue recovery changed task status before the OCR worker claimed it")
                recovery_events = db.query(Event).filter(
                    Event.task_id == latest_recovery_task_id,
                    Event.kind == "intake_recovery_requested",
                ).count()
                if recovery_events != 1:
                    raise AssertionError("Queue recovery did not record one actor-attributed request")
            active_recovery_jobs = [
                job for job in Job.fetch_many(ocr_queue.get_job_ids(), connection=ocr_queue.connection)
                if job is not None
                and job.func_name == "app.workers.jobs.recognize"
                and job.args == (latest_recovery_task_id,)
            ]
            if len(active_recovery_jobs) != 1:
                raise AssertionError("Lost OCR delivery was not restored exactly once")

            # A second employee's near-immediate retry sees the active Redis
            # job and leaves one delayed outbox intent instead of enqueuing a
            # duplicate delivery or claiming success twice.
            owner_context = recovery_browser.new_context()
            owner_context.route("**/*", allow_loopback)
            owner_context.add_cookies([{"name": "session", "value": cookie, "url": base}])
            owner_page = owner_context.new_page()
            owner_page.goto(base + "/intake-history", wait_until="networkidle")
            owner_row = owner_page.locator("tbody tr").filter(has_text=latest_filename)
            owner_row.get_by_role("button", name="Восстановить доставку OCR (1)").click()
            owner_page.wait_for_url("**recovery=pending&queued=0&pending=1&started=0")
            with SessionLocal() as db:
                queued_recovery_intents = db.query(JobOutbox).filter(
                    JobOutbox.task_id == latest_recovery_task_id,
                    JobOutbox.job_kind == "ocr",
                ).count()
                recovery_actor_ids = [user_id for (user_id,) in db.query(Event.user_id).filter(
                    Event.task_id == latest_recovery_task_id,
                    Event.kind == "intake_recovery_requested",
                ).order_by(Event.id).all()]
            if queued_recovery_intents != 1 or recovery_actor_ids[-2:] != [_observer_id, user_id]:
                raise AssertionError("Concurrent employee recovery was not deduplicated and attributed")
            active_recovery_jobs = [
                job for job in Job.fetch_many(ocr_queue.get_job_ids(), connection=ocr_queue.connection)
                if job is not None
                and job.func_name == "app.workers.jobs.recognize"
                and job.args == (latest_recovery_task_id,)
            ]
            if len(active_recovery_jobs) != 1:
                raise AssertionError("Second employee recovery created a duplicate active OCR delivery")
            owner_context.close()

            recovery_page.goto(base + "/intake-history", wait_until="networkidle")
            older_operations = recovery_page.get_by_role("navigation", name="Страницы истории загрузок").get_by_role(
                "link", name="Старее →"
            )
            if older_operations.count() != 1:
                raise AssertionError("More than 50 accepted operations did not expose history pagination")
            older_operations.click()
            recovery_page.wait_for_url("**/intake-history?page=2")
            original_row = recovery_page.locator("tbody tr").filter(has_text=source_filename)
            original_link = original_row.get_by_role("link", name="Открыть задания")
            if original_link.count() != 1:
                raise AssertionError("Second history page omitted the original accepted upload occurrence")
            original_link.click()
            recovery_page.wait_for_url("**/debtors/intake?intake=*")
            if parse_qs(urlsplit(recovery_page.url).query).get("intake") != [str(recovered_operation_id)]:
                raise AssertionError("Second-page recovery did not open the original exact upload operation")
            if recovery_page.locator(
                f'a[href$="/tasks/{task_id}?intake={recovered_operation_id}"]'
            ).count() != 1:
                raise AssertionError("Recovered original operation dashboard omitted its original task")
            if recovery_page.locator(
                f'a[href$="/tasks/{latest_task_id}?intake={recovered_operation_id}"]'
            ).count() != 0:
                raise AssertionError("Opening the original upload accidentally merged another operation")
            recovery_context.close()
            recovery_browser.close()
            with SessionLocal() as db:
                counts_after_recovery = (
                    db.scalar(select(func.count()).select_from(Task)),
                    db.scalar(select(func.count()).select_from(IntakeOperation)),
                )
            if counts_after_recovery != counts_before_recovery:
                raise AssertionError("Opening intake history created new tasks or upload operations")

        result = {
            "synthetic_only": True,
            "chromium": "pass",
            "package_created_through_ui": True,
            "package_search": True,
            "explicit_task_add_remove": True,
            "stale_replay_rejected": True,
            "membership_history_visible": True,
            "membership_history_pagination": True,
            "source_occurrence_filename_visible": True,
            "package_list_pagination": True,
            "candidate_pagination": True,
            "task_review_active_package_links": True,
            "task_review_links_follow_remove_readd": True,
            "task_review_case_number_does_not_auto_link": True,
            "task_review_links_visible_to_second_employee": True,
            "upload_scoped_review_return_and_continue": True,
            "review_continuation_keeps_exact_operation_and_confirms_explicitly": True,
            "stale_review_conflict_recovery_keeps_exact_operation": True,
            "assignment_source_acts_switch_without_losing_unsaved_values": True,
            "assignment_source_occurrence_names_are_exact": True,
            "removed_assignment_evidence_membership_hides_after_refresh": True,
            "manual_assignment_does_not_borrow_debtor_card_source": True,
            "assignment_evidence_view_is_read_only": True,
            "confirmed_ocr_retry_creates_one_clean_linked_child": True,
            "ocr_retry_replay_returns_same_child": True,
            "retried_original_pdf_reaches_review_after_local_ocr": True,
            "intake_history_visible_after_app_restart": True,
            "intake_history_visible_to_second_employee_without_local_storage": True,
            "intake_history_operation_identity_and_pagination": True,
            "intake_history_navigation_created_no_tasks": True,
            "lost_redis_ocr_delivery_recovered_in_chromium": True,
            "recovery_preserved_task_identity_and_audit": True,
            "second_employee_recovery_deduplicated_active_delivery": True,
            "external_sro_accessed": False,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        stop_app(process)
        for evidence_path in evidence_paths:
            evidence_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
