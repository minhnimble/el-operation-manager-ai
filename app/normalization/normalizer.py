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
    # Standup bots often post a question at the top level and collect responses
    # in the thread — both the top-level reply AND thread replies in a standup
    # channel should be classified as STANDUP.
    if msg.is_standup_channel:
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
    # Track ts values added in this batch so the dedup SELECT (which queries the
    # DB, not the in-session pending objects) doesn't miss them.  Combined with
    # no_autoflush this prevents the "Query-invoked autoflush" IntegrityError
    # that fires when a SELECT triggers a flush of a pending WorkUnit whose
    # slack_message_ts already exists in the DB.
    seen_ts: set[str] = set()

    with db.no_autoflush:
        for msg in messages:
            ts = msg.message_ts

            # In-batch duplicate (same ts appeared more than once in the batch)
            if ts in seen_ts:
                await db.execute(
                    update(SlackMessage).where(SlackMessage.id == msg.id).values(processed=True)
                )
                continue

            # Dedup: skip if a WorkUnit already exists for this Slack message timestamp.
            existing_wu = await db.execute(
                select(WorkUnit.id).where(WorkUnit.slack_message_ts == ts)
            )
            if existing_wu.scalar_one_or_none() is not None:
                await db.execute(
                    update(SlackMessage).where(SlackMessage.id == msg.id).values(processed=True)
                )
                continue

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
                slack_message_ts=ts,
                timestamp=msg.timestamp,
                extra_data={
                    "channel_name": msg.channel_name,
                    "thread_ts": msg.thread_ts,
                },
            )
            db.add(work_unit)
            seen_ts.add(ts)

            await db.execute(
                update(SlackMessage)
                .where(SlackMessage.id == msg.id)
                .values(processed=True)
            )
            created += 1

    # Single explicit flush after the loop — no autoflush surprises mid-batch.
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
    seen_refs: set[tuple[str, str, str]] = set()

    with db.no_autoflush:
        for act in activities:
            wu_type = _GITHUB_TYPE_MAP.get(act.activity_type, WorkUnitType.COMMIT)
            ref_key = (act.ref_id or "", act.repo_full_name or "", wu_type.value)

            # In-batch duplicate
            if ref_key in seen_refs:
                await db.execute(
                    update(GitHubActivity).where(GitHubActivity.id == act.id).values(processed=True)
                )
                continue

            # Dedup: skip if a WorkUnit already exists for this GitHub ref.
            existing_wu = await db.execute(
                select(WorkUnit.id).where(
                    WorkUnit.github_ref_id == act.ref_id,
                    WorkUnit.github_repo == act.repo_full_name,
                    WorkUnit.type == wu_type,
                )
            )
            if existing_wu.scalar_one_or_none() is not None:
                await db.execute(
                    update(GitHubActivity).where(GitHubActivity.id == act.id).values(processed=True)
                )
                continue

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
                extra_data=act.raw_payload,
            )
            db.add(work_unit)
            seen_refs.add(ref_key)

            await db.execute(
                update(GitHubActivity)
                .where(GitHubActivity.id == act.id)
                .values(processed=True)
            )
            created += 1

    await db.flush()
    logger.info("Normalized %d GitHub activities for team %s", created, team_id)
    return created
