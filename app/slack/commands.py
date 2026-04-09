"""
Slash command handlers.

Commands:
  /work-report [@user] [last-week | last-month | YYYY-MM-DD:YYYY-MM-DD]
  /link-github
  /backfill
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta

from app.slack.app import bolt_app
from app.database import AsyncSessionLocal
from app.analytics.report_builder import build_work_report, format_report_for_slack
from app.tasks.ingestion_tasks import trigger_backfill, trigger_github_sync

logger = logging.getLogger(__name__)


def _parse_date_range(token: str) -> tuple[datetime, datetime]:
    now = datetime.utcnow()
    token = token.strip().lower()
    if token in ("last-week", "lastweek", "week"):
        return now - timedelta(days=7), now
    if token in ("last-month", "lastmonth", "month"):
        return now - timedelta(days=30), now
    if ":" in token:
        parts = token.split(":")
        start = datetime.strptime(parts[0].strip(), "%Y-%m-%d")
        end = datetime.strptime(parts[1].strip(), "%Y-%m-%d").replace(
            hour=23, minute=59, second=59
        )
        return start, end
    return now - timedelta(days=7), now


def _extract_user_id(text: str) -> str | None:
    match = re.search(r"<@([A-Z0-9]+)(?:\|[^>]+)?>", text)
    return match.group(1) if match else None


async def _build_report_async(slack_user_id, slack_team_id, start, end):
    async with AsyncSessionLocal() as db:
        return await build_work_report(
            db=db,
            slack_user_id=slack_user_id,
            slack_team_id=slack_team_id,
            start_date=start,
            end_date=end,
        )


@bolt_app.command("/work-report")
def cmd_work_report(ack, command, respond) -> None:
    ack()
    text = command.get("text", "").strip()
    team_id = command["team_id"]
    requesting_user = command["user_id"]

    parts = text.split()
    target_user_id = None
    date_token = "last-week"

    for part in parts:
        if part.startswith("<@"):
            uid = _extract_user_id(part)
            if uid:
                target_user_id = uid
        else:
            date_token = part

    if not target_user_id:
        target_user_id = requesting_user

    respond(
        text=f"Generating work report for <@{target_user_id}>...",
        response_type="ephemeral",
    )

    try:
        start, end = _parse_date_range(date_token)
        report = asyncio.run(_build_report_async(target_user_id, team_id, start, end))
        blocks = format_report_for_slack(report)
        respond(blocks=blocks, response_type="in_channel")
    except Exception as e:
        logger.exception("work-report command failed")
        respond(text=f"Error generating report: {e}", response_type="ephemeral")


@bolt_app.command("/link-github")
def cmd_link_github(ack, command, respond) -> None:
    ack()
    from app.config import get_settings
    settings = get_settings()

    user_id = command["user_id"]
    team_id = command["team_id"]
    state = f"{team_id}:{user_id}"
    github_oauth_url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={settings.github_client_id}"
        f"&scope=read:user,repo"
        f"&state={state}"
        f"&redirect_uri={settings.app_base_url}/auth/github/callback"
    )

    respond(
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "Connect your GitHub account to enable commit + PR tracking.\n\n"
                        f"<{github_oauth_url}|Click here to authorize GitHub access>"
                    ),
                },
            }
        ],
        response_type="ephemeral",
    )


@bolt_app.command("/backfill")
def cmd_backfill(ack, command, respond) -> None:
    ack()
    team_id = command["team_id"]
    user_id = command["user_id"]

    trigger_backfill.delay(team_id=team_id, requested_by=user_id)

    respond(
        text=(
            "Backfill started for all public channels. "
            "This runs in the background and may take several minutes."
        ),
        response_type="ephemeral",
    )
