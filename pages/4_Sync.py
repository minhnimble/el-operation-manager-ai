"""
Sync Data page.

Syncs run directly in the Streamlit session (no Celery/Redis required).
Progress is shown per-channel (Slack) and per-repo (GitHub) by breaking
each sync into small asyncio.run() steps so the UI can update between them.

Slack sync uses the EM's own token and captures messages from every author
in every joined channel — no member selector needed for Slack.

GitHub sync targets a specific team member and resolves credentials in order:
member OAuth token first, then manager OAuth token + manually-set github_login.
"""

import asyncio
from datetime import date, time as dt_time
import threading
import time
import streamlit as st

from app.streamlit_env import load_streamlit_secrets_into_env

load_streamlit_secrets_into_env()

from datetime import datetime, timedelta
from sqlalchemy import select, delete, func

from app.database import AsyncSessionLocal
from app.models.team_member import TeamMember
from app.models.user import User, UserGitHubLink
from app.models.slack_token import SlackUserToken
from app.models.raw_data import SlackMessage
from app.models.work_unit import WorkUnit
from app.ingestion.slack_ingester import SlackIngester
from app.ingestion.github_ingester import GitHubIngester
from app.normalization.normalizer import normalize_slack_messages, normalize_github_activities

st.set_page_config(page_title="Sync Data", page_icon="🔄", layout="wide")
from app.ui.page_utils import inject_page_load_bar
from app.ui.session_cookie import restore_session_from_cookie
inject_page_load_bar()
restore_session_from_cookie()

# ── Channel ignore list ────────────────────────────────────────────────────────

_IGNORED_CHANNEL_EXACT = {
    "access-requests",
    "vn-community",
    "cat-place",
    "hardware-and-machinery",
    "badminton",
    "ios-rss",
    "watercooler",
}
_IGNORED_CHANNEL_SUFFIXES = ("-activity", "-corner")
_IGNORED_CHANNEL_PREFIXES = ("ic-", "nimble")

# ── Channels always included when syncing a team member ───────────────────────
# These are synced regardless of whether the member is technically a Slack
# member — standup bots post on their behalf without them joining the channel.

_ALWAYS_INCLUDE_CHANNELS = {"daily-standup"}


# ── Process-wide sync generation counters ─────────────────────────────────────
# Daemon threads in Python cannot be killed externally — and on Streamlit Cloud
# they survive code redeploys until the dyno restarts. Each new sync bumps the
# matching counter; in-flight daemon threads see the mismatch on their next
# cancel_check and exit immediately, so a stale "Sync Slack Messages" thread
# can never write into the DB after a fresh "Sync Daily Standup" starts.
#
# These are MODULE-LEVEL globals so they persist across Streamlit reruns inside
# the same Python process.

_slack_sync_gen: int = 0
_github_sync_gen: int = 0
_sync_gen_lock = threading.Lock()


def _bump_slack_gen() -> int:
    global _slack_sync_gen
    with _sync_gen_lock:
        _slack_sync_gen += 1
        return _slack_sync_gen


def _bump_github_gen() -> int:
    global _github_sync_gen
    with _sync_gen_lock:
        _github_sync_gen += 1
        return _github_sync_gen


def _slack_gen_alive(my_gen: int) -> bool:
    return _slack_sync_gen == my_gen


def _github_gen_alive(my_gen: int) -> bool:
    return _github_sync_gen == my_gen


def _should_skip_channel(name: str) -> bool:
    n = name.lower()
    return (
        n in _IGNORED_CHANNEL_EXACT
        or n.endswith(_IGNORED_CHANNEL_SUFFIXES)
        or n.startswith(_IGNORED_CHANNEL_PREFIXES)
    )


def run(coro):
    return asyncio.run(coro)


# ── Team selector ─────────────────────────────────────────────────────────────

async def _get_team_options(manager_user_id: str, manager_team_id: str, self_name: str) -> dict[str, str]:
    options: dict[str, str] = {f"{self_name} (me)": manager_user_id}
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TeamMember).where(
                TeamMember.manager_slack_user_id == manager_user_id,
                TeamMember.manager_slack_team_id == manager_team_id,
            ).order_by(TeamMember.member_display_name)
        )
        for m in result.scalars().all():
            options[m.display()] = m.member_slack_user_id
    return options


async def _get_github_links_batch(
    slack_user_ids: list[str], slack_team_id: str,
) -> dict[str, tuple[bool, str, bool]]:
    """Batch-fetch GitHub link info for multiple users in two queries.

    Returns {slack_user_id: (can_sync, github_login, uses_own_token)}.

    With PAT-only auth, `uses_own_token` is always False — the server-wide
    `GITHUB_PAT` env/secret is always used. `can_sync` is True iff a
    github_login is mapped (either via UserGitHubLink or TeamMember).
    """
    async with AsyncSessionLocal() as db:
        # ── 1. UserGitHubLink rows (login-only mapping) ──────────────────────
        link_result = await db.execute(
            select(
                UserGitHubLink.slack_user_id,
                UserGitHubLink.github_login,
            ).where(
                UserGitHubLink.slack_user_id.in_(slack_user_ids),
                UserGitHubLink.slack_team_id == slack_team_id,
            )
        )
        links: dict[str, tuple[bool, str, bool]] = {}
        for row in link_result.all():
            has_login = bool(row.github_login)
            links[row.slack_user_id] = (
                has_login,                # can_sync
                row.github_login or "",
                False,                    # uses_own_token (always False now)
            )

        # ── 2. Manually-set handles in TeamMember (for users not in OAuth table)
        missing = [uid for uid in slack_user_ids if uid not in links]
        if missing:
            tm_result = await db.execute(
                select(TeamMember.member_slack_user_id, TeamMember.github_login).where(
                    TeamMember.member_slack_user_id.in_(missing),
                    TeamMember.manager_slack_team_id == slack_team_id,
                    TeamMember.github_login.isnot(None),
                    TeamMember.github_login != "",
                )
            )
            for row in tm_result.all():
                links[row.member_slack_user_id] = (
                    True,               # can_sync (manager's token will be used)
                    row.github_login,
                    False,              # uses_own_token
                )

    # Fill in users with no GitHub info at all
    return {uid: links.get(uid, (False, "", False)) for uid in slack_user_ids}


def _load_sync_page_data(
    manager_user_id: str, manager_team_id: str, self_name: str,
) -> tuple[dict[str, str], dict[str, tuple[bool, str]]]:
    """Load team options + GitHub link info. Cached in session state to avoid
    re-querying the DB on every widget interaction (multiselect change, button
    click, date slider adjustment, etc.)."""
    cache = st.session_state.get("_sync_page_cache")
    if (
        cache
        and cache["user_id"] == manager_user_id
        and cache["team_id"] == manager_team_id
    ):
        return cache["team_options"], cache["gh_links"]

    team_options = run(_get_team_options(manager_user_id, manager_team_id, self_name))
    all_uids = list(team_options.values())
    gh_links = run(_get_github_links_batch(all_uids, manager_team_id))
    # Map by display name for easy lookup later
    gh_links_by_name = {
        name: gh_links.get(uid, (False, ""))
        for name, uid in team_options.items()
    }

    st.session_state["_sync_page_cache"] = {
        "user_id": manager_user_id,
        "team_id": manager_team_id,
        "team_options": team_options,
        "gh_links": gh_links_by_name,
    }
    return team_options, gh_links_by_name


def _invalidate_sync_cache():
    st.session_state.pop("_sync_page_cache", None)


# ── Slack helpers (one asyncio.run() per step) ────────────────────────────────

async def _get_slack_token(slack_user_id: str, team_id: str) -> str:
    """Returns the raw access token string, or raises."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SlackUserToken).where(
                SlackUserToken.slack_user_id == slack_user_id,
                SlackUserToken.slack_team_id == team_id,
            )
        )
        record = result.scalar_one_or_none()
        if not record:
            raise RuntimeError(
                "No Slack token found. Please reconnect your Slack account on the Connect Accounts page."
            )
        return record.access_token


async def _get_slack_channels(access_token: str, team_id: str) -> tuple[list[dict], list[str]]:
    ingester = SlackIngester(user_token=access_token, team_id=team_id)
    try:
        return await ingester.get_joined_channels()
    finally:
        await ingester.close()


async def _filter_channels_by_member(
    access_token: str, team_id: str, channels: list[dict], user_id: str
) -> list[dict]:
    """Return only channels that user_id is a member of.

    Channels in _ALWAYS_INCLUDE_CHANNELS are passed through unconditionally —
    standup bots post on behalf of users who may never have joined the channel.
    """
    ingester = SlackIngester(user_token=access_token, team_id=team_id)
    try:
        result = []
        for ch in channels:
            if ch.get("name", "").lower() in _ALWAYS_INCLUDE_CHANNELS:
                result.append(ch)  # always include — bot posts on user's behalf
            elif await ingester.is_member(ch["id"], user_id):
                result.append(ch)
        return result
    finally:
        await ingester.close()


async def _sync_slack_channel(
    access_token: str,
    team_id: str,
    slack_user_id: str,
    channel_id: str,
    channel_name: str,
    oldest: datetime,
    latest: datetime | None = None,
    filter_user_id: str | None = None,
    cancel_check: "callable | None" = None,
) -> tuple[int, str | None, list[str]]:
    """Sync one channel. Returns (messages_saved, error_or_None, unresolved_bot_names).

    oldest / latest   — time bounds; latest=None means up to the present.
    filter_user_id    — when set, only messages from or mentioning this user are saved.
    unresolved_bot_names — standup bot usernames that couldn't be matched to any user.
    cancel_check      — callable returning True to abort mid-channel.
    """
    from app.ingestion.slack_ingester import CancelledError as _SlackCancelled

    async with AsyncSessionLocal() as db:
        ingester = SlackIngester(user_token=access_token, team_id=team_id)
        try:
            result = await ingester.backfill_channel(
                db=db,
                channel_id=channel_id,
                channel_name=channel_name,
                slack_user_id=slack_user_id,
                oldest=oldest,
                latest=latest,
                filter_user_id=filter_user_id,
                cancel_check=cancel_check,
            )
            # backfill_channel returns (count, unresolved_names); guard against
            # any cached old version that returned just int.
            if isinstance(result, tuple):
                count, unresolved = result
            else:
                count, unresolved = int(result), []
            await db.commit()
            return count, None, unresolved
        except _SlackCancelled:
            # User-requested stop: commit any flushed work and return cleanly.
            try:
                await db.commit()
            except Exception:
                await db.rollback()
            return 0, None, []
        except Exception as e:
            await db.rollback()
            return 0, str(e), []
        finally:
            await ingester.close()


async def _get_standup_channels(access_token: str, team_id: str) -> list[dict]:
    """Return channel dicts for every channel in _ALWAYS_INCLUDE_CHANNELS.

    Bypasses the full channel list — fetches only the named channels directly.
    """
    ingester = SlackIngester(user_token=access_token, team_id=team_id)
    try:
        return await ingester.find_channels_by_names(_ALWAYS_INCLUDE_CHANNELS)
    finally:
        await ingester.close()


async def _normalize_slack(team_id: str) -> int:
    async with AsyncSessionLocal() as db:
        count = await normalize_slack_messages(db, team_id=team_id)
        await db.commit()
        return count


async def _get_valid_channel_ids(
    access_token: str, team_id: str, target_user_id: str
) -> tuple[list[str], list[str]]:
    """Return (valid_channel_ids, valid_channel_names) for target_user_id.

    Applies the same ignore list and member-filter used during sync.
    """
    ingester = SlackIngester(user_token=access_token, team_id=team_id)
    try:
        all_channels, _ = await ingester.get_joined_channels()
        channels = [
            ch for ch in all_channels
            if not _should_skip_channel(ch.get("name", ""))
        ]
        ids: list[str] = []
        names: list[str] = []
        for ch in channels:
            if ch.get("name", "").lower() in _ALWAYS_INCLUDE_CHANNELS or \
                    await ingester.is_member(ch["id"], target_user_id):
                ids.append(ch["id"])
                names.append(ch.get("name", ch["id"]))
        return ids, names
    finally:
        await ingester.close()


async def _count_stale_slack_data(
    target_user_id: str, team_id: str, valid_channel_ids: list[str]
) -> tuple[int, int]:
    """Return (slack_message_count, work_unit_count) outside valid channels."""
    async with AsyncSessionLocal() as db:
        msg_count = await db.scalar(
            select(func.count()).select_from(SlackMessage).where(
                SlackMessage.slack_user_id == target_user_id,
                SlackMessage.slack_team_id == team_id,
                SlackMessage.channel_id.not_in(valid_channel_ids) if valid_channel_ids
                else SlackMessage.slack_team_id == team_id,  # safety: keep all if list empty
            )
        )
        wu_count = await db.scalar(
            select(func.count()).select_from(WorkUnit).where(
                WorkUnit.slack_user_id == target_user_id,
                WorkUnit.slack_team_id == team_id,
                WorkUnit.slack_channel_id.is_not(None),
                WorkUnit.slack_channel_id.not_in(valid_channel_ids) if valid_channel_ids
                else WorkUnit.slack_team_id == team_id,
            )
        )
    return msg_count or 0, wu_count or 0


async def _delete_stale_slack_data(
    target_user_id: str, team_id: str, valid_channel_ids: list[str]
) -> tuple[int, int]:
    """Delete SlackMessages and WorkUnits outside valid channels.

    Returns (deleted_messages, deleted_work_units).
    Aborts and returns (0, 0) if valid_channel_ids is empty (safety guard).
    """
    if not valid_channel_ids:
        return 0, 0

    async with AsyncSessionLocal() as db:
        # Remove WorkUnits first (they reference SlackMessage via slack_message_ts)
        wu_result = await db.execute(
            delete(WorkUnit).where(
                WorkUnit.slack_user_id == target_user_id,
                WorkUnit.slack_team_id == team_id,
                WorkUnit.slack_channel_id.is_not(None),
                WorkUnit.slack_channel_id.not_in(valid_channel_ids),
            )
        )
        # Remove the raw SlackMessage rows
        msg_result = await db.execute(
            delete(SlackMessage).where(
                SlackMessage.slack_user_id == target_user_id,
                SlackMessage.slack_team_id == team_id,
                SlackMessage.channel_id.not_in(valid_channel_ids),
            )
        )
        await db.commit()
    return msg_result.rowcount, wu_result.rowcount


# ── DB-level ignored-channel cleanup (no Slack API required) ──────────────────

async def _preview_ignored_channel_cleanup(
    team_id: str,
) -> tuple[int, int, list[tuple[str, str, int]]]:
    """Scan the DB for messages in channels that match the current ignore list.

    Returns (total_msg_count, total_wu_count, [(channel_id, channel_name, msg_count)]).
    Purely DB-driven — no Slack API calls.
    """
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(
                SlackMessage.channel_id,
                SlackMessage.channel_name,
                func.count(SlackMessage.id).label("cnt"),
            )
            .where(SlackMessage.slack_team_id == team_id)
            .group_by(SlackMessage.channel_id, SlackMessage.channel_name)
        )
        all_rows = rows.all()

        ignored: list[tuple[str, str, int]] = [
            (row.channel_id, row.channel_name or row.channel_id, row.cnt)
            for row in all_rows
            if row.channel_name and _should_skip_channel(row.channel_name)
        ]

        if not ignored:
            return 0, 0, []

        ignored_ids = [r[0] for r in ignored]
        total_msgs  = sum(r[2] for r in ignored)

        wu_count = await db.scalar(
            select(func.count()).select_from(WorkUnit).where(
                WorkUnit.slack_team_id == team_id,
                WorkUnit.slack_channel_id.in_(ignored_ids),
            )
        ) or 0

    return total_msgs, wu_count, ignored


async def _delete_ignored_channel_data(
    team_id: str, channel_ids: list[str]
) -> tuple[int, int]:
    """Delete ALL SlackMessages + WorkUnits for the given channel IDs, all users.

    Returns (deleted_messages, deleted_work_units).
    """
    if not channel_ids:
        return 0, 0

    async with AsyncSessionLocal() as db:
        wu_result = await db.execute(
            delete(WorkUnit).where(
                WorkUnit.slack_team_id == team_id,
                WorkUnit.slack_channel_id.in_(channel_ids),
            )
        )
        msg_result = await db.execute(
            delete(SlackMessage).where(
                SlackMessage.slack_team_id == team_id,
                SlackMessage.channel_id.in_(channel_ids),
            )
        )
        await db.commit()
    return msg_result.rowcount, wu_result.rowcount


# ── GitHub helpers (one asyncio.run() per step) ───────────────────────────────

async def _get_github_credentials(
    slack_user_id: str,
    slack_team_id: str,
    manager_user_id: str | None = None,
) -> tuple[str, str]:
    """Returns (access_token, github_login), or raises.

    GitHub auth is now PAT-only via the `GITHUB_PAT` env/secret.
    The DB stores only the slack→github_login mapping (no tokens).

    `manager_user_id` is unused; kept for call-site compatibility.
    """
    from app.config import get_settings as _gs
    _server_pat = (_gs().github_pat or "").strip()
    if not _server_pat:
        raise RuntimeError(
            "GITHUB_PAT is not configured. "
            "Set it in env / Streamlit secrets (scopes: repo + read:org)."
        )

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserGitHubLink.github_login).where(
                UserGitHubLink.slack_user_id == slack_user_id,
                UserGitHubLink.slack_team_id == slack_team_id,
            )
        )
        member_login = result.scalar_one_or_none()

        if not member_login:
            tm_result = await db.execute(
                select(TeamMember.github_login).where(
                    TeamMember.member_slack_user_id == slack_user_id,
                    TeamMember.manager_slack_team_id == slack_team_id,
                    TeamMember.github_login.isnot(None),
                    TeamMember.github_login != "",
                )
            )
            member_login = tm_result.scalar_one_or_none()

        if not member_login:
            raise RuntimeError(
                f"No GitHub login mapped for Slack user {slack_user_id}. "
                "Set the github_login on the Connect or Team Overview page."
            )

        return _server_pat, member_login


async def _get_github_link_info(slack_user_id: str, slack_team_id: str) -> tuple[bool, str, bool]:
    """Returns (can_sync, github_login, uses_own_token).

    PAT-only auth: `uses_own_token` is always False. `can_sync` is True iff
    a github_login mapping exists for the user.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserGitHubLink.github_login).where(
                UserGitHubLink.slack_user_id == slack_user_id,
                UserGitHubLink.slack_team_id == slack_team_id,
            )
        )
        login = result.scalar_one_or_none()
        if login:
            return True, login, False

        tm_result = await db.execute(
            select(TeamMember.github_login).where(
                TeamMember.member_slack_user_id == slack_user_id,
                TeamMember.manager_slack_team_id == slack_team_id,
                TeamMember.github_login.isnot(None),
                TeamMember.github_login != "",
            )
        )
        member_login = tm_result.scalar_one_or_none()
        if member_login:
            return True, member_login, False
        return False, "", False


async def _get_github_repos(access_token: str, github_login: str) -> list[dict]:
    ingester = GitHubIngester(access_token=access_token, github_login=github_login)
    try:
        return await ingester.get_repos()
    finally:
        await ingester.close()


async def _sync_github_repo(
    access_token: str,
    github_login: str,
    slack_team_id: str,
    slack_user_id: str,
    repo: dict,
    since: datetime,
) -> dict[str, int]:
    """Sync one repo. Returns counts dict."""
    async with AsyncSessionLocal() as db:
        ingester = GitHubIngester(access_token=access_token, github_login=github_login)
        try:
            counts = await ingester.ingest_single_repo(
                db=db,
                slack_team_id=slack_team_id,
                slack_user_id=slack_user_id,
                repo=repo,
                since=since,
            )
            await db.commit()
            return counts
        finally:
            await ingester.close()


async def _sync_github_via_search(
    access_token: str,
    github_login: str,
    slack_team_id: str,
    slack_user_id: str,
    since: datetime,
    until: datetime | None = None,
) -> dict[str, int]:
    """Overview-mode sync — Search API, cross-org, no repo loop."""
    async with AsyncSessionLocal() as db:
        ingester = GitHubIngester(access_token=access_token, github_login=github_login)
        try:
            counts = await ingester.ingest_via_search(
                db=db,
                slack_team_id=slack_team_id,
                slack_user_id=slack_user_id,
                since=since,
                until=until,
            )
            await db.commit()
            return counts
        finally:
            await ingester.close()


async def _normalize_github(team_id: str) -> int:
    async with AsyncSessionLocal() as db:
        count = await normalize_github_activities(db, team_id=team_id)
        await db.commit()
        return count


# ── Background sync job helpers ───────────────────────────────────────────────

def _make_job(label: str) -> dict:
    return {
        "label": label,
        "running": True,
        "stop_requested": False,  # set by UI Stop button; checked by bg thread
        "log": [],               # list of (level, msg); level: "info"|"warn"|"error"|"ok"
        "progress": 0,
        "progress_text": "Starting…",
        "member_statuses": [],   # list of {name, status, detail}
        "summary": "",
    }


def _jlog(job: dict, msg: str, level: str = "info") -> None:
    """Thread-safe log append (CPython list.append is atomic under the GIL)."""
    job["log"].append((level, msg))


def _run_slack_sync_bg(
    job: dict,
    access_token: str,
    slack_user_id: str,
    slack_team_id: str,
    target_users: list,
    oldest: "datetime",
    latest: "datetime | None",
    slack_sync_mode: str,
    my_gen: int,
) -> None:
    """Runs in a daemon thread — updates `job` dict with live progress.

    *my_gen* is the generation counter snapshot from when this thread was
    started. If a newer Slack sync starts (which bumps `_slack_sync_gen`),
    this thread's cancel hook flips True and we abort cleanly — preventing
    an old normal-mode thread from continuing to write to the DB after a
    new standup-mode sync begins.
    """

    def _run(coro):
        return asyncio.run(coro)

    # Hard allowlist for standup mode: even if any rogue channel sneaks into
    # `channels`, we refuse to touch it. Belt-and-suspenders against the
    # exact bug reported (non-standup channels being synced in standup mode).
    allowed_names: set[str] | None = (
        _ALWAYS_INCLUDE_CHANNELS if slack_sync_mode == "standup" else None
    )

    # Combined cancel: per-job stop button OR a newer sync superseding us.
    def _cancel() -> bool:
        return job.get("stop_requested", False) or not _slack_gen_alive(my_gen)

    try:
        grand_msgs = 0
        all_errors: list[str] = []

        base_channels: list[dict] = []
        if slack_sync_mode == "normal":
            _jlog(job, "📋 Loading joined channels…")
            try:
                all_channels, warnings = _run(_get_slack_channels(access_token, slack_team_id))
                for w in warnings:
                    _jlog(job, f"⚠️ {w}", "warn")
                base_channels = [
                    ch for ch in all_channels
                    if not _should_skip_channel(ch.get("name", ""))
                    and ch.get("name", "").lower() not in _ALWAYS_INCLUDE_CHANNELS
                ]
                _jlog(job, f"→ {len(base_channels)} channel(s) to sync.")
            except Exception as e:
                _jlog(job, f"❌ Failed to load channels: {e}", "error")
                job["summary"] = f"❌ Failed: {e}"
                return

        total_members = len(target_users)
        stopped = False
        for idx, (member_name, target_user_id) in enumerate(target_users):
            if _cancel():
                if not _slack_gen_alive(my_gen):
                    _jlog(job, "\n🛑 Superseded by a newer sync — exiting.", "warn")
                else:
                    _jlog(job, "\n🛑 Stop requested — aborting remaining members.", "warn")
                stopped = True
                break

            is_self_m = target_user_id == slack_user_id
            job["progress"] = int(idx / total_members * 94)
            job["progress_text"] = f"Member {idx + 1}/{total_members}: {member_name}…"

            ms: dict = {"name": member_name, "status": "⏳", "detail": ""}
            job["member_statuses"].append(ms)
            _jlog(job, f"\n👤 **{member_name}**")

            total_msgs = 0
            member_errors: list[str] = []

            try:
                if slack_sync_mode == "standup":
                    _jlog(job, "  📋 Looking up standup channel(s)…")
                    channels = _run(_get_standup_channels(access_token, slack_team_id))
                    _jlog(job, f"  → {len(channels)} channel(s).")
                else:
                    if is_self_m:
                        channels = base_channels
                        _jlog(job, f"  Using {len(channels)} channel(s).")
                    else:
                        _jlog(job, f"  Filtering channels for {member_name}…")
                        channels = _run(_filter_channels_by_member(
                            access_token, slack_team_id, base_channels, target_user_id
                        ))
                        _jlog(job, f"  → {len(channels)} channel(s).")

                # Hard guard: in standup mode, drop anything that isn't on the
                # allowlist. Defends against API quirks or stale code paths.
                if allowed_names is not None:
                    before = len(channels)
                    channels = [
                        c for c in channels
                        if c.get("name", "").lower() in allowed_names
                    ]
                    if len(channels) != before:
                        _jlog(
                            job,
                            f"  ⛔ Dropped {before - len(channels)} non-standup "
                            f"channel(s) — standup mode allowlist enforced.",
                            "warn",
                        )

                n_ch = max(len(channels), 1)
                for ch_idx, ch in enumerate(channels):
                    if _cancel():
                        if not _slack_gen_alive(my_gen):
                            _jlog(job, "  🛑 Superseded by a newer sync — exiting.", "warn")
                        else:
                            _jlog(job, "  🛑 Stop requested — skipping remaining channels.", "warn")
                        stopped = True
                        break

                    # Defense in depth: re-check the allowlist on each channel
                    # so an in-flight loop can't slip a non-standup channel
                    # through even if the list above was somehow mutated.
                    if allowed_names is not None and ch.get("name", "").lower() not in allowed_names:
                        _jlog(job, f"  ⛔ Skipping #{ch.get('name')} — not in standup allowlist.", "warn")
                        continue

                    ch_id   = ch["id"]
                    ch_name = ch.get("name", ch_id)
                    # Per-channel progress: interpolate within this member's slice
                    frac = (idx + ch_idx / n_ch) / total_members
                    job["progress"] = int(frac * 94)
                    job["progress_text"] = (
                        f"[{idx + 1}/{total_members}] {member_name} · "
                        f"#{ch_name} ({ch_idx + 1}/{len(channels)})"
                    )
                    _jlog(job, f"  📥 #{ch_name}")
                    # In standup mode, always pass the target user as the filter — even
                    # for self — so backfill_channel can use its `target_names`
                    # optimization and skip resolving every other team member's bot
                    # username (chuu, tung nguyen, vo minh don, …) inside the channel.
                    if slack_sync_mode == "standup":
                        mf = target_user_id
                    else:
                        mf = None if is_self_m else target_user_id
                    count, err, unresolved = _run(_sync_slack_channel(
                        access_token, slack_team_id, slack_user_id,
                        ch_id, ch_name, oldest, latest=latest,
                        filter_user_id=mf,
                        cancel_check=_cancel,
                    ))
                    if err:
                        member_errors.append(f"#{ch_name}: {err}")
                        _jlog(job, f"    ⚠️ {err}", "warn")
                    else:
                        total_msgs += count
                        if count:
                            _jlog(job, f"    ✓ {count} new message(s)")
                        if unresolved:
                            _jlog(job, f"    ⚠️ Unmatched: {', '.join(unresolved)}", "warn")

                grand_msgs += total_msgs
                all_errors.extend(member_errors)
                if stopped:
                    ms["status"] = "🛑"
                    ms["detail"] = f"{total_msgs} msgs (stopped)"
                else:
                    ms["status"] = "✅" if not member_errors else "⚠️"
                    ms["detail"] = f"{total_msgs} msgs" + (f" · {len(member_errors)} err" if member_errors else "")

            except Exception as e:
                member_errors.append(str(e))
                ms["status"] = "❌"
                ms["detail"] = str(e)[:60]
                _jlog(job, f"  ❌ {e}", "error")

        job["progress"] = 96
        job["progress_text"] = "Normalizing work units…"
        _jlog(job, "\n🔄 Normalizing work units…")
        normalized = _run(_normalize_slack(slack_team_id))
        _jlog(job, f"  ✓ {normalized} work unit(s)")

        job["progress"] = 100
        job["progress_text"] = "Done ✓" if not stopped else "Stopped ■"
        mode_label = "normal messages" if slack_sync_mode == "normal" else "daily standup"
        stop_note = " (stopped early by user)" if stopped else ""
        job["summary"] = (
            f"{'⚠️' if stopped else '✅'} Slack sync {'stopped' if stopped else 'complete'} ({mode_label}){stop_note} — "
            f"**{grand_msgs}** new message(s), **{normalized}** work unit(s) "
            f"across **{len(target_users)}** member(s)"
            + (f" · {len(all_errors)} error(s)" if all_errors else "")
        )
        _jlog(job, job["summary"], "warn" if stopped else "ok")

    except Exception as e:
        job["summary"] = f"❌ Slack sync failed: {e}"
        _jlog(job, job["summary"], "error")
    finally:
        job["running"] = False


def _run_slack_sync_both_bg(
    job: dict,
    access_token: str,
    slack_user_id: str,
    slack_team_id: str,
    target_users: list,
    oldest: "datetime",
    latest: "datetime | None",
    my_gen: int,
) -> None:
    """Run normal + standup Slack sync back-to-back under a single job."""
    phase_summaries: list[str] = []
    for phase_idx, mode in enumerate(("normal", "standup"), start=1):
        # Reset per-phase state while preserving cumulative log.
        job["running"] = True
        job["progress"] = 0
        job["progress_text"] = f"Phase {phase_idx}/2 · {mode}…"
        job["member_statuses"] = []
        job["summary"] = ""
        job["_mode"] = mode
        _jlog(job, f"\n━━━ Phase {phase_idx}/2 · {mode} ━━━", "info")

        _run_slack_sync_bg(
            job, access_token, slack_user_id, slack_team_id,
            target_users, oldest, latest, mode, my_gen,
        )
        phase_summaries.append(job.get("summary", ""))

        # Bail out early if the user stopped or a newer sync superseded us.
        if job.get("stop_requested") or not _slack_gen_alive(my_gen):
            break

    job["progress"] = 100
    job["progress_text"] = "Done ✓"
    job["summary"] = "  \n".join(s for s in phase_summaries if s)
    job["running"] = False


def _run_github_sync_bg(
    job: dict,
    slack_team_id: str,
    members_with_gh: list,
    gh_info: dict,
    since: "datetime",
    my_gen: int,
    manager_user_id: str | None = None,
    *,
    use_overview_mode: bool = False,
    until: "datetime | None" = None,
) -> None:
    """Runs in a daemon thread — updates `job` dict with live progress.

    *my_gen* is the GitHub-sync generation snapshot. A newer GitHub sync
    bumps the counter and supersedes any in-flight thread, ensuring stale
    daemon threads can't keep writing after a restart.
    """

    def _run(coro):
        return asyncio.run(coro)

    def _cancel() -> bool:
        return job.get("stop_requested", False) or not _github_gen_alive(my_gen)

    try:
        grand: dict[str, int] = {"commits": 0, "prs": 0, "reviews": 0, "issues": 0}

        total_members = len(members_with_gh)
        stopped = False
        for idx, (member_name, target_user_id) in enumerate(members_with_gh):
            if _cancel():
                if not _github_gen_alive(my_gen):
                    _jlog(job, "\n🛑 Superseded by a newer GitHub sync — exiting.", "warn")
                else:
                    _jlog(job, "\n🛑 Stop requested — aborting remaining members.", "warn")
                stopped = True
                break

            gh_login_d = gh_info[member_name][1]
            job["progress"] = int(idx / total_members * 94)
            job["progress_text"] = f"Member {idx + 1}/{total_members}: {member_name}…"

            ms: dict = {"name": member_name, "status": "⏳", "detail": ""}
            job["member_statuses"].append(ms)
            _jlog(job, f"\n👤 **{member_name}** (@{gh_login_d})")

            try:
                gh_token, github_login = _run(_get_github_credentials(
                    target_user_id, slack_team_id, manager_user_id=manager_user_id,
                ))

                # ── Overview / Search-API mode ────────────────────────────
                if use_overview_mode:
                    _jlog(
                        job,
                        f"  🔍 Overview mode: search PRs authored/reviewed by "
                        f"@{github_login} across all orgs…",
                    )
                    job["progress"] = int((idx + 0.5) / total_members * 94)
                    counts = _run(_sync_github_via_search(
                        gh_token, github_login, slack_team_id,
                        target_user_id, since, until,
                    ))
                    added = sum(counts.values())
                    if added:
                        parts = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
                        _jlog(job, f"    ✓ {parts}")
                    else:
                        _jlog(job, "    ✓ nothing new")
                    for k, v in counts.items():
                        grand[k] = grand.get(k, 0) + v
                    member_summary = ", ".join(f"{v} {k}" for k, v in counts.items() if v) or "nothing new"
                    ms["status"] = "✅"
                    ms["detail"] = member_summary
                    _jlog(job, f"  → {member_summary}")
                    continue

                _jlog(job, f"  📋 Loading repos for @{github_login}…")
                repos = _run(_get_github_repos(gh_token, github_login))
                _jlog(job, f"  Found {len(repos)} repo(s).")

                tc: dict[str, int] = {"commits": 0, "prs": 0, "reviews": 0, "issues": 0}
                n_repos = max(len(repos), 1)
                for r_idx, repo in enumerate(repos):
                    if _cancel():
                        if not _github_gen_alive(my_gen):
                            _jlog(job, "  🛑 Superseded by a newer GitHub sync — exiting.", "warn")
                        else:
                            _jlog(job, "  🛑 Stop requested — skipping remaining repos.", "warn")
                        stopped = True
                        break

                    repo_name = repo["full_name"]
                    # Per-repo progress: interpolate within this member's slice
                    frac = (idx + r_idx / n_repos) / total_members
                    job["progress"] = int(frac * 94)
                    job["progress_text"] = (
                        f"[{idx + 1}/{total_members}] {member_name} · "
                        f"{repo_name} ({r_idx + 1}/{len(repos)})"
                    )
                    _jlog(job, f"  📦 {repo_name}")
                    try:
                        counts = _run(_sync_github_repo(
                            gh_token, github_login, slack_team_id,
                            target_user_id, repo, since,
                        ))
                        added = sum(counts.values())
                        if added:
                            parts = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
                            _jlog(job, f"    ✓ {parts}")
                        for k, v in counts.items():
                            tc[k]     = tc.get(k, 0) + v
                            grand[k]  = grand.get(k, 0) + v
                    except Exception as e:
                        _jlog(job, f"    ⚠️ {e}", "warn")

                member_summary = ", ".join(f"{v} {k}" for k, v in tc.items() if v) or "nothing new"
                if stopped:
                    ms["status"] = "🛑"
                    ms["detail"] = f"{member_summary} (stopped)"
                else:
                    ms["status"] = "✅"
                    ms["detail"] = member_summary
                _jlog(job, f"  → {member_summary}")

            except Exception as e:
                ms["status"] = "❌"
                ms["detail"] = str(e)[:60]
                _jlog(job, f"  ❌ {e}", "error")

        job["progress"] = 96
        job["progress_text"] = "Normalizing…"
        _jlog(job, "\n🔄 Normalizing GitHub activity…")
        normalized = _run(_normalize_github(slack_team_id))
        _jlog(job, f"  ✓ {normalized} work unit(s)")

        job["progress"] = 100
        job["progress_text"] = "Done ✓" if not stopped else "Stopped ■"
        grand_summary = ", ".join(f"{v} {k}" for k, v in grand.items() if v) or "nothing new"
        stop_note = " (stopped early by user)" if stopped else ""
        job["summary"] = (
            f"{'⚠️' if stopped else '✅'} GitHub sync {'stopped' if stopped else 'complete'}{stop_note} — "
            f"{grand_summary}, **{normalized}** work unit(s) "
            f"across **{len(members_with_gh)}** member(s)"
        )
        _jlog(job, job["summary"], "warn" if stopped else "ok")

    except Exception as e:
        job["summary"] = f"❌ GitHub sync failed: {e}"
        _jlog(job, job["summary"], "error")
    finally:
        job["running"] = False


def _render_job_ui(job: dict, state_key: str) -> None:
    """Render live/completed job progress. Auto-reruns the page while running."""
    st.progress(job["progress"], text=job["progress_text"])

    # Member status chips
    statuses = job["member_statuses"]
    if statuses:
        cols = st.columns(min(len(statuses), 5))
        for i, ms in enumerate(statuses):
            cols[i % 5].caption(f"{ms['status']} **{ms['name']}**  \n{ms['detail']}")

    # Scrollable log — use a text_area so it doesn't explode into hundreds of st.write rows
    log_lines = [msg for _, msg in job["log"]]
    st.text_area(
        "Log",
        value="\n".join(log_lines),
        height=280,
        disabled=True,
        label_visibility="collapsed",
        key=f"_log_area_{state_key}_{len(log_lines)}",
    )

    if job["running"]:
        # Show Stop button while sync is in progress
        if job.get("stop_requested"):
            st.warning("⏳ Stopping after the current channel/repo finishes…")
            # Force-abandon: detach the job from the UI.  The orphan daemon
            # thread will finish on its own but the user can start a new sync.
            if st.button("⏹ Force stop (abandon)", key=f"_force_stop_{state_key}", type="primary"):
                job["running"] = False
                job["summary"] = "🛑 Force stopped — orphan thread may still be running in the background."
                st.session_state.pop(state_key, None)
                st.rerun()
        elif st.button("⏹ Stop sync", key=f"_stop_{state_key}", type="secondary"):
            job["stop_requested"] = True
    else:
        summary = job.get("summary", "")
        if "✅" in summary:
            st.success(summary)
        elif summary:
            st.warning(summary) if "⚠️" in summary else st.error(summary)
        if st.button("Clear", key=f"_clear_{state_key}"):
            st.session_state.pop(state_key, None)
            st.rerun()


# Fragments re-render only themselves — no full-page rerun, so no dim flicker.
# `run_every` polls the fragment at a fixed cadence; when no job (or job done),
# the body returns early so polling is essentially free.
@st.fragment(run_every="1.5s")
def _live_slack_job_fragment() -> None:
    job = st.session_state.get("_slack_sync_job")
    if not job:
        return
    with st.container(border=True):
        _render_job_ui(job, "_slack_sync_job")


@st.fragment(run_every="1.5s")
def _live_github_job_fragment() -> None:
    job = st.session_state.get("_github_sync_job")
    if not job:
        return
    with st.container(border=True):
        _render_job_ui(job, "_github_sync_job")


# ── Page ──────────────────────────────────────────────────────────────────────

st.title("🔄 Sync Data")
st.caption("Pull the latest Slack messages and GitHub activity into the database.")
st.markdown("---")

# Pin widget state so it survives navigation away from this page — Streamlit
# drops keys for widgets that aren't currently rendered, which resets the
# filters when the user switches tabs. Defaults are seeded here so the widgets
# can omit `value=`/`index=` (Streamlit rejects passing both a default and a
# pre-set session_state value).
_sync_filter_defaults = {
    "date_mode":         "Last N days",
    "days_slider":       90,
    "days_input":        90,
    "date_range_input":  (date.today() - timedelta(days=90), date.today()),
}
for _k, _v in _sync_filter_defaults.items():
    st.session_state.setdefault(_k, _v)

for _persist_key in ("sync_member_select", *_sync_filter_defaults.keys()):
    if _persist_key in st.session_state:
        st.session_state[_persist_key] = st.session_state[_persist_key]

slack_user_id = st.session_state.get("slack_user_id")
slack_team_id = st.session_state.get("slack_team_id")

if not slack_user_id:
    st.warning("Please connect your Slack account first on the **Connect Accounts** page.")
    st.page_link("pages/1_Connect.py", label="Go to Connect Accounts")
    st.stop()

st.markdown("---")

# ─── Team member selector ─────────────────────────────────────────────────────

from app.ui.page_utils import loading_section

self_name = st.session_state.get("slack_display_name", slack_user_id)
# Only show loading skeleton on a cache miss (first visit or after refresh)
_sync_cache_hit = bool(
    st.session_state.get("_sync_page_cache")
    and st.session_state["_sync_page_cache"].get("user_id") == slack_user_id
)
if _sync_cache_hit:
    team_options, _gh_links_by_name = _load_sync_page_data(slack_user_id, slack_team_id, self_name)
else:
    with loading_section("Loading team and GitHub links…", n_skeleton_lines=4):
        team_options, _gh_links_by_name = _load_sync_page_data(slack_user_id, slack_team_id, self_name)
all_member_names = list(team_options.keys())

# Initialise to just "myself" on first load
if "sync_member_select" not in st.session_state:
    st.session_state["sync_member_select"] = [all_member_names[0]]

_self_label = all_member_names[0]  # always the "(me)" entry
_team_only_names = [n for n in all_member_names if n != _self_label]

_sel_col1, _sel_col2, _sel_col3, _sel_col4, _sel_col5 = st.columns([5, 1, 1, 1, 1])
with _sel_col2:
    if st.button("All", use_container_width=True, help="Select everyone including yourself"):
        st.session_state["sync_member_select"] = all_member_names
        st.rerun()
with _sel_col3:
    if st.button("Team", use_container_width=True, help="Select all team members (exclude yourself)"):
        st.session_state["sync_member_select"] = _team_only_names
        st.rerun()
with _sel_col4:
    if st.button("Clear", use_container_width=True, help="Clear selection"):
        st.session_state["sync_member_select"] = []
        st.rerun()
with _sel_col5:
    if st.button("🔄", use_container_width=True, help="Refresh team list from database"):
        _invalidate_sync_cache()
        st.rerun()
with _sel_col1:
    selected_names: list[str] = st.multiselect(
        "Sync for",
        options=all_member_names,
        key="sync_member_select",
        help="Select one or more members. Slack sync uses your token for all members. "
             "GitHub sync skips members who haven't connected their GitHub account.",
    )

if not selected_names:
    st.info("Select at least one team member above to sync.")
    st.stop()

target_users: list[tuple[str, str]] = [(name, team_options[name]) for name in selected_names]
is_batch = len(selected_names) > 1

st.markdown("---")

# ─── Date range selector (shared for Slack + GitHub) ─────────────────────────

st.subheader("Date range")

_date_mode = st.radio(
    "date_mode",
    ["Last N days", "Custom range"],
    horizontal=True,
    label_visibility="collapsed",
    key="date_mode",
)

if _date_mode == "Last N days":
    # Keep the slider and number input in sync: whichever the user changes
    # last drives the other. The input accepts up to 3650 days while the
    # slider maxes at 365, so we clamp on the slider side to stay valid.
    def _sync_days_from_slider() -> None:
        st.session_state["days_input"] = st.session_state["days_slider"]

    def _sync_days_from_input() -> None:
        v = int(st.session_state["days_input"])
        st.session_state["days_slider"] = min(max(v, 1), 365)

    _d_col1, _d_col2 = st.columns([3, 1])
    _input_days = int(st.session_state["days_input"])
    with _d_col1:
        st.slider(
            "Days to backfill",
            min_value=1, max_value=365,
            key="days_slider",
            on_change=_sync_days_from_slider,
        )
        # When the input exceeds the slider's max, rewrite the two numeric
        # labels Streamlit renders inside this slider:
        #   • thumb value (top)  → the actual entered days (e.g. "500")
        #   • max tick   (bottom) → "365+"
        # Streamlit's `format` param only affects the thumb value, and it
        # can't diverge from the clamped slider value, so we override both
        # via scoped CSS on the `st-key-days_slider` wrapper.
        if _input_days > 365:
            st.markdown(
                f"""
                <style>
                .st-key-days_slider [data-testid="stThumbValue"] {{
                    visibility: hidden; position: relative;
                }}
                .st-key-days_slider [data-testid="stThumbValue"]::after {{
                    content: "{_input_days}";
                    visibility: visible;
                    position: absolute;
                    right: 0; top: 0;
                }}
                .st-key-days_slider [data-testid="stTickBarMax"] {{
                    visibility: hidden; position: relative;
                }}
                .st-key-days_slider [data-testid="stTickBarMax"]::after {{
                    content: "365+";
                    visibility: visible;
                    position: absolute;
                    right: 0; top: 0;
                }}
                </style>
                """,
                unsafe_allow_html=True,
            )
    with _d_col2:
        st.number_input(
            "Or enter days", min_value=1, max_value=3650,
            key="days_input", label_visibility="visible",
            on_change=_sync_days_from_input,
        )
    # `days_input` is the canonical source — it covers the full 1–3650 range.
    _days = int(st.session_state["days_input"])
    sync_start: datetime = datetime.now(tz=None) - timedelta(days=_days)
    sync_end: datetime | None = None
    st.caption(f"From **{sync_start.strftime('%b %d, %Y')}** to **now**.")
else:
    _today = date.today()
    _range = st.date_input(
        "Select date range",
        max_value=_today,
        key="date_range_input",
    )
    # date_input returns a tuple when a range is selected, a single date otherwise
    if isinstance(_range, (list, tuple)) and len(_range) == 2:
        _start_d, _end_d = _range[0], _range[1]
    elif isinstance(_range, (list, tuple)) and len(_range) == 1:
        _start_d = _end_d = _range[0]
    else:
        _start_d = _end_d = _range  # type: ignore[assignment]

    sync_start = datetime.combine(_start_d, dt_time.min)
    sync_end   = datetime.combine(_end_d,   dt_time(23, 59, 59))
    st.caption(
        f"From **{_start_d.strftime('%b %d, %Y')}** to **{_end_d.strftime('%b %d, %Y')}**."
    )

st.markdown("---")

# ─── Slack Sync ───────────────────────────────────────────────────────────────

st.subheader("Slack")

if is_batch:
    st.caption(
        f"Will sync Slack messages for **{len(selected_names)} members** one by one, "
        "using **your** Slack token for all of them."
    )
elif team_options[selected_names[0]] == slack_user_id:
    st.caption(
        "Pulls messages from all public channels you are a member of. "
        "Messages from **all team members** in those channels are captured automatically."
    )
else:
    st.caption(
        f"Slack sync uses **your** token and filters messages attributed to "
        f"**{selected_names[0]}**. No separate token needed."
    )

_slack_btn_col1, _slack_btn_col2, _slack_btn_col3 = st.columns(3)
with _slack_btn_col1:
    sync_normal_clicked = st.button(
        "Sync Slack Messages", type="primary", key="sync_slack_normal",
        help="Sync all channels except daily-standup.",
    )
with _slack_btn_col2:
    sync_standup_clicked = st.button(
        "Sync Daily Standup", type="primary", key="sync_slack_standup",
        help="Sync only the daily-standup channel.",
    )
with _slack_btn_col3:
    sync_both_clicked = st.button(
        "Sync Both", type="primary", key="sync_slack_both",
        help="Run Sync Slack Messages and Sync Daily Standup back-to-back.",
    )

if sync_normal_clicked or sync_standup_clicked or sync_both_clicked:
    if sync_both_clicked:
        slack_sync_mode = "both"
    else:
        slack_sync_mode = "normal" if sync_normal_clicked else "standup"
    # Fetch token in main thread (fast DB lookup — needed before thread starts)
    try:
        access_token = run(_get_slack_token(slack_user_id, slack_team_id))
    except Exception as e:
        st.error(str(e))
        st.stop()
    # Bump the generation counter — any older Slack daemon thread (including
    # ones started before today's redeploy that ignore stop_requested) will
    # see the mismatch on its next cancel-check and exit. New threads only.
    my_gen = _bump_slack_gen()
    job = _make_job("Slack")
    job["_gen"] = my_gen
    job["_mode"] = slack_sync_mode
    st.session_state["_slack_sync_job"] = job
    if slack_sync_mode == "both":
        threading.Thread(
            target=_run_slack_sync_both_bg,
            args=(job, access_token, slack_user_id, slack_team_id,
                  target_users, sync_start, sync_end, my_gen),
            daemon=True,
        ).start()
    else:
        threading.Thread(
            target=_run_slack_sync_bg,
            args=(job, access_token, slack_user_id, slack_team_id,
                  target_users, sync_start, sync_end, slack_sync_mode, my_gen),
            daemon=True,
        ).start()
    st.rerun()

# Show live / last-run progress for Slack (persists across page navigations).
# The fragment auto-refreshes itself every 1.5s without re-running the whole page,
# so the rest of the UI no longer flickers / dims while a sync is in flight.
_live_slack_job_fragment()

st.markdown("---")

# ─── GitHub Sync ──────────────────────────────────────────────────────────────

st.subheader("GitHub")

# Check GitHub connectivity for every selected member (from cache — no DB call).
# gh_info value: (can_sync, github_login, uses_own_token)
gh_info: dict[str, tuple[bool, str, bool]] = {
    name: _gh_links_by_name.get(name, (False, "", False)) for name, _uid in target_users
}

# Check if the manager (logged-in user) has a GitHub token for proxy use.
# Look up by display name in the full team map — not via target_users, since
# the manager may not be in the selected sync set.
_manager_has_gh_token = _gh_links_by_name.get(
    f"{self_name} (me)", (False, "", False)
)[2]

members_with_gh = [(n, u) for n, u in target_users if gh_info[n][0]]
members_no_gh   = [(n, u) for n, u in target_users if not gh_info[n][0]]

if members_no_gh:
    skipped_str = ", ".join(f"**{n}**" for n, _ in members_no_gh)
    if members_with_gh:
        st.warning(f"⚠️ No GitHub handle set — will skip: {skipped_str}")
    else:
        st.warning(f"None of the selected members have a GitHub handle set: {skipped_str}")
        st.caption("Set each member's GitHub handle in **Team Overview** to enable sync using your token.")
        st.button("Sync GitHub", type="primary", disabled=True)

if members_with_gh:
    # Show which token will be used for each member
    _sync_labels = []
    for n, _ in members_with_gh:
        login = gh_info[n][1]
        own_token = gh_info[n][2]
        if own_token:
            _sync_labels.append(f"**{n}** (@{login} — own token)")
        else:
            _sync_labels.append(f"**{n}** (@{login} — via your token)")
    st.caption("Will sync: " + ", ".join(_sync_labels))

    if not _manager_has_gh_token and any(not gh_info[n][2] for n, _ in members_with_gh):
        _proxy_members = ", ".join(
            f"**{n}**" for n, _ in members_with_gh if not gh_info[n][2]
        )
        st.warning(
            f"⚠️ {_proxy_members} haven't connected their own GitHub account, "
            "so syncing their data requires falling back to your GitHub token — "
            "but you haven't connected yours either. "
            "Either ask them to connect their GitHub, or go to **Connect Accounts** "
            "to link a GitHub account with access to their activity."
        )

    _use_overview = st.toggle(
        "🔭 Overview mode (Search API, cross-org, faster)",
        value=True,
        key="_gh_overview_mode",
        help=(
            "Mirrors the GitHub user overview page "
            "(github.com/<user>?tab=overview&from=...&to=...). "
            "Uses the Search API with your PAT to find PRs the member "
            "**created** and **reviewed** across **all organizations** they "
            "belong to — even ones you don't list as repos. Skips commits "
            "(commit-search is heavily rate-limited). "
            "Disable to fall back to per-repo iteration."
        ),
    )

    if st.button("Sync GitHub", type="primary"):
        # Bump the GitHub gen counter — supersedes any older daemon thread.
        my_gh_gen = _bump_github_gen()
        job = _make_job("GitHub")
        job["_gen"] = my_gh_gen
        st.session_state["_github_sync_job"] = job
        threading.Thread(
            target=_run_github_sync_bg,
            args=(job, slack_team_id, members_with_gh, gh_info, sync_start,
                  my_gh_gen, slack_user_id),
            kwargs={
                "use_overview_mode": _use_overview,
                "until": sync_end,
            },
            daemon=True,
        ).start()
        st.rerun()

    # Fragment auto-refresh — avoids full-page rerun / dim flicker.
    _live_github_job_fragment()

st.markdown("---")

# ─── Database Cleanup ─────────────────────────────────────────────────────────
# Completely independent of member selection and date range.

st.subheader("🗑️ Database Cleanup")
st.caption(
    "Remove data that should never have been stored. "
    "These operations apply to **all team members** and ignore the selectors above."
)

_tab_ignored, _tab_member = st.tabs(["Remove ignored channels", "Remove stale member data"])

# ── Tab 1: ignored channels (pure DB scan — no Slack API) ─────────────────────
with _tab_ignored:
    st.caption(
        "Scans the database for messages stored in channels that match the current "
        "ignore list (suffixes: `-activity`, `-corner`; prefixes: `ic-`, `nimble`; "
        "exact names: `watercooler`, `badminton`, etc.). "
        "Deletes them for **every user** in one shot — no Slack API calls needed."
    )

    if st.button("Scan for ignored-channel data", key="scan_ignored"):
        st.session_state.pop("_ignored_preview", None)
        st.session_state.pop("_ignored_confirm", None)
        with st.spinner("Scanning database…"):
            total_msgs, total_wus, ignored_channels = run(
                _preview_ignored_channel_cleanup(slack_team_id)
            )
        st.session_state["_ignored_preview"] = (total_msgs, total_wus, ignored_channels)

    if "_ignored_preview" in st.session_state:
        total_msgs, total_wus, ignored_channels = st.session_state["_ignored_preview"]

        if not ignored_channels:
            st.success("✅ No ignored-channel data found — database is clean.")
        else:
            # Show a breakdown table
            rows_display = [
                {"Channel": f"#{name}", "Messages": cnt}
                for _, name, cnt in sorted(ignored_channels, key=lambda r: -r[2])
            ]
            st.warning(
                f"Found **{len(ignored_channels)}** ignored channel(s) with "
                f"**{total_msgs}** message(s) and **{total_wus}** work unit(s)."
            )
            st.dataframe(rows_display, use_container_width=True, hide_index=True)

            if not st.session_state.get("_ignored_confirm"):
                if st.button(
                    f"⚠️ Delete all {total_msgs} messages + {total_wus} work units",
                    type="primary",
                    key="confirm_ignored_delete",
                ):
                    st.session_state["_ignored_confirm"] = True
                    st.rerun()
            else:
                try:
                    channel_ids = [r[0] for r in ignored_channels]
                    del_msgs, del_wus = run(
                        _delete_ignored_channel_data(slack_team_id, channel_ids)
                    )
                    st.success(
                        f"🗑️ Deleted **{del_msgs}** message(s) and **{del_wus}** work unit(s) "
                        f"from {len(channel_ids)} ignored channel(s)."
                    )
                except Exception as e:
                    st.error(f"Deletion failed: {e}")
                finally:
                    for k in ("_ignored_preview", "_ignored_confirm"):
                        st.session_state.pop(k, None)

# ── Tab 2: per-member stale membership cleanup (uses Slack API) ───────────────
with _tab_member:
    st.caption(
        "Removes messages synced for a specific member that belong to channels "
        "they are no longer (or never were) a member of. "
        "Uses the Slack API to determine current membership."
    )

    _member_names_clean = list(team_options.keys())
    _clean_selected = st.selectbox(
        "Member to clean up",
        options=_member_names_clean,
        key="cleanup_member_select",
    )
    _clean_user_id = team_options[_clean_selected]

    _ck_preview = f"_cleanup_preview_{_clean_user_id}"
    _ck_valid   = f"_cleanup_valid_{_clean_user_id}"
    _ck_confirm = f"_cleanup_confirm_{_clean_user_id}"

    if st.button("Preview stale data", key=f"preview_cleanup_{_clean_user_id}"):
        st.session_state.pop(_ck_confirm, None)
        try:
            access_token_cleanup = run(_get_slack_token(slack_user_id, slack_team_id))
            with st.spinner("Checking Slack membership…"):
                valid_ids, valid_names = run(
                    _get_valid_channel_ids(access_token_cleanup, slack_team_id, _clean_user_id)
                )
                msg_c, wu_c = run(
                    _count_stale_slack_data(_clean_user_id, slack_team_id, valid_ids)
                )
            st.session_state[_ck_valid]   = valid_ids
            st.session_state[_ck_preview] = (msg_c, wu_c, valid_names)
        except Exception as e:
            st.error(f"Could not load channels: {e}")

    if _ck_preview in st.session_state:
        msg_c, wu_c, valid_names = st.session_state[_ck_preview]
        valid_ids = st.session_state.get(_ck_valid, [])

        ch_list  = ", ".join(f"#{n}" for n in valid_names[:10])
        overflow = f" … and {len(valid_names) - 10} more" if len(valid_names) > 10 else ""
        st.info(
            f"**{_clean_selected}** is currently in **{len(valid_names)}** channel(s): "
            f"{ch_list}{overflow}"
        )

        if msg_c == 0 and wu_c == 0:
            st.success("✅ No stale data found — everything looks clean.")
        else:
            st.warning(
                f"Found **{msg_c}** message(s) and **{wu_c}** work unit(s) "
                "outside those channels."
            )
            if not st.session_state.get(_ck_confirm):
                if st.button(
                    f"⚠️ Delete {msg_c} messages + {wu_c} work units",
                    type="primary",
                    key=f"confirm_cleanup_{_clean_user_id}",
                ):
                    st.session_state[_ck_confirm] = True
                    st.rerun()
            else:
                try:
                    del_msgs, del_wus = run(
                        _delete_stale_slack_data(_clean_user_id, slack_team_id, valid_ids)
                    )
                    st.success(
                        f"🗑️ Deleted **{del_msgs}** message(s) and "
                        f"**{del_wus}** work unit(s) from stale channels."
                    )
                except Exception as e:
                    st.error(f"Deletion failed: {e}")
                finally:
                    for k in (_ck_preview, _ck_valid, _ck_confirm):
                        st.session_state.pop(k, None)

st.markdown("---")

st.caption(
    "💡 For automated daily syncs, run `make worker` and `make beat` locally "
    "or set up a Celery worker in your deployment."
)
