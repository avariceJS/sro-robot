from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
from types import SimpleNamespace

from starlette.requests import Request

sys.path.insert(0, os.environ["PPV_CHECKOUT"])

from app.main import logger, unexpected_error


def invoke(path: str, route_template: str | None, query: bytes = b"") -> tuple[str, int, str]:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query,
        "headers": [],
        "state": {"request_id": "d" * 32},
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }
    if route_template is not None:
        scope["route"] = SimpleNamespace(path=route_template)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        response = asyncio.run(unexpected_error(Request(scope), RuntimeError("synthetic")))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    output = stream.getvalue().strip()
    return output, response.status_code, response.body.decode("utf-8")


def main(mode: str) -> None:
    if mode == "task-id":
        raw_path = "/tasks/741923/run"
        route_template = "/tasks/{task_id}/run"
        log, _, _ = invoke(raw_path, route_template)
        print("PPV_REACHED", flush=True)
        assert "741923" not in log, "concrete task identifier was written to the error log"
        assert "route=/tasks/{task_id}/run" in log
    elif mode == "unmatched":
        raw_path = "/lookup/synthetic-person@example.invalid"
        log, _, _ = invoke(raw_path, None, b"token=synthetic-query-secret")
        print("PPV_REACHED", flush=True)
        assert raw_path not in log, "user-controlled unmatched path was written to the error log"
        assert "synthetic-person@example.invalid" not in log
        assert "synthetic-query-secret" not in log
        assert "route=<unmatched>" in log
    elif mode == "control":
        log, status, body = invoke("/healthz", "/healthz")
        assert "request_id=" + "d" * 32 in log
        assert "error_type=RuntimeError" in log
        assert status == 500 and body == "Внутренняя ошибка сервера"
    elif mode == "behavior":
        _, status, body = invoke("/healthz", "/healthz")
        print(json.dumps({"status": status, "body": body}, ensure_ascii=False, sort_keys=True))
    elif mode == "adjacent":
        log, _, _ = invoke("/lookup", None, b"q=synthetic@example.invalid")
        assert "synthetic@example.invalid" not in log
    else:
        raise SystemExit("unknown mode")


if __name__ == "__main__":
    main(sys.argv[1])
