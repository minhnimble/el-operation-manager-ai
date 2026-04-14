"""
Team Overview page.

Shows all opted-in users and their connection status.
"""

import asyncio
import os
import streamlit as st

for _key, _val in st.secrets.items():
    if isinstance(_val, str):
        os.environ.setdefault(_key.upper(), _val)
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.user import User, UserGitHubLink
from app.models.slack_token import SlackUserToken

st.set_page_config(page_title="Team Overview", page_icon="👥", layout="wide")


def run(coro):
    return asyncio.run(coro)


async def _get_team(team_id: str) -> list[dict]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.slack_team_id == team_id)
        )
        users = result.scalars().all()

        rows = []
        for u in users:
            github_result = await db.execute(
                select(UserGitHubLink).where(
                    UserGitHubLink.slack_user_id == u.slack_user_id
                )
            )
            github = github_result.scalar_one_or_none()

            slack_result = await db.execute(
                select(SlackUserToken).where(
                    SlackUserToken.slack_user_id == u.slack_user_id
                )
            )
            slack_token = slack_result.scalar_one_or_none()

            rows.append({
                "Name": u.slack_display_name or u.slack_real_name or u.slack_user_id,
                "Slack ID": u.slack_user_id,
                "Slack Connected": "✅" if slack_token else "❌",
                "GitHub Login": f"@{github.github_login}" if github and github.github_login else "—",
                "GitHub Connected": "✅" if github and github.github_login else "❌",
                "Opted In": "✅" if u.opted_in else "❌",
            })
        return rows


st.title("👥 Team Overview")
st.caption("All users who have connected their accounts.")
st.markdown("---")

slack_team_id = st.session_state.get("slack_team_id")

if not slack_team_id:
    st.warning("Please connect your Slack account first on the **Connect Accounts** page.")
    st.page_link("pages/1_Connect.py", label="Go to Connect Accounts")
    st.stop()

team = run(_get_team(slack_team_id))

if not team:
    st.info("No users have connected yet. Share the app URL with your team.")
else:
    total = len(team)
    slack_connected = sum(1 for u in team if u["Slack Connected"] == "✅")
    github_connected = sum(1 for u in team if u["GitHub Connected"] == "✅")

    c1, c2, c3 = st.columns(3)
    c1.metric("Total Users",        total)
    c2.metric("Slack Connected",    slack_connected)
    c3.metric("GitHub Connected",   github_connected)

    st.markdown("---")
    st.dataframe(team, use_container_width=True, hide_index=True)
