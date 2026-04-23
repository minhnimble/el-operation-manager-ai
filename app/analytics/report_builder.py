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
from app.ui.time_format import to_gmt7

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
        real_name = user.slack_real_name or ""
        email = getattr(user, "slack_email", "") or ""
    elif team_member:
        display_name = team_member.display()
        real_name = team_member.member_real_name or ""
        email = team_member.member_email or ""
    else:
        display_name = slack_user_id
        real_name = ""
        email = ""

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

    # Build channel_id → channel_name lookup from SlackMessage for fallback
    from app.models.raw_data import SlackMessage as _SlackMessage
    _ch_rows = await db.execute(
        select(_SlackMessage.channel_id, _SlackMessage.channel_name)
        .where(_SlackMessage.slack_team_id == slack_team_id)
        .distinct()
    )
    _channel_name_map: dict[str, str] = {
        row.channel_id: row.channel_name
        for row in _ch_rows.all()
        if row.channel_id and row.channel_name
    }

    # ── Activity feed ─────────────────────────────────────────────────────────
    # The single-query / limit(100) approach used to be a footgun for power
    # users: someone with hundreds of PR reviews would see their entire feed
    # filled with GitHub items, leaving 5–10 slots for Slack standups and
    # discussions — even though the header counters reported the true totals.
    #
    # Slack content is small (~bytes per row) and is the primary thing humans
    # actually read in the report, so we fetch it without a limit. GitHub
    # activity can run into the thousands for active reviewers, so we keep a
    # generous cap that's high enough to make the per-category PR expanders
    # (Created / Merged / Reviewed) match the header counts in practice.
    GITHUB_ACTIVITY_LIMIT = 1000

    slack_activity_result = await db.execute(
        select(WorkUnit).where(
            *base_filter,
            WorkUnit.source == WorkUnitSource.SLACK,
        ).order_by(WorkUnit.timestamp.desc())
    )
    github_activity_result = await db.execute(
        select(WorkUnit).where(
            *base_filter,
            WorkUnit.source == WorkUnitSource.GITHUB,
        ).order_by(WorkUnit.timestamp.desc()).limit(GITHUB_ACTIVITY_LIMIT)
    )
    # Merge and re-sort so the page can keep its existing
    # "newest first, filter by source" rendering logic unchanged.
    activity_units = sorted(
        list(slack_activity_result.scalars().all())
        + list(github_activity_result.scalars().all()),
        key=lambda wu: wu.timestamp,
        reverse=True,
    )

    # ── Enrich Slack items with sender + file attachments ─────────────────────
    # We persist the SlackMessage row's slack_user_id as the *target* of the
    # sync (mention-attribution rewrites the author), so the WorkUnit's
    # slack_user_id is unreliable as "who actually wrote this". The original
    # author lives in SlackMessage.raw_payload["user"] (or "username" /
    # "user_profile" for bot reposts). Same payload also carries any uploaded
    # files we can render inline. One bulk query keeps this O(1).
    slack_ts_list = [
        wu.slack_message_ts for wu in activity_units
        if wu.source == WorkUnitSource.SLACK and wu.slack_message_ts
    ]
    slack_payload_by_ts: dict[str, dict] = {}
    if slack_ts_list:
        sm_rows = await db.execute(
            select(_SlackMessage.message_ts, _SlackMessage.raw_payload).where(
                _SlackMessage.message_ts.in_(slack_ts_list),
            )
        )
        for ts, payload in sm_rows.all():
            if payload:
                slack_payload_by_ts[ts] = payload

    def _extract_sender(payload: dict) -> tuple[str | None, str | None]:
        """Return (sender_user_id, sender_display_name) from a Slack payload.

        Bot reposts (Standuply etc.) don't have a real `user` field — the bot
        posts under the user's name via `username` / `user_profile`. For those
        we surface the display name instead.
        """
        if not payload:
            return None, None
        uid = payload.get("user")
        if uid:
            return uid, None
        # Bot-reposted on behalf of someone — name only, no resolvable uid
        name = (
            payload.get("username")
            or (payload.get("user_profile") or {}).get("display_name")
            or (payload.get("user_profile") or {}).get("real_name")
        )
        return None, name

    def _extract_files(payload: dict) -> list[dict]:
        """Return a normalized list of file metadata for inline rendering.

        Only fields the UI needs — kept small to avoid bloating the report
        payload. Slack file URLs are private and require Bearer auth, so the
        UI fetches them via the user's stored token at render time.
        """
        if not payload:
            return []
        files = payload.get("files") or []
        out: list[dict] = []
        for f in files:
            if not isinstance(f, dict):
                continue
            out.append({
                "id": f.get("id"),
                "name": f.get("name") or f.get("title") or "file",
                "mimetype": f.get("mimetype") or "",
                "filetype": f.get("filetype") or "",
                "size": f.get("size") or 0,
                # Prefer a thumbnail for inline display; fall back to full URL.
                "url_private": f.get("url_private") or "",
                "thumb_url": (
                    f.get("thumb_720")
                    or f.get("thumb_480")
                    or f.get("thumb_360")
                    or f.get("thumb_160")
                    or ""
                ),
                "permalink": f.get("permalink") or "",
            })
        return out

    recent_activity: list[dict] = []
    for wu in activity_units:
        item = {
            "source": wu.source.value,
            "type": wu.type.value,
            "title": wu.title or "",
            "body": wu.body or "",
            "url": wu.url or "",
            "github_repo": wu.github_repo or "",
            "slack_channel_id": wu.slack_channel_id or "",
            "channel_name": (
                (wu.extra_data or {}).get("channel_name")
                or _channel_name_map.get(wu.slack_channel_id or "")
                or wu.slack_channel_id
                or ""
            ),
            # GMT+7 with AM/PM suffix — 24h format was being misread as 12h
            # without meridiem, so make the period explicit.
            "timestamp": to_gmt7(wu.timestamp).strftime("%b %d, %Y %I:%M %p"),
            # Keep the raw datetime alongside the pre-formatted display string.
            # The page-level sorting in PR expanders needs chronological order,
            # and sorting "%b %d, %Y %H:%M" strings orders months alphabetically
            # (Apr < Aug < Dec < Feb ...), which scrambles cross-month data.
            "timestamp_dt": wu.timestamp,
            "slack_message_ts": wu.slack_message_ts or "",
            "sender_id": None,
            "sender_name": None,
            "files": [],
        }
        if wu.source == WorkUnitSource.SLACK and wu.slack_message_ts:
            payload = slack_payload_by_ts.get(wu.slack_message_ts)
            if payload:
                sid, sname = _extract_sender(payload)
                item["sender_id"] = sid
                item["sender_name"] = sname
                item["files"] = _extract_files(payload)
        recent_activity.append(item)

    report = WorkReport(
        user_display_name=display_name,
        user_real_name=real_name,
        user_email=email,
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
        from app.ai.work_extractor import AIBillingError
        try:
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

        except AIBillingError as e:
            logger.warning("AI disabled — billing error: %s", e)
            report.ai_error = str(e)

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
