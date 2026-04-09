"""
Celery tasks for normalization (raw data → WorkUnits).
Runs every 15 minutes via beat schedule.
"""

import asyncio
import logging

from app.tasks.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.models.installation import SlackInstallation
from app.normalization.normalizer import (
    normalize_slack_messages,
    normalize_github_activities,
)
from sqlalchemy import select

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.normalization_tasks.normalize_all_teams")
def normalize_all_teams():
    asyncio.run(_async_normalize_all())


async def _async_normalize_all() -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(SlackInstallation.team_id))
        team_ids = [row[0] for row in result.all()]

    for team_id in team_ids:
        normalize_team.delay(team_id=team_id)


@celery_app.task(name="app.tasks.normalization_tasks.normalize_team")
def normalize_team(*, team_id: str):
    asyncio.run(_async_normalize_team(team_id))


async def _async_normalize_team(team_id: str) -> None:
    async with AsyncSessionLocal() as db:
        slack_count = await normalize_slack_messages(db, team_id=team_id)
        github_count = await normalize_github_activities(db, team_id=team_id)
        await db.commit()
        if slack_count or github_count:
            logger.info(
                "Normalized team %s: %d slack, %d github",
                team_id, slack_count, github_count,
            )
