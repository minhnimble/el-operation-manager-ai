"""
Team Overview page.

The engineering manager can search for workspace users and add them to
their tracked team.  Team members' Slack messages are captured automatically
when the EM syncs (SlackIngester stores all message authors, not just the EM).
"""

import asyncio
import os
import streamlit as st

for _key, _val in st.secrets.items():
    if isinstance(_val, str):
        os.environ.setdefault(_key.upper(), _val)

from sqlalchemy import select, delete

from app.database import AsyncSessionLocal
from app.models.team_member import TeamMember
from app.models.user import UserGitHubLink
from app.ingestion.slack_ingester import get_slack_ingester
from app.slack.users import list_workspace_users

st.set_page_config(page_title="Team Overview", page_icon="👥", layout="wide")


def run(coro):
    return asyncio.run(coro)


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _get_team_members(manager_user_id: str, manager_team_id: str) -> list[TeamMember]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TeamMember).where(
                TeamMember.manager_slack_user_id == manager_user_id,
                TeamMember.manager_slack_team_id == manager_team_id,
            ).order_by(TeamMember.member_display_name)
        )
        return result.scalars().all()


async def _add_members(
    manager_user_id: str,
    manager_team_id: str,
    new_members: list[dict],
) -> int:
    """Upsert team members. Returns number added."""
    added = 0
    async with AsyncSessionLocal() as db:
        # Get existing member IDs to skip duplicates
        existing_result = await db.execute(
            select(TeamMember.member_slack_user_id).where(
                TeamMember.manager_slack_user_id == manager_user_id,
                TeamMember.manager_slack_team_id == manager_team_id,
            )
        )
        existing_ids = {row[0] for row in existing_result}

        for m in new_members:
            if m["slack_user_id"] in existing_ids:
                continue
            db.add(TeamMember(
                manager_slack_user_id=manager_user_id,
                manager_slack_team_id=manager_team_id,
                member_slack_user_id=m["slack_user_id"],
                member_slack_team_id=manager_team_id,
                member_display_name=m["display_name"],
                member_real_name=m["real_name"],
                member_avatar_url=m.get("avatar_url", ""),
            ))
            added += 1

        await db.commit()
    return added


async def _remove_member(
    manager_user_id: str,
    manager_team_id: str,
    member_user_id: str,
) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(TeamMember).where(
                TeamMember.manager_slack_user_id == manager_user_id,
                TeamMember.manager_slack_team_id == manager_team_id,
                TeamMember.member_slack_user_id == member_user_id,
            )
        )
        await db.commit()


async def _get_workspace_users(slack_user_id: str, slack_team_id: str) -> list[dict]:
    async with AsyncSessionLocal() as db:
        ingester = await get_slack_ingester(db, slack_user_id, slack_team_id)
        if not ingester:
            return []
        try:
            users = await list_workspace_users(ingester)
        finally:
            await ingester.close()
    return users


async def _get_github_link(slack_user_id: str) -> str | None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserGitHubLink.github_login).where(
                UserGitHubLink.slack_user_id == slack_user_id
            )
        )
        row = result.scalar_one_or_none()
        return row


# ── Page ──────────────────────────────────────────────────────────────────────

st.title("👥 Team Overview")
st.caption("Add team members to track their Slack and GitHub activity.")
st.markdown("---")

slack_user_id = st.session_state.get("slack_user_id")
slack_team_id = st.session_state.get("slack_team_id")
manager_name = st.session_state.get("slack_display_name", slack_user_id)

if not slack_user_id:
    st.warning("Please connect your Slack account first on the **Connect Accounts** page.")
    st.page_link("pages/1_Connect.py", label="Go to Connect Accounts")
    st.stop()

# ─── Current team ─────────────────────────────────────────────────────────────

st.subheader("Your Team")
st.caption(f"Managing as **{manager_name}**")

team_members = run(_get_team_members(slack_user_id, slack_team_id))

if not team_members:
    st.info("No team members added yet. Use **Add Members** below to get started.")
else:
    # Summary metrics
    c1, c2 = st.columns(2)
    c1.metric("Team size", len(team_members))

    github_count = sum(
        1 for m in team_members
        if run(_get_github_link(m.member_slack_user_id))
    )
    c2.metric("GitHub connected", github_count)

    st.markdown("")

    # Member table with remove buttons
    header_cols = st.columns([3, 3, 2, 1])
    header_cols[0].markdown("**Name**")
    header_cols[1].markdown("**Slack ID**")
    header_cols[2].markdown("**GitHub**")
    header_cols[3].markdown("**Remove**")

    st.divider()

    for member in team_members:
        github_login = run(_get_github_link(member.member_slack_user_id))
        cols = st.columns([3, 3, 2, 1])
        cols[0].write(member.display())
        cols[1].write(f"`{member.member_slack_user_id}`")
        cols[2].write(f"@{github_login}" if github_login else "—")
        if cols[3].button("✕", key=f"remove_{member.member_slack_user_id}"):
            run(_remove_member(slack_user_id, slack_team_id, member.member_slack_user_id))
            st.success(f"Removed {member.display()}")
            st.rerun()

st.markdown("---")

# ─── Add members ──────────────────────────────────────────────────────────────

st.subheader("Add Members")

with st.expander("Browse workspace users", expanded=len(team_members) == 0):
    if st.button("Load workspace users", type="secondary"):
        with st.spinner("Fetching users from Slack..."):
            ws_users = run(_get_workspace_users(slack_user_id, slack_team_id))
            st.session_state["_ws_users"] = ws_users

    ws_users: list[dict] = st.session_state.get("_ws_users", [])

    if ws_users:
        already_added = {m.member_slack_user_id for m in team_members}
        # Build options, excluding self and already-added members
        options = [
            u for u in ws_users
            if u["slack_user_id"] != slack_user_id
            and u["slack_user_id"] not in already_added
        ]

        if not options:
            st.info("All workspace users are already on your team.")
        else:
            selected = st.multiselect(
                f"Select users to add ({len(options)} available)",
                options=options,
                format_func=lambda u: f"{u['display_name']}  ({u['slack_user_id']})",
                key="members_to_add",
            )

            if selected and st.button("Add selected members", type="primary"):
                count = run(_add_members(slack_user_id, slack_team_id, selected))
                if count:
                    st.success(f"Added {count} member(s) to your team.")
                else:
                    st.info("Those members were already in your team.")
                st.session_state.pop("_ws_users", None)
                st.rerun()
    elif "_ws_users" in st.session_state:
        st.warning("Could not load workspace users. Make sure your Slack token is connected.")

st.markdown("---")

# ─── Help ─────────────────────────────────────────────────────────────────────

with st.expander("How does team tracking work?"):
    st.markdown("""
    **You don't need your team members to sign in.**

    When you sync Slack (on the **Sync Data** page), the ingester reads every
    message from every public channel you're a member of — including messages
    written by your team members.  Each message is stored with the original
    author's Slack user ID.

    Adding a team member here simply tells the dashboard *who to look for* when
    building reports.  Their Slack activity is already captured automatically.

    **GitHub data** is per-user and requires the team member to connect their
    own GitHub account via the Connect Accounts page.  Until they do, the
    GitHub metrics in their report will show zeros.
    """)
