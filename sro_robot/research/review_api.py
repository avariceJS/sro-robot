"""Loopback-only review API. It never imports application settings or office DBs."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from .review_store import ReviewStore, ReviewError, ReviewConflict
from .review_diagnostics import diagnostic_packet

COOKIE = 'sro_local_review'
STATIC = Path(__file__).parent / 'static'


def create_app(store: ReviewStore, *, launch_token: str, port: int = 8767):
    if not isinstance(launch_token, str) or len(launch_token) < 32 or not 1024 <= port <= 65535:
        raise ValueError('invalid_local_service_configuration')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    session = hmac.new(launch_token.encode(), b'sro-local-review-session-v1', hashlib.sha256).hexdigest()
    hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
    origins = {'http://' + host for host in hosts}

    @app.middleware('http')
    async def local_session(request: Request, call_next):
        host = request.headers.get('host', '')
        origin = request.headers.get('origin')
        if host not in hosts or (origin is not None and origin not in origins):
            return JSONResponse({'detail': 'local_origin_required'}, status_code=403)
        if request.method not in {'GET', 'HEAD'} and origin not in origins:
            return JSONResponse({'detail': 'local_origin_required'}, status_code=403)
        if request.url.path != '/session':
            cookie = request.cookies.get(COOKIE, '')
            if not hmac.compare_digest(cookie.encode(), session.encode()):
                return JSONResponse({'detail': 'local_session_required'}, status_code=401)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src 'self'; object-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'"
        return response

    @app.exception_handler(ReviewError)
    async def review_failure(request, error):
        return JSONResponse({'detail': str(error)}, status_code=409 if isinstance(error, ReviewConflict) else 422)

    @app.get('/session')
    def bootstrap(token: str = ''):
        if not hmac.compare_digest(token.encode(), launch_token.encode()):
            return JSONResponse({'detail': 'local_session_required'}, status_code=401)
        response = RedirectResponse('/', status_code=303)
        response.set_cookie(COOKIE, session, httponly=True, samesite='strict', max_age=31536000)
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
        return store.apply_review(document_id, payload)

    @app.get('/api/documents/{document_id}/preview/{page}')
    def preview(document_id: str, page: int):
        if store.get_document(document_id) is None:
            return JSONResponse({'detail': 'not_found'}, status_code=404)
        return FileResponse(store.preview(document_id, page), media_type='image/png')

    @app.get('/api/diagnostics/summary')
    def diagnostic_summary():
        return diagnostic_packet(store.diagnostics_summary())

    return app
