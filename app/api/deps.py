"""Общие зависимости роутов: сессия базы и текущий сотрудник."""

from __future__ import annotations

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db.base import get_session
from ..db.models import User
from ..services import auth

SESSION_USER_KEY = "user_id"


class NotAuthenticated(Exception):
    """Перехватывается в main.py и уводит на страницу входа."""


def current_user(request: Request, db: Session = Depends(get_session)) -> User:
    user_id = request.session.get(SESSION_USER_KEY)
    user = auth.get_user(db, user_id) if user_id else None
    if user is None or not user.is_active:
        raise NotAuthenticated()
    return user


def login_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(request.url_for("login_page"), status_code=303)
