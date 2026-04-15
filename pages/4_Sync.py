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
import os
import streamlit as st

for _key, _val in st.secrets.items():
    if isinstance(_val, str):
        os.environ.setdefault(_key.upper(), _val)

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
}
_IGNORED_CHANNEL_SUFFIXES = ("-activity", "-corner")
_IGNORED_CHANNEL_PREFIXES = ("ic-", "nimble")


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
    """Return only channels that user_id is a member of."""
    ingester = SlackIngester(user_token=access_token, team_id=team_id)
    try:
        result = []
        for ch in channels:
            if await ingester.is_member(ch["id"], user_id):
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
) -> tuple[int, str | None]:
    """Sync one channel. Returns (messages_saved, error_or_None).

    filter_user_id — when set, only messages from or mentioning this user are saved.
    """
    async with AsyncSessionLocal() as db:
        ingester = SlackIngester(user_token=access_token, team_id=team_id)
        try:
            count = await ingester.backfill_channel(
                db=db,
                channel_id=channel_id,
                channel_name=channel_name,
                slack_user_id=slack_user_id,
                oldest=oldest,
                filter_user_id=filter_user_id,
            )
            await db.commit()
            return count, None
        except Exception as e:
            await db.rollback()
            return 0, str(e)
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
            if await ingester.is_member(ch["id"], target_user_id):
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

selected_name = st.selectbox(
    "Sync for",
    options=list(team_options.keys()),
    help="Slack sync always captures all team members. GitHub sync targets this member specifically.",
)
target_user_id = team_options[selected_name]
is_self = target_user_id == slack_user_id

st.markdown("---")

# ─── Slack Sync ───────────────────────────────────────────────────────────────

st.subheader("Slack")

if is_self:
    st.caption(
        "Pulls messages from all public channels you are a member of. "
        "Messages from **all team members** in those channels are captured automatically."
    )
else:
    st.caption(
        f"Slack sync always uses **your** token and captures messages from every user in your channels — "
        f"including **{selected_name}**. No separate token needed."
    )

days_slack = st.slider("Days to backfill", min_value=1, max_value=90, value=7, key="slack_days")

if st.button("Sync Slack", type="primary"):
    oldest = datetime.utcnow() - timedelta(days=days_slack)
    total_msgs = 0
    errors: list[str] = []

    # Progress bar lives OUTSIDE st.status so it stays visible while the
    # status log is expanded or collapsed.
    slack_progress = st.progress(0, text="Connecting to Slack…")
    slack_status_text = st.empty()

    with st.status("Sync log", expanded=True) as status:
        # Step 1 — fetch token + channel list
        try:
            st.write("🔑 Fetching Slack token…")
            access_token = run(_get_slack_token(slack_user_id, slack_team_id))

            st.write("📋 Loading joined channels…")
            all_channels, ch_warnings = run(_get_slack_channels(access_token, slack_team_id))

            for w in ch_warnings:
                st.warning(w)

            public_ch  = [c for c in all_channels if not c.get("is_private")]
            private_ch = [c for c in all_channels if c.get("is_private")]

            # Apply ignore list
            channels = [
                ch for ch in all_channels
                if not _should_skip_channel(ch.get("name", ""))
            ]
            skipped = len(all_channels) - len(channels)

            # When syncing a specific team member, filter to channels they're in.
            # The EM's token lists the EM's channels — the target may not be in all of them.
            if target_user_id != slack_user_id:
                st.write(
                    f"Checking which of the {len(channels)} channel(s) "
                    f"**{selected_name}** is a member of…"
                )
                channels = run(
                    _filter_channels_by_member(access_token, slack_team_id, channels, target_user_id)
                )
                st.write(f"  → **{selected_name}** is in **{len(channels)}** channel(s).")

            skip_note = f", {skipped} ignored" if skipped else ""
            st.write(
                f"Found **{len(public_ch)}** public + **{len(private_ch)}** private "
                f"channels{skip_note}. Syncing **{len(channels)}**."
            )
        except Exception as e:
            slack_progress.empty()
            slack_status_text.empty()
            status.update(label="Failed to connect to Slack", state="error")
            st.error(str(e))
            st.stop()

        # Step 2 — sync each channel
        n = max(len(channels), 1)
        for i, ch in enumerate(channels):
            ch_id   = ch["id"]
            ch_name = ch.get("name", ch_id)
            pct     = int(i / n * 90)            # reserve last 10 % for normalization
            slack_progress.progress(pct, text=f"Syncing #{ch_name}… ({i + 1}/{n})")
            slack_status_text.caption(f"⏳ #{ch_name}")
            st.write(f"📥 #{ch_name}")

            # Filter to target user's messages when syncing a team member
            member_filter = target_user_id if target_user_id != slack_user_id else None
            count, err = run(
                _sync_slack_channel(
                    access_token, slack_team_id, slack_user_id,
                    ch_id, ch_name, oldest,
                    filter_user_id=member_filter,
                )
            )
            if err:
                errors.append(f"#{ch_name}: {err}")
                st.write(f"  ⚠️ {err}")
            else:
                total_msgs += count
                if count:
                    st.write(f"  ✓ {count} new message(s)")

        # Step 3 — normalize
        slack_progress.progress(92, text="Normalizing work units…")
        slack_status_text.caption("⏳ Normalizing…")
        st.write("🔄 Normalizing raw messages into work units…")
        normalized = run(_normalize_slack(slack_team_id))
        st.write(f"  ✓ {normalized} work unit(s) created.")

        slack_progress.progress(100, text="Done ✓")
        slack_status_text.empty()
        label = (
            f"✅ Slack sync complete — {total_msgs} new messages, {normalized} work units"
            + (f" · {len(errors)} error(s)" if errors else "")
        )
        status.update(label=label, state="complete")

    if errors:
        with st.expander(f"{len(errors)} channel(s) had errors"):
            for err in errors:
                st.warning(err)

# ─── Clean up stale channel data ──────────────────────────────────────────────

with st.expander("🗑️ Clean up stale channel data", expanded=False):
    st.caption(
        "Removes **SlackMessages** and **WorkUnits** that were synced for "
        f"**{selected_name}** but belong to channels they are no longer (or never were) "
        "a member of. Run this after adjusting the ignore list or team membership."
    )

    _ck_preview  = f"_cleanup_preview_{target_user_id}"
    _ck_valid    = f"_cleanup_valid_{target_user_id}"
    _ck_confirm  = f"_cleanup_confirm_{target_user_id}"

    col_preview, col_confirm, col_cancel = st.columns([2, 2, 1])

    with col_preview:
        if st.button("Preview stale data", key=f"preview_cleanup_{target_user_id}"):
            with st.spinner("Checking channels…"):
                try:
                    access_token_cleanup = run(_get_slack_token(slack_user_id, slack_team_id))
                    valid_ids, valid_names = run(
                        _get_valid_channel_ids(access_token_cleanup, slack_team_id, target_user_id)
                    )
                    msg_c, wu_c = run(
                        _count_stale_slack_data(target_user_id, slack_team_id, valid_ids)
                    )
                    st.session_state[_ck_valid]   = valid_ids
                    st.session_state[_ck_preview] = (msg_c, wu_c, valid_names)
                    st.session_state[_ck_confirm] = False
                except Exception as e:
                    st.error(f"Could not load channels: {e}")

    if _ck_preview in st.session_state:
        msg_c, wu_c, valid_names = st.session_state[_ck_preview]
        valid_ids = st.session_state.get(_ck_valid, [])
        st.info(
            f"**{selected_name}** is a member of **{len(valid_names)}** synced channel(s): "
            f"{', '.join(f'#{n}' for n in valid_names[:10])}"
            + (f" … and {len(valid_names) - 10} more" if len(valid_names) > 10 else "")
        )
        if msg_c == 0 and wu_c == 0:
            st.success("✅ No stale data found — everything looks clean.")
        else:
            st.warning(
                f"Found **{msg_c}** SlackMessage(s) and **{wu_c}** WorkUnit(s) "
                "outside those channels."
            )
            with col_confirm:
                if st.button(
                    f"Delete {msg_c} messages + {wu_c} work units",
                    type="primary",
                    key=f"confirm_cleanup_{target_user_id}",
                ):
                    st.session_state[_ck_confirm] = True

            if st.session_state.get(_ck_confirm):
                with st.spinner("Deleting stale data…"):
                    try:
                        del_msgs, del_wus = run(
                            _delete_stale_slack_data(target_user_id, slack_team_id, valid_ids)
                        )
                        st.success(
                            f"🗑️ Deleted **{del_msgs}** SlackMessage(s) and "
                            f"**{del_wus}** WorkUnit(s) from stale channels."
                        )
                        # Clear state so preview refreshes on next run
                        for k in (_ck_preview, _ck_valid, _ck_confirm):
                            st.session_state.pop(k, None)
                    except Exception as e:
                        st.error(f"Deletion failed: {e}")

st.markdown("---")

# ─── GitHub Sync ──────────────────────────────────────────────────────────────

st.subheader("GitHub")

has_token, gh_login = run(_get_github_link_info(target_user_id, slack_team_id))

if not has_token:
    if is_self:
        st.caption("You have not connected your GitHub account yet.")
        st.page_link("pages/1_Connect.py", label="Connect GitHub on the Connect Accounts page")
    else:
        st.caption(
            f"**{selected_name}** has not connected their GitHub account via OAuth. "
            f"Ask them to visit the **Connect Accounts** page and link their GitHub."
        )
    st.button("Sync GitHub", type="primary", disabled=True)
else:
    if is_self:
        st.caption(f"Pulls commits, PRs, reviews, and issues from your repositories (@{gh_login}).")
    else:
        st.caption(f"Syncing GitHub for **{selected_name}** (@{gh_login}).")

    days_github = st.slider("Days to backfill", min_value=1, max_value=90, value=7, key="github_days")

    if st.button("Sync GitHub", type="primary"):
        since = datetime.utcnow() - timedelta(days=days_github)
        total_counts: dict[str, int] = {"commits": 0, "prs": 0, "reviews": 0, "issues": 0}

        gh_progress = st.progress(0, text="Connecting to GitHub…")
        gh_status_text = st.empty()

        with st.status("Sync log", expanded=True) as status:
            try:
                st.write("🔑 Fetching credentials…")
                gh_token, github_login = run(_get_github_credentials(target_user_id, slack_team_id))
                st.write(f"📋 Loading repositories for @{github_login}…")
                repos = run(_get_github_repos(gh_token, github_login))
                st.write(f"Found **{len(repos)}** repository(ies) to scan.")
            except Exception as e:
                gh_progress.empty()
                gh_status_text.empty()
                status.update(label="Failed to connect to GitHub", state="error")
                st.error(str(e))
                st.stop()

            # Step 2 — sync each repo
            n = max(len(repos), 1)
            for i, repo in enumerate(repos):
                repo_name = repo["full_name"]
                pct = int(i / n * 90)
                gh_progress.progress(pct, text=f"Scanning {repo_name}… ({i + 1}/{n})")
                gh_status_text.caption(f"⏳ {repo_name}")
                st.write(f"📦 {repo_name}")

                try:
                    counts = run(
                        _sync_github_repo(gh_token, github_login, slack_team_id, target_user_id, repo, since)
                    )
                    added = sum(counts.values())
                    if added:
                        parts = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
                        st.write(f"  ✓ {parts}")
                    for k, v in counts.items():
                        total_counts[k] = total_counts.get(k, 0) + v
                except Exception as e:
                    st.write(f"  ⚠️ {e}")

            # Step 3 — normalize
            gh_progress.progress(92, text="Normalizing work units…")
            gh_status_text.caption("⏳ Normalizing…")
            st.write("🔄 Normalizing GitHub activity into work units…")
            normalized = run(_normalize_github(slack_team_id))
            st.write(f"  ✓ {normalized} work unit(s) created.")

            gh_progress.progress(100, text="Done ✓")
            gh_status_text.empty()
            summary = ", ".join(f"{v} {k}" for k, v in total_counts.items() if v) or "nothing new"
            status.update(
                label=f"✅ GitHub sync complete — {summary}, {normalized} work units",
                state="complete",
            )

st.markdown("---")

st.caption(
    "💡 For automated daily syncs, run `make worker` and `make beat` locally "
    "or set up a Celery worker in your deployment."
)
