"""
Slack Ingester — pulls messages from Slack using a user OAuth token.

Uses conversations.history to backfill channels the user is a member of.
No bot app or event subscriptions required.

Standup handling covers two common bot patterns:
  1. Bot posts question top-level; users reply in-thread with their own account.
     → Thread replies have a real `user` field — captured via conversations.replies.
  2. Bot collects answers privately then re-posts them as bot_messages with the
     user's full name as the `username` override (e.g. Geekbot style).
     → We match `username` against TeamMember / User display names to attribute
       the message to the correct Slack user ID.
"""

import logging
from datetime import datetime
from typing import AsyncIterator

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10),
           reraise=True)
    async def _get(self, endpoint: str, params: dict) -> dict:
        resp = await self._client.get(f"/{endpoint}", params=params)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack API error [{endpoint}]: {data.get('error')}")
        return data

    async def _list_channels(self, types: str) -> list[dict]:
        """Paginate conversations.list for the given channel types."""
        channels = []
        cursor = None
        while True:
            params: dict = {
                "types": types,
                "exclude_archived": "true",
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            data = await self._get("conversations.list", params)
            channels.extend(ch for ch in data["channels"] if ch.get("is_member"))
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return channels

    async def get_joined_channels(self) -> list[dict]:
        """Return all public + private channels the authenticated user is a member of.

        Falls back to public-only if the token lacks the groups:read scope
        needed for private channels.
        """
        try:
            return await self._list_channels("public_channel,private_channel")
        except Exception as e:
            # Unwrap RetryError to get the real cause
            cause = getattr(e, "last_attempt", None)
            if cause is not None:
                try:
                    cause = cause.result()
                except Exception as inner:
                    cause = inner
            else:
                cause = e

            err_str = str(cause).lower()
            if any(kw in err_str for kw in ("missing_scope", "not_allowed", "invalid_types")):
                logger.warning(
                    "Token lacks groups:read scope — falling back to public channels only. "
                    "Reconnect Slack with the groups:read scope to include private channels."
                )
                return await self._list_channels("public_channel")

            # Any other error: re-raise with the real message, not the RetryError wrapper
            raise RuntimeError(str(cause)) from None

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

    async def iter_thread_replies(
        self,
        channel_id: str,
        thread_ts: str,
        oldest: float | None = None,
    ) -> AsyncIterator[dict]:
        """Fetch all user replies in a thread (skips the parent message itself)."""
        cursor = None
        while True:
            params: dict = {"channel": channel_id, "ts": thread_ts, "limit": 200}
            if oldest:
                params["oldest"] = str(oldest)
            if cursor:
                params["cursor"] = cursor

            try:
                data = await self._get("conversations.replies", params)
            except RuntimeError as e:
                if "thread_not_found" in str(e):
                    return
                raise

            for msg in data.get("messages", []):
                # Skip the parent message (ts == thread_ts) — handled separately
                if msg.get("ts") == thread_ts:
                    continue
                yield msg

            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor or not data.get("has_more"):
                break

    # ── Name → user-id resolution (for bot-posted standup summaries) ───────────

    async def _resolve_user_by_name(
        self, db: AsyncSession, display_name: str
    ) -> str | None:
        """Look up a Slack user ID by display or real name.

        Checks TeamMember records for this workspace, then signed-in Users.
        Returns the first matching slack_user_id, or None.
        """
        # Import here to avoid circular imports at module level
        from app.models.team_member import TeamMember
        from app.models.user import User

        name_lower = display_name.strip().lower()
        if not name_lower:
            return None

        # TeamMember display name
        result = await db.execute(
            select(TeamMember.member_slack_user_id).where(
                TeamMember.member_slack_team_id == self.team_id,
                func.lower(TeamMember.member_display_name) == name_lower,
            ).limit(1)
        )
        uid = result.scalar_one_or_none()
        if uid:
            return uid

        # TeamMember real name
        result = await db.execute(
            select(TeamMember.member_slack_user_id).where(
                TeamMember.member_slack_team_id == self.team_id,
                func.lower(TeamMember.member_real_name) == name_lower,
            ).limit(1)
        )
        uid = result.scalar_one_or_none()
        if uid:
            return uid

        # Signed-in User (display name)
        result = await db.execute(
            select(User.slack_user_id).where(
                User.slack_team_id == self.team_id,
                func.lower(User.slack_display_name) == name_lower,
            ).limit(1)
        )
        uid = result.scalar_one_or_none()
        if uid:
            return uid

        # Signed-in User (real name)
        result = await db.execute(
            select(User.slack_user_id).where(
                User.slack_team_id == self.team_id,
                func.lower(User.slack_real_name) == name_lower,
            ).limit(1)
        )
        return result.scalar_one_or_none()

    # ── Message persistence ────────────────────────────────────────────────────

    async def _save_message(
        self,
        db: AsyncSession,
        msg: dict,
        channel_id: str,
        channel_name: str,
        is_standup: bool,
        is_reply: bool,
        user_id: str | None = None,
    ) -> bool:
        """Persist a single Slack message. Returns True if newly saved.

        `user_id` overrides msg['user'] — used when attributing bot-posted
        standup summaries to the real author via name resolution.
        """
        uid = user_id or msg.get("user")
        if not uid:
            return False

        ts = msg.get("ts", "")
        if not ts:
            return False

        existing = await db.execute(
            select(SlackMessage).where(SlackMessage.message_ts == ts)
        )
        if existing.scalar_one_or_none():
            return False

        thread_ts = msg.get("thread_ts")
        record = SlackMessage(
            slack_team_id=self.team_id,
            slack_user_id=uid,
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
        return True

    async def backfill_channel(
        self,
        db: AsyncSession,
        channel_id: str,
        channel_name: str,
        slack_user_id: str,
        oldest: datetime | None = None,
    ) -> int:
        """Backfill a channel into SlackMessage table. Returns count saved.

        Handles two standup-bot patterns:
        - Thread replies: bot asks question top-level; users reply in-thread
          with their own Slack account → captured via conversations.replies.
        - Bot-reposted summaries: bot reposts the user's answer as a
          bot_message with username=<user's full name> → we resolve the name
          to a TeamMember / User slack_user_id.
        """
        is_standup = _is_standup_channel(channel_name)
        oldest_ts = oldest.timestamp() if oldest else None
        saved = 0

        async for msg in self.iter_channel_messages(channel_id, oldest=oldest_ts):
            subtype = msg.get("subtype", "")
            ts = msg.get("ts", "")

            # ── Skip non-content events ──────────────────────────────────────
            if subtype in {"channel_join", "channel_leave", "channel_purpose"}:
                continue

            # ── Bot-posted standup summaries (Geekbot-style) ─────────────────
            # The bot posts with username=<member's full name> and no `user` field.
            if subtype == "bot_message" and is_standup:
                bot_username = (
                    msg.get("username")
                    or msg.get("user_profile", {}).get("display_name", "")
                ).strip()
                if bot_username:
                    resolved_uid = await self._resolve_user_by_name(db, bot_username)
                    if resolved_uid:
                        if await self._save_message(
                            db, msg, channel_id, channel_name,
                            is_standup=True, is_reply=False,
                            user_id=resolved_uid,
                        ):
                            saved += 1
                # Don't fall through — handled above (or intentionally skipped)
                # Still fetch thread replies on this bot message if any
                if msg.get("reply_count", 0) > 0:
                    async for reply in self.iter_thread_replies(channel_id, ts, oldest_ts):
                        if reply.get("subtype", "") in {"channel_join", "channel_leave"}:
                            continue
                        reply_uid = reply.get("user")
                        if reply_uid:
                            if await self._save_message(
                                db, reply, channel_id, channel_name,
                                is_standup=is_standup, is_reply=True,
                            ):
                                saved += 1
                continue

            # ── Regular user messages ────────────────────────────────────────
            if subtype not in {"bot_message"}:
                if await self._save_message(
                    db, msg, channel_id, channel_name,
                    is_standup=is_standup, is_reply=False,
                ):
                    saved += 1

            # Fetch thread replies for any message that has them
            if msg.get("reply_count", 0) > 0:
                async for reply in self.iter_thread_replies(channel_id, ts, oldest_ts):
                    if reply.get("subtype", "") in {"channel_join", "channel_leave"}:
                        continue
                    # Bot replies in threads: try name resolution in standup channels
                    reply_subtype = reply.get("subtype", "")
                    if reply_subtype == "bot_message" and is_standup:
                        bot_username = (
                            reply.get("username")
                            or reply.get("user_profile", {}).get("display_name", "")
                        ).strip()
                        if bot_username:
                            resolved_uid = await self._resolve_user_by_name(db, bot_username)
                            if resolved_uid:
                                if await self._save_message(
                                    db, reply, channel_id, channel_name,
                                    is_standup=True, is_reply=True,
                                    user_id=resolved_uid,
                                ):
                                    saved += 1
                    elif not reply_subtype or reply_subtype not in {"bot_message"}:
                        if await self._save_message(
                            db, reply, channel_id, channel_name,
                            is_standup=is_standup, is_reply=True,
                        ):
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
