"""Очереди RQ.

`ocr` — без состояния, воркеров можно сколько угодно.
`sro` — запись в реестр; воркер один, пока у каждого сотрудника не появится
своя браузерная сессия (`25-web-platform.md`).
"""

from __future__ import annotations

from redis import Redis
from rq import Queue

from ..services.leases import OCR_JOB_TIMEOUT_SECONDS
from ..settings import settings


def _create_redis_client() -> Redis:
    """Keep API/worker queue calls bounded when Redis is unavailable."""
    return Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=1,
        socket_timeout=5,
    )


redis = _create_redis_client()

ocr_queue = Queue("ocr", connection=redis, default_timeout=OCR_JOB_TIMEOUT_SECONDS)
sro_queue = Queue("sro", connection=redis, default_timeout=900)


def enqueue_recognize(task_id: int) -> str:
    from .jobs import recognize

    return ocr_queue.enqueue(recognize, task_id).id


def enqueue_kad_recognize(task_id: int) -> str:
    from .jobs import recognize_from_kad

    return ocr_queue.enqueue(recognize_from_kad, task_id).id


def enqueue_write(task_id: int, live: bool = False, force: bool = False,
                  preview: bool = True, user_id: int | None = None) -> str:
    from .jobs import write_to_sro

    # Браузер держится открытым, пока оператор смотрит форму, поэтому запас
    # по времени больше таймаута ожидания решения.
    return sro_queue.enqueue(write_to_sro, task_id, live, force, preview, user_id).id
