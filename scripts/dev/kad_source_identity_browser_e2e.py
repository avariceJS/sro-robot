#!/usr/bin/env python3
"""Verify KAD source identity through Chromium and a disposable local app.

The app must be serving a fresh loopback benchmark PostgreSQL database. Only
synthetic users, KAD URLs, and PDF bytes are created; no external KAD or SRO
site is contacted.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import sys
import uuid
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from itsdangerous import TimestampSigner

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.db.base import SessionLocal
from app.db.models import (
    AssignmentDraft,
    DebtorCardDraft,
    Document,
    Event,
    IntakeItem,
    IntakeOperation,
    JobOutbox,
    KadImportRequestReceipt,
    KadSourceRun,
    LoginSession,
    Task,
    User,
)
from app.services import auth
from app.settings import settings


PDF = b"%PDF-1.4\nsynthetic KAD source identity browser fixture\n%%EOF\n"


def require_safe_runtime(base_url: str) -> None:
    env_path = Path(os.environ.get("SRO_ROBOT_ENV_FILE", ""))
    if (
        not env_path.is_file() or env_path.is_symlink() or env_path.stat().st_size
        or env_path.stat().st_mode & 0o077
    ):
        raise SystemExit("SRO_ROBOT_ENV_FILE must point to an empty private regular file")
    database = make_url(settings.database_url)
    if (
        not settings.debug
        or os.environ.get("APP_DEBUG") != "1"
        or database.get_backend_name() != "postgresql"
        or database.host not in {"127.0.0.1", "localhost", "::1"}
        or "benchmark" not in (database.database or "").casefold()
    ):
        raise SystemExit("Refusing browser check outside APP_DEBUG loopback benchmark PostgreSQL")
    target = urlsplit(base_url)
    if (target.scheme, target.hostname) != ("http", "127.0.0.1") or target.port is None:
        raise SystemExit("Refusing non-loopback or non-HTTP app URL")


def require_empty_database() -> None:
    with SessionLocal() as db:
        counts = {
            model.__name__: int(db.scalar(select(func.count()).select_from(model)) or 0)
            for model in (
                User,
                LoginSession,
                Document,
                Task,
                Event,
                IntakeOperation,
                IntakeItem,
                JobOutbox,
                KadSourceRun,
                KadImportRequestReceipt,
                DebtorCardDraft,
                AssignmentDraft,
            )
        }
    if any(counts.values()):
        raise SystemExit(f"Browser check requires an empty benchmark database: {counts}")


def create_staff_cookie(index: int) -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    with SessionLocal() as db:
        user = User(
            login=f"synthetic-kad-browser-{secrets.token_hex(8)}-{index}",
            full_name=f"Synthetic Browser Staff {index}",
            password_hash="synthetic-only",
            sro_login="synthetic-only",
            is_active=True,
        )
        db.add(user)
        db.flush()
        session = LoginSession(
            user_id=user.id,
            token_sha256=auth._session_token_sha256(token),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            external_state_ciphertext=b"synthetic-browser-state",
            external_state_expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        db.add(session)
        db.commit()
        user_id = user.id
    payload = base64.b64encode(json.dumps(
        {"user_id": user_id, "session_token": token}, separators=(",", ":")
    ).encode())
    return TimestampSigner(settings.secret_key).sign(payload).decode("ascii"), user_id


def source_payload(card_id: str, pdf_id: str, run_id: str) -> dict:
    card = f"https://kad.arbitr.ru/Card/{card_id}"
    selected = f"https://kad.arbitr.ru/Kad/PdfDocument/{card_id}/{pdf_id}/act.pdf"
    return {
        "requestId": run_id,
        "caseNumber": "А41-12345/2026",
        "cardUrl": card,
        "candidateInventoryComplete": False,
        "selectedUrl": selected,
        "candidates": [{"url": selected, "label": "Synthetic selected act"}],
        "selectionReason": "employee_selected_visible_document",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18086")
    args = parser.parse_args()
    require_safe_runtime(args.base_url)
    require_empty_database()
    base = args.base_url.rstrip("/")
    staff = [create_staff_cookie(index) for index in range(2)]
    card_id = str(uuid.uuid4())
    act_id = str(uuid.uuid4())
    other_act_id = str(uuid.uuid4())

    async_import = """async ({source, pdf, reprocess}) => {
      const bytes = Uint8Array.from(atob(pdf), c => c.charCodeAt(0));
      const form = new FormData();
      form.append('idempotency_key', crypto.randomUUID().replaceAll('-', '') + '0000000000000000');
      form.append('source', JSON.stringify(source));
      form.append('reprocess', reprocess ? 'true' : 'false');
      form.append('file', new Blob([bytes], {type:'application/pdf'}), 'synthetic.pdf');
      const response = await fetch('/kad-helper/import', {method:'POST', body:form});
      return {status:response.status, body:await response.json()};
    }"""
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for index, (cookie, _user_id) in enumerate(staff):
                context = browser.new_context()
                context.route(
                    "**/*",
                    lambda route: route.continue_()
                    if urlsplit(route.request.url).hostname == "127.0.0.1"
                    else route.abort("blockedbyclient"),
                )
                context.add_cookies([{"name": "session", "value": cookie, "url": base}])
                page = context.new_page()
                response = page.goto(base + "/debtors/intake", wait_until="domcontentloaded")
                if response is None or response.status != 200:
                    raise AssertionError("Synthetic staff dashboard did not load")
                control = page.locator('input[name="kad_reprocess"]')
                control.wait_for(state="visible")
                if control.is_checked() or control.is_disabled():
                    raise AssertionError("Explicit reprocessing control must start unchecked and enabled")
                if index == 0:
                    result = page.evaluate(async_import, {
                        "source": source_payload(card_id, act_id, str(uuid.uuid4())),
                        "pdf": base64.b64encode(PDF).decode(),
                        "reprocess": False,
                    })
                else:
                    result = page.evaluate(async_import, {
                        "source": source_payload(card_id, act_id, str(uuid.uuid4())),
                        "pdf": base64.b64encode(PDF).decode(),
                        "reprocess": False,
                    })
                results.append(result)
                context.close()

            # A different selected act with the same PDF bytes remains separate.
            context = browser.new_context()
            context.route(
                "**/*",
                lambda route: route.continue_()
                if urlsplit(route.request.url).hostname == "127.0.0.1"
                else route.abort("blockedbyclient"),
            )
            context.add_cookies([{"name": "session", "value": staff[1][0], "url": base}])
            page = context.new_page()
            page.goto(base + "/debtors/intake", wait_until="domcontentloaded")
            other = page.evaluate(async_import, {
                "source": source_payload(card_id, other_act_id, str(uuid.uuid4())),
                "pdf": base64.b64encode(PDF).decode(),
                "reprocess": False,
            })
            # Explicit reprocessing creates a child generation for the same source.
            replay = page.evaluate(async_import, {
                "source": source_payload(card_id, act_id, str(uuid.uuid4())),
                "pdf": base64.b64encode(PDF).decode(),
                "reprocess": True,
            })
            context.close()
            browser.close()
        finally:
            browser.close()

    first, cross_employee = results
    if any(item["status"] != 200 for item in [first, cross_employee, other, replay]):
        raise AssertionError(f"Synthetic browser import failed: {[first, cross_employee, other, replay]}")
    first_body, replay_body = first["body"], cross_employee["body"]
    if (
        first_body["task_url"] != replay_body["task_url"]
        or first_body["reused"] is not False
        or replay_body["reused"] is not True
        or other["body"]["task_url"] == first_body["task_url"]
        or replay["body"]["task_url"] == first_body["task_url"]
        or replay["body"]["run_generation"] != 2
    ):
        raise AssertionError("Source identity, distinct act, or explicit reprocessing contract failed")
    with SessionLocal() as db:
        replay_task_id = int(replay["body"]["task_url"].rsplit("/", 1)[1])
        child = db.scalar(select(KadSourceRun).where(KadSourceRun.task_id == replay_task_id))
        parent = db.get(KadSourceRun, child.parent_run_id) if child else None
        if child is None or parent is None or parent.task_id != int(first_body["task_url"].rsplit("/", 1)[1]):
            raise AssertionError("Reprocessing lineage did not persist in PostgreSQL")
    print(json.dumps({
        "chromium": "pass",
        "dashboard_status": 200,
        "reprocess_control": "visible, unchecked, enabled",
        "cross_employee_same_source": "same task reused",
        "different_selected_act_same_bytes": "separate task",
        "explicit_reprocess": "generation 2 with parent lineage",
        "queue_delivery": "not asserted; Redis is not required for identity check",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
