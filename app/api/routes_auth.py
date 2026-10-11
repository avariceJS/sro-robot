"""Вход и выход сотрудника."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db.base import get_session
from ..observability import request_id_from_scope
from ..services import auth, tasks
from ..web.templates import templates
from .deps import SESSION_TOKEN_KEY, SESSION_USER_KEY

router = APIRouter(tags=["auth"])


@router.get("/login", name="login_page")
def login_page(request: Request):
    """Страница входа."""
    return templates.TemplateResponse(request, "login.html")


@router.post("/login", name="login_submit")
def login_submit(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_session),
):
    user = auth.authenticate(db, login, password)
    if user is None:
        request_id = request_id_from_scope(request.scope)
        tasks.log(
            db,
            None,
            "session_login_failed",
            "Вход отклонён: учётные данные не подтверждены",
            payload={"request_id": request_id} if request_id else None,
        )
        db.commit()
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Неверный логин или пароль", "login": login},
            status_code=401,
        )
    previous_user_id = request.session.get(SESSION_USER_KEY)
    previous_token = request.session.get(SESSION_TOKEN_KEY)
    if type(previous_user_id) is int and isinstance(previous_token, str):
        if auth.revoke_login_session(db, previous_user_id, previous_token):
            previous_user = auth.get_user(db, previous_user_id)
            if previous_user is not None and previous_user.is_active:
                request_id = request_id_from_scope(request.scope)
                tasks.log(
                    db,
                    None,
                    "session_ended",
                    "Сотрудник заменил браузерную сессию при повторном входе",
                    previous_user.id,
                    payload={"request_id": request_id} if request_id else None,
                )
    session_token = secrets.token_urlsafe(32)
    auth.new_login_session(db, user.id, session_token)
    request_id = request_id_from_scope(request.scope)
    tasks.log(
        db,
        None,
        "session_started",
        "Сотрудник вошёл в приложение",
        user.id,
        payload={"request_id": request_id} if request_id else None,
    )
    db.commit()
    request.session.clear()
    request.session[SESSION_USER_KEY] = user.id
    request.session[SESSION_TOKEN_KEY] = session_token
    return RedirectResponse("/", status_code=303)


@router.post("/logout", name="logout")
def logout(
    request: Request,
    db: Session = Depends(get_session),
) -> RedirectResponse:
    user_id = request.session.get(SESSION_USER_KEY)
    session_token = request.session.get(SESSION_TOKEN_KEY)
    try:
        revoked = (
            auth.revoke_login_session(db, user_id, session_token)
            if type(user_id) is int and isinstance(session_token, str)
            else False
        )
        user = auth.get_user(db, user_id) if revoked else None
        if revoked and user is not None and user.is_active:
            request_id = request_id_from_scope(request.scope)
            tasks.log(
                db,
                None,
                "session_ended",
                "Сотрудник вышел из приложения",
                user.id,
                payload={"request_id": request_id} if request_id else None,
            )
        if revoked:
            db.commit()
    finally:
        request.session.clear()
    return RedirectResponse(request.url_for("login_page"), status_code=303)
