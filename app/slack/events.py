"""
Slack event handlers.

Handles:
- app_mention: respond to @bot mentions
- message events: capture standup messages in real time
- app_home_opened: show onboarding / status
"""

import logging
from datetime import datetime

from app.slack.app import bolt_app
from app.database import AsyncSessionLocal
from app.models.raw_data import SlackMessage
from app.models.user import User
from app.ingestion.slack_ingester import _is_standup_channel
from sqlalchemy import select
import asyncio

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run an async coroutine from a sync Bolt handler."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
    except RuntimeError:
        pass
    return asyncio.run(coro)


@bolt_app.event("message")
def handle_message(event: dict, say, logger=logger) -> None:
    """Capture every public channel message into SlackMessage table."""
    # Skip bot messages, deleted messages, etc.
    if event.get("subtype") in {"bot_message", "message_deleted", "message_changed"}:
        return
    user_id = event.get("user")
    if not user_id:
        return

    team_id = event.get("team") or ""
    channel_id = event.get("channel", "")
    ts = event.get("ts", "")
    thread_ts = event.get("thread_ts")
    text = event.get("text", "")

    _run_async(_save_message(
        team_id=team_id,
        user_id=user_id,
        channel_id=channel_id,
        channel_name=event.get("channel_name", ""),
        ts=ts,
        thread_ts=thread_ts,
        text=text,
        raw=event,
    ))


async def _save_message(
    team_id: str,
    user_id: str,
    channel_id: str,
    channel_name: str,
    ts: str,
    thread_ts: str | None,
    text: str,
    raw: dict,
) -> None:
    async with AsyncSessionLocal() as db:
        existing = await db.execute(
            select(SlackMessage).where(SlackMessage.message_ts == ts)
        )
        if existing.scalar_one_or_none():
            return

        is_standup = _is_standup_channel(channel_name) if channel_name else False
        is_reply = thread_ts is not None and thread_ts != ts

        record = SlackMessage(
            slack_team_id=team_id,
            slack_user_id=user_id,
            channel_id=channel_id,
            channel_name=channel_name,
            message_ts=ts,
            thread_ts=thread_ts,
            text=text,
            is_standup_channel=is_standup,
            is_thread_reply=is_reply,
            raw_payload=raw,
            timestamp=datetime.utcfromtimestamp(float(ts)) if ts else datetime.utcnow(),
        )
        db.add(record)
        await db.commit()


@bolt_app.event("app_home_opened")
def handle_app_home_opened(event: dict, client) -> None:
    user_id = event.get("user")
    if not user_id:
        return

    client.views_publish(
        user_id=user_id,
        view={
            "type": "home",
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "Engineering Intelligence"},
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "Welcome to the Engineering Operations Manager.\n\n"
                            "*Available commands:*\n"
                            "• `/work-report @user [last-week | last-month | YYYY-MM-DD:YYYY-MM-DD]`\n"
                            "• `/link-github` — connect your GitHub account\n"
                            "• `/backfill` — backfill channel history _(admin)_"
                        ),
                    },
                },
            ],
        },
    )
