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

    connect_url = f"{settings.app_base_url}/Connect"

    respond(
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "GitHub now uses a *Personal Access Token (PAT)* — no OAuth flow.\n\n"
                        "1. Create a PAT at "
                        "<https://github.com/settings/tokens/new?description=Engineering+Operations+Manager&scopes=repo,read:org|github.com/settings/tokens> "
                        "with scopes `repo` + `read:org`.\n"
                        f"2. Paste it on the <{connect_url}|Connect Accounts page>."
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
