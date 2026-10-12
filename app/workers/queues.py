"""Очереди RQ.

`ocr` — без состояния, воркеров можно сколько угодно.
`sro` — запись в реестр; воркер один, пока у каждого сотрудника не появится
своя браузерная сессия (`25-web-platform.md`).
"""

from __future__ import annotations

from redis import Redis
from redis.exceptions import RedisError
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
sro_preview_queue = Queue("sro_preview", connection=redis, default_timeout=900)


class QueueUnavailable(RuntimeError):
    """A job was not confirmed enqueued because Redis admission failed."""

    def __init__(self, error_type: str) -> None:
        super().__init__("Background queue admission is unavailable")
        self.error_type = error_type


def _enqueue(queue: Queue, function, *args) -> str:
    try:
        return queue.enqueue(function, *args).id
    except (RedisError, OSError, TimeoutError) as exc:
        raise QueueUnavailable(type(exc).__name__) from None


def enqueue_recognize(task_id: int) -> str:
    from .jobs import recognize

    return _enqueue(ocr_queue, recognize, task_id)


def enqueue_kad_recognize(task_id: int) -> str:
    from .jobs import recognize_from_kad

    return _enqueue(ocr_queue, recognize_from_kad, task_id)


def enqueue_write(task_id: int, live: bool = False, force: bool = False,
                  preview: bool = True, user_id: int | None = None) -> str:
    from .jobs import write_to_sro

    # Браузер держится открытым, пока оператор смотрит форму, поэтому запас
    # по времени больше таймаута ожидания решения.
    return _enqueue(sro_queue, write_to_sro, task_id, live, force, preview, user_id)


def enqueue_sro_form_preview(task_id: int, user_id: int) -> str:
    """Prepare an unsaved new person form; the worker blocks every other POST."""
    from .jobs import prepare_sro_form

    return _enqueue(sro_preview_queue, prepare_sro_form, task_id, user_id)
