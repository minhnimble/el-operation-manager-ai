"""
Normalizer — converts raw SlackMessage and GitHubActivity rows into WorkUnits.

This is the Layer 2 abstraction. Every raw activity becomes a WorkUnit.
"""

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, tuple_, update

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
    db: AsyncSession, team_id: str, batch_size: int = 2000
) -> int:
    """Process unprocessed SlackMessages into WorkUnits. Returns count created.

    Batched DB access — all dedup SELECTs, the processed-flag UPDATE, and
    the WorkUnit inserts collapse into one round-trip each, instead of
    three per row. Critical on high-latency DB connections like the
    Supabase transaction pooler, where per-row N+1 traffic dominates.
    """
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

    # One dedup SELECT for the whole batch — all ts values that already
    # have a WorkUnit, fetched in one shot.
    batch_ts = {m.message_ts for m in messages if m.message_ts}
    existing_ts: set[str] = set()
    if batch_ts:
        existing_rows = await db.execute(
            select(WorkUnit.slack_message_ts).where(
                WorkUnit.slack_message_ts.in_(batch_ts)
            )
        )
        existing_ts = {t for t in existing_rows.scalars().all() if t}

    new_units: list[WorkUnit] = []
    seen_ts: set[str] = set()  # tracks intra-batch duplicates
    processed_ids: list[int] = []

    for msg in messages:
        ts = msg.message_ts
        processed_ids.append(msg.id)

        if not ts or ts in existing_ts or ts in seen_ts:
            continue

        wu_type = _classify_slack_message(msg)
        text = msg.text or ""
        title = text[:100] + ("..." if len(text) > 100 else "")

        new_units.append(WorkUnit(
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
        ))
        seen_ts.add(ts)

    with db.no_autoflush:
        if new_units:
            db.add_all(new_units)
        if processed_ids:
            await db.execute(
                update(SlackMessage)
                .where(SlackMessage.id.in_(processed_ids))
                .values(processed=True)
            )
        await db.flush()

    created = len(new_units)
    logger.info("Normalized %d Slack messages for team %s", created, team_id)
    return created


async def normalize_github_activities(
    db: AsyncSession, team_id: str, batch_size: int = 2000
) -> int:
    """Process unprocessed GitHubActivity rows into WorkUnits. Returns count created.

    Batched DB access — same rationale as ``normalize_slack_messages``:
    collapse N+1 dedup SELECTs + per-row UPDATEs into one round-trip each.
    Dedup key is the triple (ref_id, repo_full_name, work_unit_type), fetched
    for the whole batch in a single tuple-IN query.
    """
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

    # Precompute per-row keys (type derivation once per row).
    rows: list[tuple[GitHubActivity, WorkUnitType, tuple[str, str, WorkUnitType]]] = []
    for act in activities:
        wu_type = _GITHUB_TYPE_MAP.get(act.activity_type, WorkUnitType.COMMIT)
        key = (act.ref_id or "", act.repo_full_name or "", wu_type)
        rows.append((act, wu_type, key))

    # One dedup SELECT for the whole batch using a tuple-IN on the three
    # dedup columns. Postgres supports composite-IN natively.
    batch_keys = {k for _, _, k in rows if k[0] and k[1]}
    existing_keys: set[tuple[str, str, WorkUnitType]] = set()
    if batch_keys:
        existing_rows = await db.execute(
            select(
                WorkUnit.github_ref_id,
                WorkUnit.github_repo,
                WorkUnit.type,
            ).where(
                tuple_(
                    WorkUnit.github_ref_id,
                    WorkUnit.github_repo,
                    WorkUnit.type,
                ).in_(batch_keys)
            )
        )
        existing_keys = {(r, p, t) for r, p, t in existing_rows.all()}

    new_units: list[WorkUnit] = []
    seen_keys: set[tuple[str, str, WorkUnitType]] = set()
    processed_ids: list[int] = []

    for act, wu_type, key in rows:
        processed_ids.append(act.id)

        if not key[0] or key in existing_keys or key in seen_keys:
            continue

        new_units.append(WorkUnit(
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
        ))
        seen_keys.add(key)

    with db.no_autoflush:
        if new_units:
            db.add_all(new_units)
        if processed_ids:
            await db.execute(
                update(GitHubActivity)
                .where(GitHubActivity.id.in_(processed_ids))
                .values(processed=True)
            )
        await db.flush()

    created = len(new_units)
    logger.info("Normalized %d GitHub activities for team %s", created, team_id)
    return created
