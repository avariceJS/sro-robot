"""Очереди RQ.

`ocr` — без состояния, воркеров можно сколько угодно.
`sro` — запись в реестр; воркер один, пока у каждого сотрудника не появится
своя браузерная сессия (`25-web-platform.md`).
"""

from __future__ import annotations

from redis import Redis
from redis.exceptions import RedisError
from rq import Queue
from rq.job import Job
from rq.registry import DeferredJobRegistry, ScheduledJobRegistry, StartedJobRegistry

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


def active_ocr_task_ids(task_ids: set[int]) -> set[int]:
    """Read active OCR deliveries so replay can reconcile a lost queue job."""
    if not task_ids:
        return set()
    try:
        job_ids = set(ocr_queue.get_job_ids())
        for registry_type in (StartedJobRegistry, ScheduledJobRegistry, DeferredJobRegistry):
            registry = registry_type(ocr_queue.name, connection=redis, queue=ocr_queue)
            job_ids.update(registry.get_job_ids(cleanup=False))
        jobs = Job.fetch_many(job_ids, connection=redis)
        active: set[int] = set()
        for job in jobs:
            if job is None or job.func_name != "app.workers.jobs.recognize":
                continue
            if len(job.args) == 1 and isinstance(job.args[0], int) and job.args[0] in task_ids:
                active.add(job.args[0])
        return active
    except (RedisError, OSError, TimeoutError) as exc:
        raise QueueUnavailable(type(exc).__name__) from None
    except Exception as exc:
        # Malformed or unreadable queue state is uncertain. Do not enqueue a
        # second copy until an operator or a later retry can reconcile it.
        raise QueueUnavailable(type(exc).__name__) from None


def _enqueue(queue: Queue, function, *args, job_id: str | None = None) -> str:
    try:
        options = {"job_id": job_id} if job_id is not None else {}
        return queue.enqueue(function, *args, **options).id
    except (RedisError, OSError, TimeoutError) as exc:
        raise QueueUnavailable(type(exc).__name__) from None


def enqueue_recognize(task_id: int) -> str:
    from .jobs import recognize

    return _enqueue(ocr_queue, recognize, task_id)


def enqueue_write(task_id: int, live: bool = False, force: bool = False,
                  preview: bool = True, user_id: int | None = None) -> str:
    from .jobs import write_to_sro

    # Браузер держится открытым, пока оператор смотрит форму, поэтому запас
    # по времени больше таймаута ожидания решения.
    return _enqueue(sro_queue, write_to_sro, task_id, live, force, preview, user_id)


def enqueue_sro_form_preview(task_id: int, user_id: int, *, job_id: str | None = None) -> str:
    """Prepare a new person form in an isolated browser context."""
    from .jobs import prepare_sro_form

    return _enqueue(sro_preview_queue, prepare_sro_form, task_id, user_id,
                    job_id=job_id)


def enqueue_sro_durable_save(task_id: int, user_id: int, expected_token: str,
                             *, job_id: str | None = None) -> str:
    """Deliver one already-authorized durable Save request to the preview queue."""
    from .jobs import save_prepared_sro_form

    return _enqueue(sro_preview_queue, save_prepared_sro_form,
                    task_id, user_id, expected_token, job_id=job_id)


def enqueue_sro_preview_refresh(task_id: int, user_id: int, request_id: str) -> str:
    """Redeliver a requested same-GUID preview without personal fields in Redis."""
    from .jobs import refresh_owned_sro_preview

    return _enqueue(sro_preview_queue, refresh_owned_sro_preview,
                    task_id, user_id, request_id)
