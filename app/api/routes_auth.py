"""Вход и выход сотрудника."""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sro_robot.platform.authentication import SroLoginError, authenticate_sro_credentials

from ..db.base import get_session
from ..observability import request_id_from_scope
from ..services import auth, tasks
from ..security import (
    LoginAttemptLimiterUnavailable,
    create_login_attempt_limiter,
)
from ..settings import settings
from ..web.templates import templates
from .deps import SESSION_TOKEN_KEY, SESSION_USER_KEY

router = APIRouter(tags=["auth"])
logger = logging.getLogger(__name__)
login_attempt_limiter = create_login_attempt_limiter(
    debug=settings.debug,
    redis_url=settings.redis_url,
    secret_key=settings.secret_key,
)


def _login_limiter_unavailable(request: Request, exc: LoginAttemptLimiterUnavailable):
    cause = exc.__cause__
    logger.warning(
        "Login rate limiter unavailable request_id=%s error_type=%s",
        request_id_from_scope(request.scope) or "unavailable",
        type(cause).__name__ if cause is not None else type(exc).__name__,
    )
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "Вход временно недоступен. Повторите позже"},
        status_code=503,
        headers={"Retry-After": "1"},
    )


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
    peer = request.client.host if request.client is not None else "unknown-peer"
    normalized_login = auth.normalize_login(login)
    try:
        attempt_token, retry_after = login_attempt_limiter.reserve(peer, normalized_login)
    except LoginAttemptLimiterUnavailable as exc:
        return _login_limiter_unavailable(request, exc)
    if retry_after is not None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Слишком много попыток. Повторите вход позже"},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    if attempt_token is None:
        raise RuntimeError("Login attempt limiter admitted without a reservation")
    try:
        try:
            external = authenticate_sro_credentials(login, password)
        finally:
            password = ""
    except SroLoginError as exc:
        logger.warning(
            "SRO login verification failed request_id=%s stage=%s reason=%s",
            request_id_from_scope(request.scope) or "unavailable",
            exc.stage,
            exc.reason,
        )
        try:
            login_attempt_limiter.finish(
                peer,
                normalized_login,
                attempt_token,
                outcome="failed" if exc.reason in {"rejected", "challenge"} else "aborted",
            )
        except LoginAttemptLimiterUnavailable as exc:
            return _login_limiter_unavailable(request, exc)
        response_status = 401 if exc.reason == "rejected" else 503
        response_text = (
            "Проверьте логин и пароль СРОАрбитр"
            if exc.reason == "rejected"
            else "Не удалось безопасно проверить вход в СРОАрбитр. Повторите позже"
        )
        request_id = request_id_from_scope(request.scope)
        tasks.log(
            db,
            None,
            "session_login_failed",
            "Вход через СРОАрбитр не подтверждён",
            payload={"request_id": request_id} if request_id else None,
        )
        db.commit()
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": response_text},
            status_code=response_status,
        )
    except Exception:
        try:
            login_attempt_limiter.finish(
                peer, normalized_login, attempt_token, outcome="aborted"
            )
        except LoginAttemptLimiterUnavailable as exc:
            return _login_limiter_unavailable(request, exc)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Вход временно недоступен. Повторите позже"},
            status_code=503,
        )
    try:
        login_attempt_limiter.finish(
            peer, normalized_login, attempt_token, outcome="success"
        )
    except LoginAttemptLimiterUnavailable as exc:
        return _login_limiter_unavailable(request, exc)
    try:
        user = auth.resolve_sro_user(db, external.sro_login)
    except IntegrityError:
        # Two first-time logins for the same SRO identity can race on the
        # case-insensitive local-login index. Re-read only after rollback.
        db.rollback()
        try:
            user = auth.resolve_sro_user(db, external.sro_login)
        except (auth.InactiveUser, ValueError, IntegrityError):
            db.rollback()
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Эта учётная запись СРО не может быть связана с профилем сотрудника"},
                status_code=403,
            )
    except (auth.InactiveUser, ValueError):
        db.rollback()
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Эта учётная запись СРО не может быть связана с профилем сотрудника"},
            status_code=403,
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
    try:
        session_row = auth.new_login_session(db, user.id, session_token)
        auth.attach_external_session(
            db,
            session_row,
            external.storage_state,
            settings.secret_key,
            auth.SESSION_MAX_AGE_SECONDS,
        )
    except auth.InactiveUser:
        db.rollback()
        request_id = request_id_from_scope(request.scope)
        tasks.log(
            db,
            None,
            "session_login_failed",
            "Вход отклонён: учётная запись не активна",
            payload={"request_id": request_id} if request_id else None,
        )
        db.commit()
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Эта учётная запись СРО больше не активна"},
            status_code=401,
        )
    except Exception as exc:
        db.rollback()
        logger.error(
            "Verified SRO login could not be stored request_id=%s error_type=%s",
            request_id_from_scope(request.scope) or "unavailable",
            type(exc).__name__,
        )
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Не удалось сохранить защищённую сессию входа. Повторите позже"},
            status_code=503,
        )
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
