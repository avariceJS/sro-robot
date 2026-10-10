"""Задания: список, загрузка, проверка полей."""

from __future__ import annotations

import html

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from ..db.base import get_session
from ..db.models import IntakeSource, Task, TaskStatus, User
from ..services import fields, tasks
from ..services.artifacts import read_screenshot
from ..services.fields import scenario_of
from ..web.templates import templates
from ..workers.queues import enqueue_recognize, enqueue_write
from .deps import current_user

router = APIRouter(tags=["tasks"])


def _require_task(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задание не найдено")
    return task


async def _values_from_form(request: Request, task: Task) -> dict[str, str]:
    form = await request.form()
    scenario = scenario_of(task)
    return {spec.attr: str(form.get(spec.attr, "") or "") for spec in scenario.fields}


@router.get("/", name="tasks_page")
def tasks_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    recent_tasks = tasks.recent(db)
    status_labels = tasks.status_labels(db, recent_tasks)
    task_rows = [
        {
            "task": task,
            "summary": fields.summary(task),
            "status_ru": status_labels[task.id],
        }
        for task in recent_tasks
    ]
    return templates.TemplateResponse(
        request,
        "tasks.html",
        {"user": user, "task_rows": task_rows},
    )


@router.post("/upload", name="upload_documents")
async def upload_documents(
    request: Request,
    files: list[UploadFile] = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    source = IntakeSource.upload if len(files) == 1 else IntakeSource.batch
    created_ids: list[int] = []
    for upload in files:
        content = await upload.read()
        filename = upload.filename or "document"
        document = tasks.store_document(db, filename, content, source, user.id)
        task = tasks.create_task(db, document, user_id=user.id)
        created_ids.append(task.id)
    db.commit()
    for task_id in created_ids:
        enqueue_recognize(task_id)
    return RedirectResponse("/", status_code=303)


@router.get("/tasks/{task_id}", name="task_page")
def task_page(
    request: Request,
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    task = _require_task(db, task_id)
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "user": user,
            "task": task,
            "fields": fields.review_fields(task),
            "TaskStatus": TaskStatus,
            "status_ru": tasks.status_label(db, task),
        },
    )


@router.post("/tasks/{task_id}/save", name="task_save")
async def task_save(
    request: Request,
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    task = _require_task(db, task_id)
    values = await _values_from_form(request, task)
    try:
        tasks.save_values(db, task, values, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/approve", name="task_approve")
async def task_approve(
    request: Request,
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    task = _require_task(db, task_id)
    values = await _values_from_form(request, task)
    try:
        tasks.save_values(db, task, values, user.id)
        tasks.approve(db, task, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/run", name="task_run")
def task_run(
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    task = _require_task(db, task_id)
    # Неактивной кнопки мало: запуск без подтверждения оператором не допускается.
    if task.status == TaskStatus.approved:
        enqueue_write(task.id, live=False, user_id=user.id)
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.get("/tasks/{task_id}/confirm-save", name="task_confirm_save")
def task_confirm_save(
    request: Request,
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    """Отдельный экран подтверждения: следующий шаг пишет в реальный реестр."""
    task = _require_task(db, task_id)
    return templates.TemplateResponse(
        request,
        "confirm_save.html",
        {"user": user, "task": task, "fields": fields.review_fields(task)},
    )


@router.post("/tasks/{task_id}/decision", name="task_decision")
async def task_decision(
    request: Request,
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    """Ответ оператора роботу, который стоит на заполненной форме."""
    task = _require_task(db, task_id)
    form = await request.form()
    decision = str(form.get("decision", "close"))
    if decision not in ("save", "close"):
        decision = "close"
    if task.status == TaskStatus.awaiting_preview:
        if decision == "save":
            tasks.log(
                db,
                task,
                "live_save_blocked",
                "Запись в реальный реестр СРО отключена",
                user.id,
                payload={"saved": False},
            )
            db.commit()
            return RedirectResponse(f"/tasks/{task_id}", status_code=303)
        task.browser_decision = decision
        tasks.log(db, task, f"decision_{decision}", "Решение оператора по превью", user.id)
        db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.get("/tasks/{task_id}/shot/{index}", name="task_shot")
def task_shot(
    task_id: int,
    index: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> Response:
    """Снимок заполненной формы. Отдаются только пути, записанные в задание."""
    task = _require_task(db, task_id)
    shots = task.screenshots or []
    if index < 0 or index >= len(shots):
        raise HTTPException(status_code=404, detail="Снимок не найден")
    try:
        content = read_screenshot(str(shots[index]))
    except (OSError, ValueError, TypeError):
        raise HTTPException(status_code=404, detail="Снимок не найден") from None
    return Response(content, media_type="image/png", headers={"X-Content-Type-Options": "nosniff"})


@router.get("/tasks/{task_id}/status", name="task_status")
def task_status(
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    task = _require_task(db, task_id)
    parts = [f'<span class="status">{html.escape(tasks.status_label(db, task))}</span>']
    if task.error:
        parts.append(f'<div class="msg-error">{html.escape(task.error)}</div>')
    if task.record_url:
        url = html.escape(task.record_url, quote=True)
        parts.append(f'<div><a href="{url}" target="_blank" rel="noopener">Запись в СРО</a></div>')
    return HTMLResponse("".join(parts))
