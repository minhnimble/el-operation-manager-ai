from celery import Celery
from celery.schedules import crontab
from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "el_ops",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.tasks.ingestion_tasks",
        "app.tasks.normalization_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)

celery_app.conf.beat_schedule = {
    # Normalize raw data every 15 minutes
    "normalize-all-teams": {
        "task": "app.tasks.normalization_tasks.normalize_all_teams",
        "schedule": crontab(minute="*/15"),
    },
    # Incremental Slack sync for all users — daily at 1am UTC
    "sync-slack-all-users": {
        "task": "app.tasks.ingestion_tasks.sync_slack_all_users",
        "schedule": crontab(hour=1, minute=0),
    },
    # Incremental GitHub sync for all users — daily at 2am UTC
    "sync-github-all-users": {
        "task": "app.tasks.ingestion_tasks.sync_github_all_users",
        "schedule": crontab(hour=2, minute=0),
    },
}
