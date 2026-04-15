"""
Sync Data page.

Syncs run directly in the Streamlit session (no Celery/Redis required).
Progress is shown per-channel (Slack) and per-repo (GitHub) by breaking
each sync into small asyncio.run() steps so the UI can update between them.

Slack sync uses the EM's own token and captures messages from every author
in every joined channel — no member selector needed for Slack.

GitHub sync targets a specific team member: the selected user must have
connected their own GitHub account via OAuth for the sync to work.
"""

import asyncio
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
    filter_user_id: str | None = None,
) -> tuple[int, str | None, list[str]]:
    """Sync one channel. Returns (messages_saved, error_or_None, unresolved_bot_names).

    filter_user_id — when set, only messages from or mentioning this user are saved.
    unresolved_bot_names — standup bot usernames that couldn't be matched to any user.
    """
    async with AsyncSessionLocal() as db:
        ingester = SlackIngester(user_token=access_token, team_id=team_id)
        try:
            result = await ingester.backfill_channel(
                db=db,
                channel_id=channel_id,
                channel_name=channel_name,
                slack_user_id=slack_user_id,
                oldest=oldest,
                filter_user_id=filter_user_id,
            )
            # backfill_channel returns (count, unresolved_names); guard against
            # any cached old version that returned just int.
            if isinstance(result, tuple):
                count, unresolved = result
            else:
                count, unresolved = int(result), []
            await db.commit()
            return count, None, unresolved
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


# ── GitHub helpers (one asyncio.run() per step) ───────────────────────────────

async def _get_github_credentials(slack_user_id: str, slack_team_id: str) -> tuple[str, str]:
    """Returns (access_token, github_login), or raises."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserGitHubLink).where(
                UserGitHubLink.slack_user_id == slack_user_id,
                UserGitHubLink.slack_team_id == slack_team_id,
            )
        )
        link = result.scalar_one_or_none()
        if not link or not link.github_access_token:
            raise RuntimeError(
                "No GitHub OAuth token found for this user. "
                "They need to connect their GitHub account on the Connect Accounts page."
            )
        return link.github_access_token, link.github_login


async def _get_github_link_info(slack_user_id: str, slack_team_id: str) -> tuple[bool, str]:
    """Returns (has_token, github_login) for display purposes."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserGitHubLink).where(
                UserGitHubLink.slack_user_id == slack_user_id,
                UserGitHubLink.slack_team_id == slack_team_id,
            )
        )
        link = result.scalar_one_or_none()
        if not link or not link.github_access_token:
            return False, ""
        return True, link.github_login or ""


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


async def _normalize_github(team_id: str) -> int:
    async with AsyncSessionLocal() as db:
        count = await normalize_github_activities(db, team_id=team_id)
        await db.commit()
        return count


# ── Page ──────────────────────────────────────────────────────────────────────

st.title("🔄 Sync Data")
st.caption("Pull the latest Slack messages and GitHub activity into the database.")
st.markdown("---")

slack_user_id = st.session_state.get("slack_user_id")
slack_team_id = st.session_state.get("slack_team_id")

if not slack_user_id:
    st.warning("Please connect your Slack account first on the **Connect Accounts** page.")
    st.page_link("pages/1_Connect.py", label="Go to Connect Accounts")
    st.stop()

st.markdown("---")

# ─── Team member selector ─────────────────────────────────────────────────────

self_name = st.session_state.get("slack_display_name", slack_user_id)
team_options = run(_get_team_options(slack_user_id, slack_team_id, self_name))
all_member_names = list(team_options.keys())

# Initialise to just "myself" on first load
if "sync_member_select" not in st.session_state:
    st.session_state["sync_member_select"] = [all_member_names[0]]

_self_label = all_member_names[0]  # always the "(me)" entry
_team_only_names = [n for n in all_member_names if n != _self_label]

_sel_col1, _sel_col2, _sel_col3, _sel_col4 = st.columns([5, 1, 1, 1])
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

_slack_col1, _slack_col2 = st.columns([3, 1])
with _slack_col1:
    days_slack = st.slider("Days to backfill", min_value=1, max_value=365, value=7, key="slack_days")
with _slack_col2:
    days_slack = st.number_input(
        "Or enter days", min_value=1, max_value=3650, value=days_slack,
        key="slack_days_input", label_visibility="visible",
    )

_slack_btn_col1, _slack_btn_col2 = st.columns(2)
with _slack_btn_col1:
    sync_normal_clicked = st.button(
        "Sync Slack Messages", type="primary", key="sync_slack_normal",
        help="Sync all channels except daily-standup.",
    )
with _slack_btn_col2:
    sync_standup_clicked = st.button(
        "Sync Daily Standup", type="secondary", key="sync_slack_standup",
        help="Sync only the daily-standup channel.",
    )

if sync_normal_clicked or sync_standup_clicked:
    slack_sync_mode = "normal" if sync_normal_clicked else "standup"
    oldest = datetime.utcnow() - timedelta(days=days_slack)
    grand_total_msgs = 0
    all_slack_errors: list[str] = []

    overall_slack_progress = st.progress(
        0, text=f"Starting Slack {slack_sync_mode} sync for {len(target_users)} member(s)…"
    )

    # ── Fetch token once ──────────────────────────────────────────────────────
    try:
        access_token = run(_get_slack_token(slack_user_id, slack_team_id))
    except Exception as e:
        overall_slack_progress.empty()
        st.error(str(e))
        st.stop()

    # ── For normal sync: discover channels once, reuse per member ─────────────
    base_channels: list[dict] = []
    if slack_sync_mode == "normal":
        try:
            with st.spinner("Loading joined channels…"):
                all_channels, ch_warnings = run(_get_slack_channels(access_token, slack_team_id))
            for w in ch_warnings:
                st.warning(w)
            base_channels = [
                ch for ch in all_channels
                if not _should_skip_channel(ch.get("name", ""))
                and ch.get("name", "").lower() not in _ALWAYS_INCLUDE_CHANNELS
            ]
            total_ch  = len(all_channels)
            standup_ch = sum(
                1 for c in all_channels
                if c.get("name", "").lower() in _ALWAYS_INCLUDE_CHANNELS
            )
            skipped_ch = total_ch - len(base_channels) - standup_ch
            st.caption(
                f"Found **{total_ch}** joined channel(s) → "
                f"**{len(base_channels)}** to sync"
                + (f" ({skipped_ch} ignored)" if skipped_ch else "") + "."
            )
        except Exception as e:
            overall_slack_progress.empty()
            st.error(str(e))
            st.stop()

    # ── Per-member sync loop ───────────────────────────────────────────────────
    for member_idx, (member_name, target_user_id) in enumerate(target_users):
        is_self_member = target_user_id == slack_user_id
        overall_slack_progress.progress(
            int(member_idx / len(target_users) * 95),
            text=f"Member {member_idx + 1}/{len(target_users)}: {member_name}…",
        )

        total_msgs   = 0
        member_errors: list[str] = []

        with st.status(
            f"{'👤 ' if is_batch else ''}{member_name}",
            expanded=(member_idx == 0),
        ) as member_status:
            try:
                if slack_sync_mode == "standup":
                    st.write("📋 Looking up standup channel(s)…")
                    channels = run(_get_standup_channels(access_token, slack_team_id))
                    if channels:
                        st.write("  → " + ", ".join(f"#**{c.get('name', c['id'])}**" for c in channels))
                    else:
                        st.warning("No standup channels found.")
                else:
                    if is_self_member:
                        channels = base_channels
                        st.write(f"Using all **{len(channels)}** channel(s).")
                    else:
                        st.write(f"Filtering channels for **{member_name}**…")
                        channels = run(
                            _filter_channels_by_member(
                                access_token, slack_team_id, base_channels, target_user_id
                            )
                        )
                        st.write(f"  → **{len(channels)}** channel(s).")

                n = max(len(channels), 1)
                ch_progress = st.progress(0, text="Starting…")

                for i, ch in enumerate(channels):
                    ch_id   = ch["id"]
                    ch_name = ch.get("name", ch_id)
                    ch_progress.progress(int(i / n * 90), text=f"#{ch_name} ({i + 1}/{n})")
                    st.write(f"📥 #{ch_name}")

                    member_filter = None if is_self_member else target_user_id
                    count, err, unresolved = run(
                        _sync_slack_channel(
                            access_token, slack_team_id, slack_user_id,
                            ch_id, ch_name, oldest,
                            filter_user_id=member_filter,
                        )
                    )
                    if err:
                        member_errors.append(f"#{ch_name}: {err}")
                        st.write(f"  ⚠️ {err}")
                    else:
                        total_msgs += count
                        if count:
                            st.write(f"  ✓ {count} new message(s)")
                        if unresolved:
                            st.write(
                                "  ⚠️ Unmatched standup names: "
                                + ", ".join(f"**{u}**" for u in unresolved)
                            )

                ch_progress.progress(100, text="Done ✓")

            except Exception as e:
                member_errors.append(str(e))
                st.error(str(e))

            grand_total_msgs  += total_msgs
            all_slack_errors.extend(member_errors)

            status_label = (
                f"{'✅' if not member_errors else '⚠️'} {member_name} — {total_msgs} message(s)"
                + (f" · {len(member_errors)} error(s)" if member_errors else "")
            )
            member_status.update(
                label=status_label,
                state="complete" if not member_errors else "error",
            )

    # ── Normalize once across the whole team ──────────────────────────────────
    overall_slack_progress.progress(97, text="Normalizing work units…")
    normalized_slack = run(_normalize_slack(slack_team_id))
    overall_slack_progress.progress(100, text="Done ✓")

    mode_label = "normal messages" if slack_sync_mode == "normal" else "daily standup"
    st.success(
        f"✅ Slack sync complete ({mode_label}) — "
        f"**{grand_total_msgs}** new message(s), **{normalized_slack}** work unit(s) "
        f"across **{len(target_users)}** member(s)"
        + (f" · {len(all_slack_errors)} error(s)" if all_slack_errors else "")
    )
    if all_slack_errors:
        with st.expander(f"{len(all_slack_errors)} error(s)"):
            for err in all_slack_errors:
                st.warning(err)

# ─── Clean up stale channel data (single member only) ─────────────────────────

if not is_batch:
    single_name, single_user_id = target_users[0]
    with st.expander("🗑️ Clean up stale channel data", expanded=False):
        st.caption(
            "Removes **SlackMessages** and **WorkUnits** that were synced for "
            f"**{single_name}** but belong to channels they are no longer (or never were) "
            "a member of. Run this after adjusting the ignore list or team membership."
        )

        _ck_preview = f"_cleanup_preview_{single_user_id}"
        _ck_valid   = f"_cleanup_valid_{single_user_id}"
        _ck_confirm = f"_cleanup_confirm_{single_user_id}"

        if st.button("Preview stale data", key=f"preview_cleanup_{single_user_id}"):
            st.session_state.pop(_ck_confirm, None)
            try:
                access_token_cleanup = run(_get_slack_token(slack_user_id, slack_team_id))
                with st.spinner("Checking which channels are valid for this user…"):
                    valid_ids, valid_names = run(
                        _get_valid_channel_ids(access_token_cleanup, slack_team_id, single_user_id)
                    )
                    msg_c, wu_c = run(
                        _count_stale_slack_data(single_user_id, slack_team_id, valid_ids)
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
                f"**{single_name}** is a member of **{len(valid_names)}** synced channel(s): "
                f"{ch_list}{overflow}"
            )

            if msg_c == 0 and wu_c == 0:
                st.success("✅ No stale data found — everything looks clean.")
            else:
                st.warning(
                    f"Found **{msg_c}** SlackMessage(s) and **{wu_c}** WorkUnit(s) "
                    "outside those channels."
                )
                if not st.session_state.get(_ck_confirm):
                    if st.button(
                        f"⚠️ Delete {msg_c} messages + {wu_c} work units",
                        type="primary",
                        key=f"confirm_cleanup_{single_user_id}",
                    ):
                        st.session_state[_ck_confirm] = True
                        st.rerun()
                else:
                    try:
                        del_msgs, del_wus = run(
                            _delete_stale_slack_data(single_user_id, slack_team_id, valid_ids)
                        )
                        st.success(
                            f"🗑️ Deleted **{del_msgs}** SlackMessage(s) and "
                            f"**{del_wus}** WorkUnit(s) from stale channels."
                        )
                    except Exception as e:
                        st.error(f"Deletion failed: {e}")
                    finally:
                        for k in (_ck_preview, _ck_valid, _ck_confirm):
                            st.session_state.pop(k, None)

st.markdown("---")

# ─── GitHub Sync ──────────────────────────────────────────────────────────────

st.subheader("GitHub")

# Check GitHub connectivity for every selected member
gh_info: dict[str, tuple[bool, str]] = {}
for _name, _uid in target_users:
    _has_tok, _login = run(_get_github_link_info(_uid, slack_team_id))
    gh_info[_name] = (_has_tok, _login)

members_with_gh = [(n, u) for n, u in target_users if gh_info[n][0]]
members_no_gh   = [(n, u) for n, u in target_users if not gh_info[n][0]]

if members_no_gh:
    skipped_str = ", ".join(f"**{n}**" for n, _ in members_no_gh)
    if members_with_gh:
        st.warning(f"⚠️ No GitHub token — will skip: {skipped_str}")
    else:
        st.warning(f"None of the selected members have connected GitHub: {skipped_str}")
        if any(u == slack_user_id for _, u in target_users):
            st.page_link("pages/1_Connect.py", label="Connect GitHub on the Connect Accounts page")
        st.button("Sync GitHub", type="primary", disabled=True)

if members_with_gh:
    gh_list = ", ".join(f"**{n}** (@{gh_info[n][1]})" for n, _ in members_with_gh)
    st.caption(f"Will sync: {gh_list}")

    _gh_col1, _gh_col2 = st.columns([3, 1])
    with _gh_col1:
        days_github = st.slider("Days to backfill", min_value=1, max_value=365, value=7, key="github_days")
    with _gh_col2:
        days_github = st.number_input(
            "Or enter days", min_value=1, max_value=3650, value=days_github,
            key="github_days_input", label_visibility="visible",
        )

    if st.button("Sync GitHub", type="primary"):
        since = datetime.utcnow() - timedelta(days=days_github)
        grand_gh_counts: dict[str, int] = {"commits": 0, "prs": 0, "reviews": 0, "issues": 0}

        overall_gh_progress = st.progress(
            0, text=f"Starting GitHub sync for {len(members_with_gh)} member(s)…"
        )

        for member_idx, (member_name, target_user_id) in enumerate(members_with_gh):
            gh_login_display = gh_info[member_name][1]
            overall_gh_progress.progress(
                int(member_idx / len(members_with_gh) * 95),
                text=f"Member {member_idx + 1}/{len(members_with_gh)}: {member_name}…",
            )

            with st.status(
                f"{'👤 ' if is_batch else ''}{member_name} (@{gh_login_display})",
                expanded=(member_idx == 0),
            ) as member_gh_status:
                try:
                    st.write("🔑 Fetching credentials…")
                    gh_token, github_login = run(_get_github_credentials(target_user_id, slack_team_id))
                    st.write(f"📋 Loading repos for @{github_login}…")
                    repos = run(_get_github_repos(gh_token, github_login))
                    st.write(f"Found **{len(repos)}** repo(s).")

                    total_counts: dict[str, int] = {"commits": 0, "prs": 0, "reviews": 0, "issues": 0}
                    n = max(len(repos), 1)
                    repo_progress = st.progress(0, text="Scanning repos…")

                    for i, repo in enumerate(repos):
                        repo_name = repo["full_name"]
                        repo_progress.progress(int(i / n * 100), text=f"{repo_name} ({i + 1}/{n})")
                        st.write(f"📦 {repo_name}")
                        try:
                            counts = run(
                                _sync_github_repo(
                                    gh_token, github_login, slack_team_id,
                                    target_user_id, repo, since,
                                )
                            )
                            added = sum(counts.values())
                            if added:
                                parts = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
                                st.write(f"  ✓ {parts}")
                            for k, v in counts.items():
                                total_counts[k]  = total_counts.get(k, 0) + v
                                grand_gh_counts[k] = grand_gh_counts.get(k, 0) + v
                        except Exception as e:
                            st.write(f"  ⚠️ {e}")

                    repo_progress.progress(100, text="Done ✓")
                    member_summary = (
                        ", ".join(f"{v} {k}" for k, v in total_counts.items() if v)
                        or "nothing new"
                    )
                    member_gh_status.update(
                        label=f"✅ {member_name} — {member_summary}", state="complete"
                    )

                except Exception as e:
                    member_gh_status.update(label=f"❌ {member_name} — failed", state="error")
                    st.error(str(e))

        overall_gh_progress.progress(97, text="Normalizing work units…")
        normalized_gh = run(_normalize_github(slack_team_id))
        overall_gh_progress.progress(100, text="Done ✓")

        grand_summary = (
            ", ".join(f"{v} {k}" for k, v in grand_gh_counts.items() if v) or "nothing new"
        )
        st.success(
            f"✅ GitHub sync complete — {grand_summary}, **{normalized_gh}** work unit(s) "
            f"across **{len(members_with_gh)}** member(s)"
        )

st.markdown("---")

st.caption(
    "💡 For automated daily syncs, run `make worker` and `make beat` locally "
    "or set up a Celery worker in your deployment."
)
