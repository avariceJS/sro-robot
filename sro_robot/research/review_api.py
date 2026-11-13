"""Private review API; remote mode requires an exact HTTPS reverse-proxy origin."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from .review_store import ReviewStore, ReviewError, ReviewConflict
from .review_diagnostics import diagnostic_packet

COOKIE = 'sro_local_review'
STATIC = Path(__file__).parent / 'static'
REMOTE_COOKIE = '__Host-sro_review'
LOGIN_HTML = '''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход — проверка документов</title><body><main><h1>Проверка документов</h1>
<form method="post" action="/login"><p><label>Учётная запись
<input name="username" autocomplete="username" required maxlength="80"></label></p>
<p><label>Пароль <input name="password" type="password" autocomplete="current-password"
required maxlength="256"></label></p><button type="submit">Войти</button></form></main></body></html>'''


def remote_origin(value: str) -> str:
    """Accept one canonical HTTPS origin, never paths, wildcards or proxy headers."""
    if not isinstance(value, str):
        raise ValueError('invalid_public_origin')
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except (ValueError, TypeError):
        raise ValueError('invalid_public_origin') from None
    if (parsed.scheme != 'https' or not host or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment or '*' in value or port == 443
            or value != 'https://' + parsed.netloc or parsed.netloc != parsed.netloc.lower()
            or any(c.isspace() for c in value) or port is not None and not 1 <= port <= 65535):
        raise ValueError('invalid_public_origin')
    # Restrict DNS labels / IPv4 / bracketed IPv6; no ambiguous URL host syntax.
    import ipaddress
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if (len(host) > 253 or any(not label or len(label) > 63 or not label[0].isalnum()
                or not label[-1].isalnum() or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in label)
                for label in host.split('.'))):
            raise ValueError('invalid_public_origin')
    return value


def validate_remote_configuration(public_origin, expert_identity, session_seconds):
    remote_origin(public_origin)
    if (not isinstance(expert_identity, str) or not 1 <= len(expert_identity) <= 80
            or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.' for c in expert_identity)
            or type(session_seconds) is not int or not 60 <= session_seconds <= 86400):
        raise ValueError('invalid_remote_account_configuration')


def create_app(store: ReviewStore, *, launch_token: str, port: int = 8767,
               public_origin: str | None = None, expert_identity: str | None = None,
               session_seconds: int = 43200):
    if not isinstance(launch_token, str) or len(launch_token) < 32 or not 1024 <= port <= 65535:
        raise ValueError('invalid_local_service_configuration')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    remote = public_origin is not None
    if remote:
        validate_remote_configuration(public_origin, expert_identity, session_seconds)
    elif expert_identity is not None:
        raise ValueError('invalid_remote_account_configuration')
    session = hmac.new(launch_token.encode(), b'sro-local-review-session-v1', hashlib.sha256).hexdigest()
    hosts = {urlsplit(public_origin).netloc} if remote else {f'127.0.0.1:{port}', f'localhost:{port}'}
    origins = {public_origin} if remote else {'http://' + host for host in hosts}
    cookie_name = REMOTE_COOKIE if remote else COOKIE
    failures = deque(maxlen=10)

    def sign(value):
        return hmac.new(launch_token.encode(), ('remote-review-v1:' + public_origin + ':' + value).encode(), hashlib.sha256).hexdigest()

    def authenticated(cookie):
        if not remote:
            return hmac.compare_digest(cookie.encode(), session.encode())
        try:
            issued, expires, nonce, signature = cookie.split('.')
            now = int(time.time())
            value = '.'.join((issued, expires, nonce))
            return (len(cookie) <= 200 and len(nonce) == 32 and int(issued) <= now < int(expires)
                    and int(expires) - int(issued) == session_seconds
                    and hmac.compare_digest(signature, sign(expert_identity + ':' + value)))
        except (ValueError, TypeError):
            return False

    @app.middleware('http')
    async def local_session(request: Request, call_next):
        host = request.headers.get('host', '')
        origin = request.headers.get('origin')
        if (len(request.headers.getlist('host')) != 1 or len(request.headers.getlist('origin')) > 1
                or host not in hosts or (origin is not None and origin not in origins)):
            return JSONResponse({'detail': 'local_origin_required'}, status_code=403)
        if request.method not in {'GET', 'HEAD'} and origin not in origins:
            return JSONResponse({'detail': 'local_origin_required'}, status_code=403)
        if remote and request.url.path == '/session':
            response = JSONResponse({'detail': 'not_found'}, status_code=404)
        elif request.url.path != ('/login' if remote else '/session') and not authenticated(request.cookies.get(cookie_name, '')):
            response = (RedirectResponse('/login', status_code=303) if remote and request.url.path == '/' and request.method == 'GET'
                        else JSONResponse({'detail': 'review_session_required' if remote else 'local_session_required'}, status_code=401))
        else:
            response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        if remote:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src 'self'; object-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'"
        return response

    @app.exception_handler(ReviewError)
    async def review_failure(request, error):
        return JSONResponse({'detail': str(error)}, status_code=409 if isinstance(error, ReviewConflict) else 422)

    @app.get('/session')
    def bootstrap(token: str = ''):
        if remote:
            return JSONResponse({'detail': 'not_found'}, status_code=404)
        if not hmac.compare_digest(token.encode(), launch_token.encode()):
            return JSONResponse({'detail': 'local_session_required'}, status_code=401)
        response = RedirectResponse('/', status_code=303)
        response.set_cookie(COOKIE, session, httponly=True, samesite='strict', max_age=31536000)
        return response

    @app.get('/login')
    def login_page():
        return HTMLResponse(LOGIN_HTML) if remote else JSONResponse({'detail': 'not_found'}, status_code=404)

    @app.post('/login')
    async def login(request: Request):
        if not remote:
            return JSONResponse({'detail': 'not_found'}, status_code=404)
        now = time.time()
        while failures and failures[0] <= now - 60:
            failures.popleft()
        if len(failures) >= 10:
            return JSONResponse({'detail': 'login_rate_limit'}, status_code=429, headers={'Retry-After': '60'})
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 4096:
                return JSONResponse({'detail': 'request_size_limit'}, status_code=413)
            body.extend(chunk)
        # Streaming yields to other logins. Recheck after the final await so the
        # credential check and failure reservation run atomically on this loop.
        now = time.time()
        while failures and failures[0] <= now - 60:
            failures.popleft()
        if len(failures) >= 10:
            return JSONResponse({'detail': 'login_rate_limit'}, status_code=429, headers={'Retry-After': '60'})
        try:
            values = parse_qs(body.decode('utf-8'), strict_parsing=True, max_num_fields=2)
            username, password = values.get('username', ['']), values.get('password', [''])
            valid = (set(values) == {'username', 'password'} and len(username) == len(password) == 1
                     and hmac.compare_digest(username[0].encode(), expert_identity.encode())
                     and hmac.compare_digest(password[0].encode(), launch_token.encode()))
        except (ValueError, UnicodeDecodeError):
            valid = False
        if not valid:
            failures.append(now)
            return JSONResponse({'detail': 'invalid_login'}, status_code=401)
        issued = int(time.time())
        value = f'{issued}.{issued + session_seconds}.{secrets.token_hex(16)}'
        response = RedirectResponse('/', status_code=303)
        response.set_cookie(cookie_name, value + '.' + sign(expert_identity + ':' + value), secure=True,
                            httponly=True, samesite='strict', max_age=session_seconds, path='/')
        return response

    @app.post('/logout')
    def logout():
        response = RedirectResponse('/login' if remote else '/', status_code=303)
        response.delete_cookie(cookie_name, secure=remote, httponly=True, samesite='strict', path='/')
        return response

    @app.get('/')
    def index():
        return FileResponse(STATIC / 'review.html', media_type='text/html')

    @app.get('/static/{name}')
    def static(name: str):
        if name not in {'review.js', 'review.css'}:
            return JSONResponse({'detail': 'not_found'}, status_code=404)
        return FileResponse(STATIC / name, media_type='text/javascript' if name.endswith('.js') else 'text/css')

    @app.get('/api/stats')
    def stats():
        return store.stats()

    @app.get('/api/next')
    def next_document(after: str | None = None):
        return {'document': store.next_document(after), 'stats': store.stats()}

    @app.get('/api/documents/{document_id}')
    def document(document_id: str):
        result = store.get_document(document_id)
        return {'document': result} if result else JSONResponse({'detail': 'not_found'}, status_code=404)

    @app.get('/api/documents/{document_id}/original')
    def original(document_id: str):
        if store.get_document(document_id) is None:
            return JSONResponse({'detail': 'not_found'}, status_code=404)
        path, media = store.asset(document_id)
        return FileResponse(path, media_type=media, filename='original' + path.suffix, content_disposition_type='inline')

    @app.post('/api/documents/{document_id}/review')
    async def review(document_id: str, request: Request):
        try:
            length = int(request.headers.get('content-length', '0') or 0)
        except ValueError:
            return JSONResponse({'detail': 'invalid_request_length'}, status_code=422)
        if length < 0 or length > 512 * 1024:
            return JSONResponse({'detail': 'request_size_limit'}, status_code=413)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 512 * 1024:
                return JSONResponse({'detail': 'request_size_limit'}, status_code=413)
            body.extend(chunk)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({'detail': 'invalid_review'}, status_code=422)
        return store.apply_review(document_id, payload, actor=expert_identity) if remote else store.apply_review(document_id, payload)

    @app.get('/api/documents/{document_id}/preview/{page}')
    def preview(document_id: str, page: int):
        if store.get_document(document_id) is None:
            return JSONResponse({'detail': 'not_found'}, status_code=404)
        return FileResponse(store.preview(document_id, page), media_type='image/png')

    @app.get('/api/diagnostics/summary')
    def diagnostic_summary():
        return diagnostic_packet(store.diagnostics_summary())

    return app
