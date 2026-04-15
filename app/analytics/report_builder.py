"""
Report Builder — aggregates WorkUnits into a WorkReport.

This is the core of Phase 2. Queries WorkUnit table and builds
structured metrics that can be rendered in Slack or exported.
"""

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.models.work_unit import WorkUnit, WorkUnitType, WorkUnitSource, WorkCategory
from app.models.user import User
from app.models.team_member import TeamMember
from app.ai.schemas import WorkReport, StandupExtraction
from app.ai.work_extractor import WorkExtractor
from app.ai.insight_generator import InsightGenerator
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def build_work_report(
    db: AsyncSession,
    slack_user_id: str,
    slack_team_id: str,
    start_date: datetime,
    end_date: datetime,
    include_ai: bool | None = None,
) -> WorkReport:
    if include_ai is None:
        include_ai = settings.enable_ai_extraction

    # Resolve display name — check User table first (signed-in users),
    # then fall back to TeamMember (members added by an EM who haven't signed in).
    user_result = await db.execute(
        select(User).where(User.slack_user_id == slack_user_id)
    )
    user = user_result.scalar_one_or_none()

    # Look up TeamMember record (may exist regardless of whether user signed in)
    member_result = await db.execute(
        select(TeamMember).where(
            TeamMember.member_slack_user_id == slack_user_id,
            TeamMember.member_slack_team_id == slack_team_id,
        ).limit(1)
    )
    team_member = member_result.scalar_one_or_none()

    if user:
        display_name = user.slack_display_name or user.slack_real_name or slack_user_id
    elif team_member:
        display_name = team_member.display()
    else:
        display_name = slack_user_id

    date_range = f"{start_date.strftime('%b %d')} – {end_date.strftime('%b %d, %Y')}"

    # Base query filter
    base_filter = (
        WorkUnit.slack_user_id == slack_user_id,
        WorkUnit.slack_team_id == slack_team_id,
        WorkUnit.timestamp >= start_date,
        WorkUnit.timestamp <= end_date,
    )

    # Count by type helper
    async def count_type(*types: WorkUnitType) -> int:
        result = await db.execute(
            select(func.count(WorkUnit.id)).where(
                *base_filter,
                WorkUnit.type.in_(types),
            )
        )
        return result.scalar_one() or 0

    commits = await count_type(WorkUnitType.COMMIT)
    prs_opened = await count_type(WorkUnitType.PR_OPENED)
    prs_merged = await count_type(WorkUnitType.PR_MERGED)
    pr_reviews = await count_type(WorkUnitType.PR_REVIEW)
    issues_opened = await count_type(WorkUnitType.ISSUE_OPENED)
    standup_count = await count_type(WorkUnitType.STANDUP)
    discussion_messages = await count_type(WorkUnitType.DISCUSSION)
    thread_replies = await count_type(WorkUnitType.THREAD_REPLY)

    # Fetch standup bodies for AI extraction
    standup_result = await db.execute(
        select(WorkUnit).where(
            *base_filter,
            WorkUnit.type == WorkUnitType.STANDUP,
        ).order_by(WorkUnit.timestamp.desc()).limit(30)
    )
    standups = standup_result.scalars().all()
    standup_texts = [wu.body for wu in standups if wu.body and wu.body.strip()]

    # Fetch recent activity for the feed (most recent 100, all types)
    activity_result = await db.execute(
        select(WorkUnit).where(
            *base_filter,
        ).order_by(WorkUnit.timestamp.desc()).limit(100)
    )
    activity_units = activity_result.scalars().all()
    recent_activity = [
        {
            "source": wu.source.value,
            "type": wu.type.value,
            "title": wu.title or "",
            "body": wu.body or "",
            "url": wu.url or "",
            "github_repo": wu.github_repo or "",
            "slack_channel_id": wu.slack_channel_id or "",
            "channel_name": (
                (wu.extra_data or {}).get("channel_name") or wu.slack_channel_id or ""
            ),
            "timestamp": wu.timestamp.strftime("%b %d, %Y %H:%M"),
        }
        for wu in activity_units
    ]

    report = WorkReport(
        user_display_name=display_name,
        date_range=date_range,
        commits=commits,
        prs_opened=prs_opened,
        prs_merged=prs_merged,
        pr_reviews=pr_reviews,
        issues_opened=issues_opened,
        standup_count=standup_count,
        discussion_messages=discussion_messages,
        thread_replies=thread_replies,
        recent_standups=standup_texts[:5],
        recent_activity=recent_activity,
    )

    if include_ai and standup_texts:
        extractor = WorkExtractor()
        extractions: list[StandupExtraction] = extractor.batch_extract(standup_texts)

        category_counts: dict[str, int] = {}
        for extraction in extractions:
            for item in extraction.work_items:
                category_counts[item.category] = (
                    category_counts.get(item.category, 0) + 1
                )

        report.feature_work = category_counts.get("feature", 0)
        report.bug_fixes = category_counts.get("bug_fix", 0)
        report.architecture_work = category_counts.get("architecture", 0)
        report.mentorship = category_counts.get("mentorship", 0)
        report.incidents = category_counts.get("incident", 0)

        # Generate leadership insights
        insight_gen = InsightGenerator()
        insights = insight_gen.generate(report)
        report.ai_insights = insights.summary
        report.standup_summary = insights.standup_vs_github_alignment

    return report


def format_report_for_slack(report: WorkReport) -> list[dict]:
    """Format a WorkReport as Slack Block Kit blocks."""
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Work Report: {report.user_display_name}",
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Period:* {report.date_range}"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*GitHub Activity*"},
            "fields": [
                {"type": "mrkdwn", "text": f"*Commits*\n{report.commits}"},
                {"type": "mrkdwn", "text": f"*PRs Opened*\n{report.prs_opened}"},
                {"type": "mrkdwn", "text": f"*PRs Merged*\n{report.prs_merged}"},
                {"type": "mrkdwn", "text": f"*PR Reviews*\n{report.pr_reviews}"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Slack Activity*"},
            "fields": [
                {"type": "mrkdwn", "text": f"*Standups*\n{report.standup_count}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Discussions*\n{report.discussion_messages}",
                },
                {"type": "mrkdwn", "text": f"*Thread Replies*\n{report.thread_replies}"},
            ],
        },
    ]

    # AI section — only if we have data
    ai_fields = []
    if report.feature_work or report.bug_fixes or report.architecture_work:
        blocks.append({"type": "divider"})
        ai_fields = [
            {"type": "mrkdwn", "text": f"*Feature Work*\n{report.feature_work}"},
            {"type": "mrkdwn", "text": f"*Bug Fixes*\n{report.bug_fixes}"},
            {"type": "mrkdwn", "text": f"*Architecture*\n{report.architecture_work}"},
            {"type": "mrkdwn", "text": f"*Mentorship*\n{report.mentorship}"},
            {"type": "mrkdwn", "text": f"*Incidents*\n{report.incidents}"},
        ]
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*AI Work Classification* _(from standups)_"},
                "fields": ai_fields[:4],  # Slack limits to 10 fields
            }
        )

    if report.ai_insights:
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*AI Insights*\n{report.ai_insights}",
                },
            }
        )

    if report.standup_summary:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"_Standup vs GitHub: {report.standup_summary}_",
                    }
                ],
            }
        )

    return blocks
