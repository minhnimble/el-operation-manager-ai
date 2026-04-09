"""Tests for the normalization layer."""

import pytest
from datetime import datetime

from app.models.raw_data import SlackMessage
from app.models.work_unit import WorkUnit, WorkUnitType
from app.normalization.normalizer import normalize_slack_messages


@pytest.mark.asyncio
async def test_standup_message_normalized_correctly(db):
    # Arrange
    msg = SlackMessage(
        slack_team_id="T001",
        slack_user_id="U001",
        channel_id="C001",
        channel_name="daily-standup",
        message_ts="1234567890.000100",
        text="Yesterday: worked on auth PR. Today: finishing up tests. Blocker: none.",
        is_standup_channel=True,
        is_thread_reply=False,
        timestamp=datetime(2024, 1, 15, 9, 0),
    )
    db.add(msg)
    await db.flush()

    # Act
    count = await normalize_slack_messages(db, team_id="T001")

    # Assert
    assert count == 1
    result = await db.execute(
        __import__("sqlalchemy", fromlist=["select"]).select(WorkUnit).where(
            WorkUnit.slack_user_id == "U001"
        )
    )
    wu = result.scalar_one()
    assert wu.type == WorkUnitType.STANDUP
    assert wu.slack_team_id == "T001"


@pytest.mark.asyncio
async def test_thread_reply_classified_correctly(db):
    msg = SlackMessage(
        slack_team_id="T002",
        slack_user_id="U002",
        channel_id="C002",
        channel_name="engineering",
        message_ts="1234567890.000200",
        thread_ts="1234567890.000100",
        text="LGTM, merging this.",
        is_standup_channel=False,
        is_thread_reply=True,
        timestamp=datetime(2024, 1, 15, 10, 0),
    )
    db.add(msg)
    await db.flush()

    count = await normalize_slack_messages(db, team_id="T002")
    assert count == 1

    from sqlalchemy import select
    result = await db.execute(
        select(WorkUnit).where(WorkUnit.slack_user_id == "U002")
    )
    wu = result.scalar_one()
    assert wu.type == WorkUnitType.THREAD_REPLY
