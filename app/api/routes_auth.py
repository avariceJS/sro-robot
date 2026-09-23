"""Вход и выход сотрудника."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db.base import get_session
from ..services import auth
from ..web.templates import templates
from .deps import SESSION_USER_KEY

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
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Неверный логин или пароль", "login": login},
            status_code=401,
        )
    request.session[SESSION_USER_KEY] = user.id
    return RedirectResponse("/", status_code=303)


@router.post("/logout", name="logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(request.url_for("login_page"), status_code=303)
