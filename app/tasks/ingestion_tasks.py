"""
Celery tasks for data ingestion.

- trigger_backfill: backfill all public Slack channels for a team
- trigger_github_sync: sync GitHub activity for a single user
- sync_github_all_users: nightly sweep of all linked users
"""

import asyncio
import logging
from datetime import datetime, timedelta

from app.tasks.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.models.installation import SlackInstallation
from app.models.user import UserGitHubLink
from app.ingestion.slack_ingester import SlackIngester
from app.ingestion.github_ingester import GitHubIngester
from sqlalchemy import select

logger = logging.getLogger(__name__)


def _run(coro):
    return asyncio.run(coro)


@celery_app.task(bind=True, max_retries=3, name="app.tasks.ingestion_tasks.trigger_backfill")
def trigger_backfill(self, *, team_id: str, requested_by: str, days_back: int = 30):
    """Backfill all public Slack channels for a workspace."""
    logger.info("Starting backfill for team %s (requested by %s)", team_id, requested_by)
    try:
        _run(_async_backfill(team_id=team_id, days_back=days_back))
    except Exception as exc:
        logger.exception("Backfill failed for team %s", team_id)
        raise self.retry(exc=exc, countdown=60)


async def _async_backfill(team_id: str, days_back: int) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SlackInstallation).where(SlackInstallation.team_id == team_id)
        )
        installation = result.scalar_one_or_none()
        if not installation:
            logger.error("No installation found for team %s", team_id)
            return

        ingester = SlackIngester(bot_token=installation.bot_token, team_id=team_id)
        try:
            channels = await ingester.get_public_channels()
            oldest = datetime.utcnow() - timedelta(days=days_back)

            for channel in channels:
                if not channel.get("is_member"):
                    continue
                channel_id = channel["id"]
                channel_name = channel.get("name", "")
                try:
                    count = await ingester.backfill_channel(
                        db=db,
                        channel_id=channel_id,
                        channel_name=channel_name,
                        oldest=oldest,
                    )
                    await db.commit()
                    logger.info("Backfilled %d messages from #%s", count, channel_name)
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
    """Sync GitHub activity for a single user."""
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
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserGitHubLink).where(
                UserGitHubLink.slack_user_id == slack_user_id,
                UserGitHubLink.slack_team_id == slack_team_id,
            )
        )
        link = result.scalar_one_or_none()
        if not link or not link.github_access_token:
            logger.warning("No GitHub link for user %s", slack_user_id)
            return

        ingester = GitHubIngester(
            access_token=link.github_access_token,
            github_login=link.github_login,
        )
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
    """Nightly task: sync GitHub for all linked users."""
    _run(_async_sync_all_users())


async def _async_sync_all_users() -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserGitHubLink).where(
                UserGitHubLink.github_access_token.is_not(None)
            )
        )
        links = result.scalars().all()

    for link in links:
        trigger_github_sync.delay(
            slack_user_id=link.slack_user_id,
            slack_team_id=link.slack_team_id,
            days_back=1,  # Daily incremental
        )

    logger.info("Queued GitHub sync for %d users", len(links))
