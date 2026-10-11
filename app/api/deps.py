"""Общие зависимости роутов: сессия базы и текущий сотрудник."""

from __future__ import annotations

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db.base import get_session
from ..db.models import User
from ..services import auth

SESSION_USER_KEY = "user_id"
SESSION_TOKEN_KEY = "session_token"


class NotAuthenticated(Exception):
    """Перехватывается в main.py и уводит на страницу входа."""


def current_user(request: Request, db: Session = Depends(get_session)) -> User:
    user_id = request.session.get(SESSION_USER_KEY)
    token = request.session.get(SESSION_TOKEN_KEY)
    if (
        type(user_id) is not int
        or not isinstance(token, str)
        or not auth.login_session_is_active(db, user_id, token)
    ):
        request.session.clear()
        raise NotAuthenticated()
    user = auth.get_user(db, user_id)
    if user is None or not user.is_active:
        request.session.clear()
        raise NotAuthenticated()
    return user


def login_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(request.url_for("login_page"), status_code=303)
