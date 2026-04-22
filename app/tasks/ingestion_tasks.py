"""
Celery tasks for data ingestion.

- trigger_backfill: backfill all joined Slack channels for a user
- trigger_github_sync: sync GitHub activity for a single user
- sync_github_all_users: nightly sweep of all linked users
- sync_slack_all_users: nightly Slack backfill (incremental)
"""

import asyncio
import logging
from datetime import datetime, timedelta

from app.tasks.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.models.slack_token import SlackUserToken
from app.models.user import UserGitHubLink
from app.ingestion.slack_ingester import SlackIngester
from app.ingestion.github_ingester import GitHubIngester
from sqlalchemy import select

logger = logging.getLogger(__name__)


def _run(coro):
    return asyncio.run(coro)


@celery_app.task(bind=True, max_retries=3, name="app.tasks.ingestion_tasks.trigger_backfill")
def trigger_backfill(
    self,
    *,
    slack_user_id: str,
    team_id: str,
    days_back: int = 30,
):
    """Backfill all joined Slack channels for a single user."""
    logger.info("Starting Slack backfill for user %s (team %s)", slack_user_id, team_id)
    try:
        _run(_async_backfill(slack_user_id=slack_user_id, team_id=team_id, days_back=days_back))
    except Exception as exc:
        logger.exception("Backfill failed for user %s", slack_user_id)
        raise self.retry(exc=exc, countdown=60)


async def _async_backfill(slack_user_id: str, team_id: str, days_back: int) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SlackUserToken).where(
                SlackUserToken.slack_user_id == slack_user_id,
                SlackUserToken.slack_team_id == team_id,
            )
        )
        token_record = result.scalar_one_or_none()
        if not token_record:
            logger.error("No Slack token for user %s", slack_user_id)
            return

        ingester = SlackIngester(user_token=token_record.access_token, team_id=team_id)
        try:
            channels, _warnings = await ingester.get_joined_channels()
            oldest = datetime.utcnow() - timedelta(days=days_back)

            for channel in channels:
                channel_id = channel["id"]
                channel_name = channel.get("name", "")
                try:
                    count, unresolved = await ingester.backfill_channel(
                        db=db,
                        channel_id=channel_id,
                        channel_name=channel_name,
                        slack_user_id=slack_user_id,
                        oldest=oldest,
                    )
                    await db.commit()
                    logger.info(
                        "Backfilled %d messages from #%s for user %s (unresolved: %s)",
                        count, channel_name, slack_user_id, unresolved or "none",
                    )
                except Exception as e:
                    logger.warning("Failed to backfill #%s: %s", channel_name, e)
                    await db.rollback()
        finally:
            await ingester.close()


@celery_app.task(bind=True, max_retries=3, name="app.tasks.ingestion_tasks.trigger_github_sync")
def trigger_github_sync(
    self,
    *,
    slack_user_id: str,
    slack_team_id: str,
    days_back: int = 30,
):
    logger.info("Syncing GitHub for user %s (team %s)", slack_user_id, slack_team_id)
    try:
        _run(_async_github_sync(
            slack_user_id=slack_user_id,
            slack_team_id=slack_team_id,
            days_back=days_back,
        ))
    except Exception as exc:
        logger.exception("GitHub sync failed for user %s", slack_user_id)
        raise self.retry(exc=exc, countdown=120)


async def _async_github_sync(
    slack_user_id: str, slack_team_id: str, days_back: int
) -> None:
    from app.config import get_settings
    pat = (get_settings().github_pat or "").strip()
    if not pat:
        logger.warning("GITHUB_PAT not configured — skipping GitHub sync")
        return

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserGitHubLink.github_login).where(
                UserGitHubLink.slack_user_id == slack_user_id,
                UserGitHubLink.slack_team_id == slack_team_id,
            )
        )
        login = result.scalar_one_or_none()
        if not login:
            logger.warning("No GitHub login mapped for user %s", slack_user_id)
            return

        ingester = GitHubIngester(access_token=pat, github_login=login)
        try:
            since = datetime.utcnow() - timedelta(days=days_back)
            counts = await ingester.ingest_user_activity(
                db=db,
                slack_team_id=slack_team_id,
                slack_user_id=slack_user_id,
                since=since,
            )
            await db.commit()
            logger.info("GitHub sync complete for %s: %s", slack_user_id, counts)
        finally:
            await ingester.close()


@celery_app.task(name="app.tasks.ingestion_tasks.sync_github_all_users")
def sync_github_all_users():
    _run(_async_sync_all_github())


async def _async_sync_all_github() -> None:
    """Fan-out: queue a sync for every user with a github_login mapping."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserGitHubLink).where(
                UserGitHubLink.github_login.is_not(None)
            )
        )
        links = result.scalars().all()

    for link in links:
        trigger_github_sync.delay(
            slack_user_id=link.slack_user_id,
            slack_team_id=link.slack_team_id,
            days_back=1,
        )
    logger.info("Queued GitHub sync for %d users", len(links))


@celery_app.task(name="app.tasks.ingestion_tasks.sync_slack_all_users")
def sync_slack_all_users():
    _run(_async_sync_all_slack())


async def _async_sync_all_slack() -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(SlackUserToken))
        tokens = result.scalars().all()

    for token in tokens:
        trigger_backfill.delay(
            slack_user_id=token.slack_user_id,
            team_id=token.slack_team_id,
            days_back=1,  # incremental daily
        )
    logger.info("Queued Slack backfill for %d users", len(tokens))
