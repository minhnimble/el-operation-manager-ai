"""
Sync Data page.

Syncs run directly in the Streamlit session (no Celery/Redis required).
This works on Streamlit Cloud and local dev alike.

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
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.team_member import TeamMember
from app.models.user import User, UserGitHubLink
from app.models.slack_token import SlackUserToken
from app.ingestion.slack_ingester import SlackIngester
from app.ingestion.github_ingester import GitHubIngester

st.set_page_config(page_title="Sync Data", page_icon="🔄", layout="wide")


def run(coro):
    return asyncio.run(coro)


# ── Team selector helper ───────────────────────────────────────────────────────

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


async def _get_github_link(slack_user_id: str, slack_team_id: str) -> UserGitHubLink | None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserGitHubLink).where(
                UserGitHubLink.slack_user_id == slack_user_id,
                UserGitHubLink.slack_team_id == slack_team_id,
            )
        )
        return result.scalar_one_or_none()


# ── Sync functions (run directly, no Celery) ──────────────────────────────────

async def _run_slack_sync(slack_user_id: str, team_id: str, days_back: int) -> dict:
    """Backfill all joined Slack channels using the EM's token."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SlackUserToken).where(
                SlackUserToken.slack_user_id == slack_user_id,
                SlackUserToken.slack_team_id == team_id,
            )
        )
        token_record = result.scalar_one_or_none()
        if not token_record:
            raise RuntimeError(
                "No Slack token found. Please reconnect your Slack account on the Connect Accounts page."
            )

        ingester = SlackIngester(user_token=token_record.access_token, team_id=team_id)
        oldest = datetime.utcnow() - timedelta(days=days_back)
        total = 0
        channels_synced = 0
        errors = []

        try:
            channels = await ingester.get_joined_channels()
            for channel in channels:
                channel_id = channel["id"]
                channel_name = channel.get("name", channel_id)
                try:
                    count = await ingester.backfill_channel(
                        db=db,
                        channel_id=channel_id,
                        channel_name=channel_name,
                        slack_user_id=slack_user_id,
                        oldest=oldest,
                    )
                    await db.commit()
                    total += count
                    channels_synced += 1
                except Exception as e:
                    await db.rollback()
                    errors.append(f"#{channel_name}: {e}")
        finally:
            await ingester.close()

    return {"messages": total, "channels": channels_synced, "errors": errors}


async def _run_github_sync(slack_user_id: str, slack_team_id: str, days_back: int) -> dict:
    """Sync GitHub activity for a single user via their OAuth token."""
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

        ingester = GitHubIngester(
            access_token=link.github_access_token,
            github_login=link.github_login,
        )
        since = datetime.utcnow() - timedelta(days=days_back)
        try:
            counts = await ingester.ingest_user_activity(
                db=db,
                slack_team_id=slack_team_id,
                slack_user_id=slack_user_id,
                since=since,
            )
            await db.commit()
        finally:
            await ingester.close()

    return counts


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

days_slack = st.slider("Days to backfill", min_value=1, max_value=90, value=30, key="slack_days")

if st.button("Sync Slack", type="primary"):
    with st.spinner(f"Syncing Slack messages for the last {days_slack} days…"):
        try:
            result = run(_run_slack_sync(slack_user_id, slack_team_id, days_slack))
            st.success(
                f"✅ Synced **{result['messages']}** new messages "
                f"across **{result['channels']}** channels."
            )
            if result["errors"]:
                with st.expander(f"{len(result['errors'])} channel(s) had errors"):
                    for err in result["errors"]:
                        st.warning(err)
        except Exception as e:
            st.error(f"Slack sync failed: {e}")

st.markdown("---")

# ─── GitHub Sync ──────────────────────────────────────────────────────────────

st.subheader("GitHub")

github_link = run(_get_github_link(target_user_id, slack_team_id))

if not github_link or not github_link.github_access_token:
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
    gh_login = github_link.github_login or "—"
    if is_self:
        st.caption(f"Pulls commits, PRs, reviews, and issues from your repositories (@{gh_login}).")
    else:
        st.caption(f"Syncing GitHub for **{selected_name}** (@{gh_login}).")

    days_github = st.slider("Days to backfill", min_value=1, max_value=90, value=30, key="github_days")

    if st.button("Sync GitHub", type="primary"):
        with st.spinner(f"Syncing GitHub activity for the last {days_github} days…"):
            try:
                counts = run(_run_github_sync(target_user_id, slack_team_id, days_github))
                st.success(
                    f"✅ GitHub sync complete for **{selected_name}**: {counts}"
                )
            except Exception as e:
                st.error(f"GitHub sync failed: {e}")

st.markdown("---")

st.caption(
    "💡 For automated daily syncs, run `make worker` and `make beat` locally "
    "or set up a Celery worker in your deployment."
)
