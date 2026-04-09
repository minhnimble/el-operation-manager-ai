"""Tests for the report builder."""

import pytest
from datetime import datetime

from app.models.work_unit import WorkUnit, WorkUnitType, WorkUnitSource
from app.analytics.report_builder import build_work_report, format_report_for_slack


@pytest.mark.asyncio
async def test_build_report_counts_correctly(db):
    # Arrange: seed work units
    base = dict(
        slack_user_id="U100",
        slack_team_id="T100",
        source=WorkUnitSource.GITHUB,
        timestamp=datetime(2024, 1, 15, 12, 0),
    )
    for wu_type, count in [
        (WorkUnitType.COMMIT, 5),
        (WorkUnitType.PR_OPENED, 2),
        (WorkUnitType.PR_REVIEW, 3),
    ]:
        for _ in range(count):
            db.add(WorkUnit(type=wu_type, **base))

    db.add(WorkUnit(
        slack_user_id="U100",
        slack_team_id="T100",
        source=WorkUnitSource.SLACK,
        type=WorkUnitType.STANDUP,
        body="Yesterday: shipped login feature. Today: code review.",
        timestamp=datetime(2024, 1, 15, 9, 0),
    ))
    await db.flush()

    # Act
    report = await build_work_report(
        db=db,
        slack_user_id="U100",
        slack_team_id="T100",
        start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 1, 31),
        include_ai=False,  # skip AI in unit tests
    )

    # Assert
    assert report.commits == 5
    assert report.prs_opened == 2
    assert report.pr_reviews == 3
    assert report.standup_count == 1


def test_format_report_returns_blocks():
    from app.ai.schemas import WorkReport
    report = WorkReport(
        user_display_name="Alice",
        date_range="Jan 1 – Jan 31, 2024",
        commits=10,
        prs_opened=3,
        standup_count=5,
    )
    blocks = format_report_for_slack(report)
    assert isinstance(blocks, list)
    assert len(blocks) > 0
    assert blocks[0]["type"] == "header"
