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

import asyncio
import logging
from datetime import datetime
from typing import AsyncIterator

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update
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

    @retry(stop=stop_after_attempt(6), wait=wait_exponential(min=1, max=10),
           reraise=True)
    async def _get(self, endpoint: str, params: dict) -> dict:
        resp = await self._client.get(f"/{endpoint}", params=params)

        # Handle rate limiting: sleep for the server-requested duration then
        # raise so tenacity retries the request.
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "30"))
            logger.warning(
                "Slack rate limit hit on %s — waiting %ds before retry",
                endpoint, retry_after,
            )
            await asyncio.sleep(retry_after)
            raise RuntimeError(f"Rate limited [{endpoint}] — retrying")

        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack API error [{endpoint}]: {data.get('error')}")
        return data

    async def _paginate_channels(self, channel_type: str) -> list[dict]:
        """Fetch one channel type at a time — more reliable than mixing types."""
        channels = []
        cursor = None
        while True:
            params: dict = {
                "types": channel_type,
                "exclude_archived": "true",
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            data = await self._get("conversations.list", params)
            for ch in data.get("channels", []):
                # Public: only joined ones (is_member distinguishes joined vs not).
                # Private: API already filters to member-only channels, but
                # is_member is unreliable — include everything returned.
                if ch.get("is_private") or ch.get("is_member"):
                    channels.append(ch)
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
            # conversations.list is Tier 2 (~20 req/min) — be conservative
            await asyncio.sleep(1.0)
        return channels

    async def get_joined_channels(self) -> tuple[list[dict], list[str]]:
        """Return (channels, warnings).

        Makes separate API calls for public and private channels — mixing
        types in a single call doesn't reliably return private channels.
        Warnings describe any scope issues so the caller can surface them.
        """
        warnings: list[str] = []

        # Public channels — requires channels:read
        public = await self._paginate_channels("public_channel")

        # Private channels — requires groups:read (listing) + groups:history (reading)
        private: list[dict] = []
        try:
            private = await self._paginate_channels("private_channel")
        except RuntimeError as e:
            err = str(e).lower()
            if any(kw in err for kw in ("missing_scope", "not_allowed", "invalid_types")):
                warnings.append(
                    "Private channels skipped — token is missing `groups:read` scope. "
                    "Add it in your Slack app, then use **Reconnect Slack** on Connect Accounts."
                )
                logger.warning("groups:read scope missing — skipping private channels")
            else:
                raise

        return public + private, warnings

    async def find_channels_by_names(self, names: set[str]) -> list[dict]:
        """Return channel dicts whose name (lowercased) is in *names*.

        Paginates both public and private channels and stops as soon as all
        requested names are found, so it is much cheaper than loading the full
        channel list when you only need a handful of specific channels.
        """
        remaining = {n.lower() for n in names}
        found: list[dict] = []

        for ch_type in ("public_channel", "private_channel"):
            if not remaining:
                break
            cursor = None
            while remaining:
                params: dict = {
                    "types": ch_type,
                    "exclude_archived": "true",
                    "limit": 200,
                }
                if cursor:
                    params["cursor"] = cursor
                try:
                    data = await self._get("conversations.list", params)
                except RuntimeError:
                    break
                for ch in data.get("channels", []):
                    ch_name = ch.get("name", "").lower()
                    if ch_name in remaining:
                        found.append(ch)
                        remaining.discard(ch_name)
                cursor = data.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break

        return found

    async def is_member(self, channel_id: str, user_id: str) -> bool:
        """Return True if user_id is a member of channel_id.

        Paginates conversations.members and exits as soon as the user is found,
        so for channels where the user IS a member this is typically one API call.
        """
        cursor = None
        while True:
            params: dict = {"channel": channel_id, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            try:
                data = await self._get("conversations.members", params)
            except RuntimeError:
                return False
            if user_id in data.get("members", []):
                return True
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                return False

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
                err = str(e).lower()
                if any(kw in err for kw in ("not_in_channel", "channel_not_found")):
                    logger.warning("Cannot access channel %s (not a member), skipping", channel_id)
                    return
                if "missing_scope" in err:
                    # Private channel history requires groups:history scope
                    raise RuntimeError(
                        f"Missing scope for #{channel_id} — add `groups:history` to your "
                        f"Slack app scopes and reconnect."
                    ) from None
                raise

            for msg in data.get("messages", []):
                yield msg

            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor or not data.get("has_more"):
                break

            # Small pause between pages to stay well under Slack's rate limits
            await asyncio.sleep(0.5)

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

            # Small pause between pages to stay well under Slack's rate limits
            await asyncio.sleep(0.5)

    # ── Name → user-id resolution (for bot-posted standup summaries) ───────────

    async def _get_names_for_user(self, db: AsyncSession, user_id: str) -> set[str]:
        """Return all known lowercased name variants for a given slack_user_id.

        Used as a fast pre-filter: when filter_user_id is set we only run the
        full _resolve_user_by_name query when the bot username is in this set,
        skipping all the expensive lookups for other team members' names.
        """
        from app.models.team_member import TeamMember
        from app.models.user import User

        names: set[str] = set()

        result = await db.execute(
            select(
                TeamMember.member_display_name,
                TeamMember.member_real_name,
            ).where(
                TeamMember.member_slack_team_id == self.team_id,
                TeamMember.member_slack_user_id == user_id,
            ).limit(1)
        )
        row = result.one_or_none()
        if row:
            if row.member_display_name:
                names.add(row.member_display_name.strip().lower())
            if row.member_real_name:
                names.add(row.member_real_name.strip().lower())

        result = await db.execute(
            select(
                User.slack_display_name,
                User.slack_real_name,
            ).where(
                User.slack_team_id == self.team_id,
                User.slack_user_id == user_id,
            ).limit(1)
        )
        row = result.one_or_none()
        if row:
            if row.slack_display_name:
                names.add(row.slack_display_name.strip().lower())
            if row.slack_real_name:
                names.add(row.slack_real_name.strip().lower())

        names.discard("")
        return names

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

    # ── Relevance filter ──────────────────────────────────────────────────────

    @staticmethod
    def _is_relevant(msg: dict, user_id: str | None, resolved_uid: str | None = None) -> bool:
        """Return True if the message should be saved for this user.

        A message is relevant when:
        - The sender IS the user  (msg["user"] == user_id or resolved_uid matches)
        - The user is @mentioned  (<@USER_ID> or <@USER_ID|name> in text)

        When user_id is None the message is always relevant (no filter applied).
        """
        if user_id is None:
            return True
        effective_uid = resolved_uid or msg.get("user", "")
        if effective_uid == user_id:
            return True
        text = msg.get("text", "")
        return f"<@{user_id}>" in text or f"<@{user_id}|" in text

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
        existing_record = existing.scalar_one_or_none()
        if existing_record:
            # If we have a user_id override and the existing record is under a
            # different user, update it so the message appears in the right report.
            if uid and existing_record.slack_user_id != uid:
                await db.execute(
                    update(SlackMessage)
                    .where(SlackMessage.message_ts == ts)
                    .values(slack_user_id=uid)
                )
                # Also fix any already-normalized WorkUnit so the report picks it up
                from app.models.work_unit import WorkUnit
                await db.execute(
                    update(WorkUnit)
                    .where(WorkUnit.slack_message_ts == ts)
                    .values(slack_user_id=uid)
                )
                logger.debug(
                    "Re-attributed message %s from %s to %s",
                    ts, existing_record.slack_user_id, uid,
                )
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
        latest: datetime | None = None,
        filter_user_id: str | None = None,
    ) -> tuple[int, list[str]]:
        """Backfill a channel into SlackMessage table.

        Returns (count_saved, unresolved_bot_usernames).

        oldest / latest — inclusive time bounds passed to the Slack API.
        filter_user_id — when set, only messages sent by OR mentioning this
        user are stored. Pass None to store all messages (EM syncing for self).

        Handles two standup-bot patterns:
        - Thread replies: bot asks question top-level; users reply in-thread
          with their own Slack account → captured via conversations.replies.
        - Bot-reposted summaries: bot reposts the user's answer as a
          bot_message with username=<user's full name> → we resolve the name
          to a TeamMember / User slack_user_id.
        """
        is_standup = _is_standup_channel(channel_name)
        oldest_ts = oldest.timestamp() if oldest else None
        latest_ts = latest.timestamp() if latest else None
        saved = 0
        unresolved_bot_names: list[str] = []

        # Pre-load target user's known names so we can skip name resolution for
        # every other team member's bot messages in standup channels.
        # When filter_user_id is None (EM syncing for self) we resolve everyone.
        target_names: set[str] | None = None
        if filter_user_id and is_standup:
            target_names = await self._get_names_for_user(db, filter_user_id)
            logger.info(
                "Standup name filter for user %s in #%s: %s",
                filter_user_id, channel_name, target_names,
            )

        async for msg in self.iter_channel_messages(channel_id, oldest=oldest_ts, latest=latest_ts):
            subtype = msg.get("subtype", "")
            ts = msg.get("ts", "")

            # ── Skip non-content events ──────────────────────────────────────
            if subtype in {"channel_join", "channel_leave", "channel_purpose"}:
                continue

            # ── Bot-posted standup summaries (Geekbot-style) ─────────────────
            if subtype == "bot_message" and is_standup:
                bot_username = (
                    msg.get("username")
                    or msg.get("user_profile", {}).get("display_name", "")
                ).strip()
                if bot_username:
                    # Skip resolution entirely if target_names is set and this
                    # username clearly belongs to someone else.
                    if target_names is not None and bot_username.lower() not in target_names:
                        resolved_uid = None
                    else:
                        resolved_uid = await self._resolve_user_by_name(db, bot_username)
                    logger.info(
                        "Standup bot message in #%s: username=%r → resolved_uid=%s "
                        "(filter_user_id=%s)",
                        channel_name, bot_username, resolved_uid, filter_user_id,
                    )
                    # For bot standup messages the ONLY valid check is whether the
                    # name resolves to the target user — mentions don't apply here
                    # (we don't want Alice's standup just because it mentions Don).
                    uid_matches = (
                        resolved_uid is not None
                        and (filter_user_id is None or resolved_uid == filter_user_id)
                    )
                    if uid_matches:
                        if await self._save_message(
                            db, msg, channel_id, channel_name,
                            is_standup=True, is_reply=False,
                            user_id=resolved_uid,
                        ):
                            saved += 1
                    elif resolved_uid is None and filter_user_id is None:
                        # Only warn about unresolved names when syncing for self
                        # (no filter). When syncing a specific member we only care
                        # that their own entry is captured — other names are noise.
                        if bot_username not in unresolved_bot_names:
                            unresolved_bot_names.append(bot_username)
                if msg.get("reply_count", 0) > 0:
                    async for reply in self.iter_thread_replies(channel_id, ts, oldest_ts):
                        rsubtype = reply.get("subtype", "")
                        if rsubtype in {"channel_join", "channel_leave"}:
                            continue

                        if rsubtype == "bot_message":
                            # Bot-posted standup reply (e.g. Nimble Bot posts each
                            # member's answer in-thread with username = their name).
                            r_bot_username = (
                                reply.get("username")
                                or reply.get("user_profile", {}).get("display_name", "")
                            ).strip()
                            if r_bot_username:
                                if target_names is not None and r_bot_username.lower() not in target_names:
                                    r_resolved = None
                                else:
                                    r_resolved = await self._resolve_user_by_name(db, r_bot_username)
                                logger.info(
                                    "Standup thread reply in #%s: username=%r → resolved=%s",
                                    channel_name, r_bot_username, r_resolved,
                                )
                                r_matches = (
                                    r_resolved is not None
                                    and (filter_user_id is None or r_resolved == filter_user_id)
                                )
                                if r_matches:
                                    if await self._save_message(
                                        db, reply, channel_id, channel_name,
                                        is_standup=True, is_reply=True,
                                        user_id=r_resolved,
                                    ):
                                        saved += 1
                                elif r_resolved is None and filter_user_id is None:
                                    if r_bot_username not in unresolved_bot_names:
                                        unresolved_bot_names.append(r_bot_username)
                        elif self._is_relevant(reply, filter_user_id):
                            # Regular user reply in thread — capture if relevant
                            reply_sender = reply.get("user", "")
                            reply_override = (
                                filter_user_id
                                if (filter_user_id and reply_sender != filter_user_id)
                                else None
                            )
                            if await self._save_message(
                                db, reply, channel_id, channel_name,
                                is_standup=is_standup, is_reply=True,
                                user_id=reply_override,
                            ):
                                saved += 1
                continue

            # ── Regular user messages ────────────────────────────────────────
            if subtype not in {"bot_message"}:
                if self._is_relevant(msg, filter_user_id):
                    # Attribute to filter_user_id if relevant only due to mention
                    sender = msg.get("user", "")
                    override_uid = (
                        filter_user_id
                        if (filter_user_id and sender != filter_user_id)
                        else None
                    )
                    if await self._save_message(
                        db, msg, channel_id, channel_name,
                        is_standup=is_standup, is_reply=False,
                        user_id=override_uid,
                    ):
                        saved += 1

            # Fetch thread replies for any message that has them
            if msg.get("reply_count", 0) > 0:
                async for reply in self.iter_thread_replies(channel_id, ts, oldest_ts):
                    if reply.get("subtype", "") in {"channel_join", "channel_leave"}:
                        continue
                    reply_subtype = reply.get("subtype", "")
                    if reply_subtype == "bot_message" and is_standup:
                        bot_username = (
                            reply.get("username")
                            or reply.get("user_profile", {}).get("display_name", "")
                        ).strip()
                        if bot_username:
                            if target_names is not None and bot_username.lower() not in target_names:
                                resolved_uid = None
                            else:
                                resolved_uid = await self._resolve_user_by_name(db, bot_username)
                            uid_matches = (
                                resolved_uid is not None
                                and (filter_user_id is None or resolved_uid == filter_user_id)
                            )
                            if uid_matches:
                                if await self._save_message(
                                    db, reply, channel_id, channel_name,
                                    is_standup=True, is_reply=True,
                                    user_id=resolved_uid,
                                ):
                                    saved += 1
                    elif not reply_subtype or reply_subtype not in {"bot_message"}:
                        if self._is_relevant(reply, filter_user_id):
                            # Attribute to filter_user_id if relevant only due to mention
                            reply_sender = reply.get("user", "")
                            reply_override = (
                                filter_user_id
                                if (filter_user_id and reply_sender != filter_user_id)
                                else None
                            )
                            if await self._save_message(
                                db, reply, channel_id, channel_name,
                                is_standup=is_standup, is_reply=True,
                                user_id=reply_override,
                            ):
                                saved += 1

        await db.flush()
        logger.info(
            "Backfilled %d messages from #%s (unresolved bot names: %s)",
            saved, channel_name, unresolved_bot_names or "none",
        )
        return saved, unresolved_bot_names

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
