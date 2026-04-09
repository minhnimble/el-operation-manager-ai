"""
Slack Ingester — pulls messages from Slack using a user OAuth token.

Uses conversations.history to backfill channels the user is a member of.
No bot app or event subscriptions required.
"""

import logging
from datetime import datetime
from typing import AsyncIterator

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from tenacity import retry, stop_after_attempt, wait_exponential

from app.models.raw_data import SlackMessage
from app.models.slack_token import SlackUserToken

logger = logging.getLogger(__name__)

STANDUP_CHANNEL_KEYWORDS = {"standup", "stand-up", "daily", "scrum"}


def _is_standup_channel(channel_name: str) -> bool:
    return any(kw in channel_name.lower() for kw in STANDUP_CHANNEL_KEYWORDS)


class SlackIngester:
    def __init__(self, user_token: str, team_id: str):
        self.team_id = team_id
        self._client = httpx.AsyncClient(
            base_url="https://slack.com/api",
            headers={"Authorization": f"Bearer {user_token}"},
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def _get(self, endpoint: str, params: dict) -> dict:
        resp = await self._client.get(f"/{endpoint}", params=params)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack API error [{endpoint}]: {data.get('error')}")
        return data

    async def get_joined_channels(self) -> list[dict]:
        """Return all public channels the authenticated user is a member of."""
        channels = []
        cursor = None
        while True:
            params: dict = {
                "types": "public_channel",
                "exclude_archived": "true",
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            data = await self._get("conversations.list", params)
            # Filter to channels the user has joined
            channels.extend(
                ch for ch in data["channels"] if ch.get("is_member")
            )
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return channels

    async def iter_channel_messages(
        self,
        channel_id: str,
        oldest: float | None = None,
        latest: float | None = None,
    ) -> AsyncIterator[dict]:
        cursor = None
        while True:
            params: dict = {"channel": channel_id, "limit": 200}
            if oldest:
                params["oldest"] = str(oldest)
            if latest:
                params["latest"] = str(latest)
            if cursor:
                params["cursor"] = cursor

            try:
                data = await self._get("conversations.history", params)
            except RuntimeError as e:
                if "not_in_channel" in str(e) or "channel_not_found" in str(e):
                    logger.warning("Cannot access channel %s, skipping", channel_id)
                    return
                raise

            for msg in data.get("messages", []):
                yield msg

            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor or not data.get("has_more"):
                break

    async def backfill_channel(
        self,
        db: AsyncSession,
        channel_id: str,
        channel_name: str,
        slack_user_id: str,
        oldest: datetime | None = None,
    ) -> int:
        """Backfill a channel into SlackMessage table. Returns count saved."""
        is_standup = _is_standup_channel(channel_name)
        oldest_ts = oldest.timestamp() if oldest else None
        saved = 0

        async for msg in self.iter_channel_messages(channel_id, oldest=oldest_ts):
            subtype = msg.get("subtype")
            if subtype in {"bot_message", "channel_join", "channel_leave", "channel_purpose"}:
                continue
            user_id = msg.get("user")
            if not user_id:
                continue

            ts = msg.get("ts", "")

            existing = await db.execute(
                select(SlackMessage).where(SlackMessage.message_ts == ts)
            )
            if existing.scalar_one_or_none():
                continue

            thread_ts = msg.get("thread_ts")
            is_reply = thread_ts is not None and thread_ts != ts

            record = SlackMessage(
                slack_team_id=self.team_id,
                slack_user_id=user_id,
                channel_id=channel_id,
                channel_name=channel_name,
                message_ts=ts,
                thread_ts=thread_ts,
                text=msg.get("text", ""),
                is_standup_channel=is_standup,
                is_thread_reply=is_reply,
                raw_payload=msg,
                timestamp=datetime.utcfromtimestamp(float(ts)),
            )
            db.add(record)
            saved += 1

        await db.flush()
        logger.info("Backfilled %d messages from #%s", saved, channel_name)
        return saved

    async def get_user_info(self, user_id: str) -> dict:
        data = await self._get("users.info", {"user": user_id})
        return data["user"]


async def get_slack_ingester(
    db: AsyncSession, slack_user_id: str, team_id: str
) -> SlackIngester | None:
    result = await db.execute(
        select(SlackUserToken).where(
            SlackUserToken.slack_user_id == slack_user_id,
            SlackUserToken.slack_team_id == team_id,
        )
    )
    token_record = result.scalar_one_or_none()
    if not token_record:
        return None
    return SlackIngester(user_token=token_record.access_token, team_id=team_id)
