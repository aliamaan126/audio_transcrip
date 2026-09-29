from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "transcriptions",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=24 * 3600,
    timezone="UTC",
    beat_schedule={
        "cleanup-expired-audio": {
            "task": "jobs.cleanup_expired_audio",
            "schedule": settings.retention_cleanup_hours * 3600,
        }
    },
)
