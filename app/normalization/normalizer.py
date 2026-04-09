"""
Normalizer — converts raw SlackMessage and GitHubActivity rows into WorkUnits.

This is the Layer 2 abstraction. Every raw activity becomes a WorkUnit.
"""

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from app.models.raw_data import SlackMessage, GitHubActivity
from app.models.work_unit import WorkUnit, WorkUnitSource, WorkUnitType

logger = logging.getLogger(__name__)

_GITHUB_TYPE_MAP: dict[str, WorkUnitType] = {
    "commit": WorkUnitType.COMMIT,
    "pr_opened": WorkUnitType.PR_OPENED,
    "pr_merged": WorkUnitType.PR_MERGED,
    "pr_review": WorkUnitType.PR_REVIEW,
    "issue_opened": WorkUnitType.ISSUE_OPENED,
    "issue_closed": WorkUnitType.ISSUE_CLOSED,
    "issue_comment": WorkUnitType.ISSUE_COMMENT,
}


def _classify_slack_message(msg: SlackMessage) -> WorkUnitType:
    if msg.is_standup_channel and not msg.is_thread_reply:
        return WorkUnitType.STANDUP
    if msg.is_thread_reply:
        return WorkUnitType.THREAD_REPLY
    return WorkUnitType.DISCUSSION


async def normalize_slack_messages(
    db: AsyncSession, team_id: str, batch_size: int = 500
) -> int:
    """Process unprocessed SlackMessages into WorkUnits. Returns count created."""
    result = await db.execute(
        select(SlackMessage)
        .where(
            SlackMessage.slack_team_id == team_id,
            SlackMessage.processed == False,  # noqa: E712
        )
        .limit(batch_size)
    )
    messages = result.scalars().all()
    if not messages:
        return 0

    created = 0
    for msg in messages:
        wu_type = _classify_slack_message(msg)
        text = msg.text or ""
        title = text[:100] + ("..." if len(text) > 100 else "")

        work_unit = WorkUnit(
            slack_user_id=msg.slack_user_id,
            slack_team_id=msg.slack_team_id,
            source=WorkUnitSource.SLACK,
            type=wu_type,
            title=title,
            body=text,
            slack_channel_id=msg.channel_id,
            slack_message_ts=msg.message_ts,
            timestamp=msg.timestamp,
            metadata={
                "channel_name": msg.channel_name,
                "thread_ts": msg.thread_ts,
            },
        )
        db.add(work_unit)

        await db.execute(
            update(SlackMessage)
            .where(SlackMessage.id == msg.id)
            .values(processed=True)
        )
        created += 1

    await db.flush()
    logger.info("Normalized %d Slack messages for team %s", created, team_id)
    return created


async def normalize_github_activities(
    db: AsyncSession, team_id: str, batch_size: int = 500
) -> int:
    """Process unprocessed GitHubActivity rows into WorkUnits. Returns count created."""
    result = await db.execute(
        select(GitHubActivity)
        .where(
            GitHubActivity.slack_team_id == team_id,
            GitHubActivity.processed == False,  # noqa: E712
        )
        .limit(batch_size)
    )
    activities = result.scalars().all()
    if not activities:
        return 0

    created = 0
    for act in activities:
        wu_type = _GITHUB_TYPE_MAP.get(act.activity_type, WorkUnitType.COMMIT)
        work_unit = WorkUnit(
            slack_user_id=act.slack_user_id,
            slack_team_id=act.slack_team_id,
            github_login=act.github_login,
            source=WorkUnitSource.GITHUB,
            type=wu_type,
            title=act.title,
            url=act.url,
            github_repo=act.repo_full_name,
            github_ref_id=act.ref_id,
            timestamp=act.activity_at,
            metadata=act.raw_payload,
        )
        db.add(work_unit)

        await db.execute(
            update(GitHubActivity)
            .where(GitHubActivity.id == act.id)
            .values(processed=True)
        )
        created += 1

    await db.flush()
    logger.info("Normalized %d GitHub activities for team %s", created, team_id)
    return created
