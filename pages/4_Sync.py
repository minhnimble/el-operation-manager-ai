"""
Sync Data page.

Slack sync uses the EM's own token and captures messages from every author
in every joined channel — no member selector needed.

GitHub sync targets a specific team member: the selected user must have
connected their own GitHub account via OAuth for the sync to work.
"""

import asyncio
import os
import streamlit as st

for _key, _val in st.secrets.items():
    if isinstance(_val, str):
        os.environ.setdefault(_key.upper(), _val)

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.team_member import TeamMember
from app.models.user import User, UserGitHubLink
from app.tasks.ingestion_tasks import trigger_backfill, trigger_github_sync

st.set_page_config(page_title="Sync Data", page_icon="🔄", layout="wide")


def run(coro):
    return asyncio.run(coro)


async def _get_team_options(manager_user_id: str, manager_team_id: str, self_name: str) -> dict[str, str]:
    """Return {display_name: slack_user_id} for self + all added team members."""
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

st.info(
    "Syncs run in the background via Celery. Make sure the **worker** is running. "
    "Large backfills (30 days) may take a few minutes."
)

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
    try:
        # Always use the EM's own user_id — the token is theirs and captures everyone
        trigger_backfill.delay(
            slack_user_id=slack_user_id,
            team_id=slack_team_id,
            days_back=days_slack,
        )
        st.success(
            f"Slack backfill queued for the last {days_slack} days. "
            f"Messages from all team members will be captured."
        )
    except Exception as e:
        st.error(f"Failed to queue sync: {e}")

st.markdown("---")

# ─── GitHub Sync ──────────────────────────────────────────────────────────────

st.subheader("GitHub")

github_link = run(_get_github_link(target_user_id, slack_team_id))

if not is_self and not github_link:
    st.caption(
        f"**{selected_name}** has not connected their GitHub account via OAuth. "
        f"Ask them to visit the **Connect Accounts** page and link their GitHub."
    )
    st.button("Sync GitHub", type="primary", disabled=True)
else:
    if is_self:
        st.caption("Pulls commits, PRs, reviews, and issues from your repositories.")
    else:
        gh_login = github_link.github_login if github_link else "—"
        st.caption(f"Syncing GitHub for **{selected_name}** (@{gh_login}).")

    days_github = st.slider("Days to backfill", min_value=1, max_value=90, value=30, key="github_days")

    if st.button("Sync GitHub", type="primary"):
        try:
            trigger_github_sync.delay(
                slack_user_id=target_user_id,
                slack_team_id=slack_team_id,
                days_back=days_github,
            )
            st.success(
                f"GitHub sync queued for **{selected_name}** "
                f"— last {days_github} days. Check back shortly."
            )
        except Exception as e:
            st.error(f"Failed to queue sync: {e}")

st.markdown("---")

st.caption(
    "Syncs are also scheduled automatically: Slack at 1 am UTC, GitHub at 2 am UTC (daily)."
)
