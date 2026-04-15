"""
Team Overview page.

The engineering manager can search for workspace users and add them to
their tracked team.  Team members' Slack messages are captured automatically
when the EM syncs (SlackIngester stores all message authors, not just the EM).

When adding a member the EM can optionally supply the member's GitHub handle
so that GitHub activity is linked in reports without requiring the member to
connect their own GitHub account.
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
from app.models.user import User, UserGitHubLink  # User must be imported to register the mapper
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
    new_members: list[dict],  # each dict has slack fields + optional github_login
) -> int:
    added = 0
    async with AsyncSessionLocal() as db:
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
                member_email=m.get("email") or None,
                member_avatar_url=m.get("avatar_url") or None,
                github_login=m.get("github_login") or None,
            ))
            added += 1

        await db.commit()
    return added


async def _update_github_login(
    manager_user_id: str,
    manager_team_id: str,
    member_user_id: str,
    github_login: str | None,
) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TeamMember).where(
                TeamMember.manager_slack_user_id == manager_user_id,
                TeamMember.manager_slack_team_id == manager_team_id,
                TeamMember.member_slack_user_id == member_user_id,
            )
        )
        member = result.scalar_one_or_none()
        if member:
            member.github_login = github_login or None
            await db.commit()


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
            raise RuntimeError(
                f"No Slack token found in the database for "
                f"user_id={slack_user_id!r} team_id={slack_team_id!r}. "
                f"Try disconnecting and reconnecting Slack on the Connect Accounts page."
            )
        try:
            users = await list_workspace_users(ingester)
        finally:
            await ingester.close()
    return users


async def _get_oauth_github_login(slack_user_id: str) -> str | None:
    """Return GitHub login from UserGitHubLink (OAuth-connected members)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserGitHubLink.github_login).where(
                UserGitHubLink.slack_user_id == slack_user_id
            )
        )
        return result.scalar_one_or_none()


def _effective_github_login(member: TeamMember, oauth_login: str | None) -> str | None:
    """OAuth login takes precedence; fall back to the EM-supplied handle."""
    return oauth_login or member.github_login


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
    github_logins = {
        m.member_slack_user_id: run(_get_oauth_github_login(m.member_slack_user_id))
        for m in team_members
    }

    c1, c2 = st.columns(2)
    c1.metric("Team size", len(team_members))
    c2.metric(
        "GitHub linked",
        sum(1 for m in team_members if _effective_github_login(m, github_logins.get(m.member_slack_user_id)))
    )

    st.markdown("")

    # Column headers
    h = st.columns([3, 3, 2, 2, 1])
    h[0].markdown("**Name**")
    h[1].markdown("**Email**")
    h[2].markdown("**GitHub**")
    h[3].markdown("**Source**")
    h[4].markdown("**Remove**")
    st.divider()

    for member in team_members:
        oauth_login = github_logins.get(member.member_slack_user_id)
        effective_gh = _effective_github_login(member, oauth_login)

        cols = st.columns([3, 3, 2, 2, 1])
        cols[0].write(member.display())
        cols[1].write(member.member_email or "—")

        if effective_gh:
            cols[2].write(f"@{effective_gh}")
            cols[3].write("OAuth" if oauth_login else "Manual")
        else:
            cols[2].write("—")
            cols[3].write("—")

        # Inline GitHub handle editor (only shown when no OAuth link)
        if not oauth_login:
            with st.expander(f"Edit GitHub handle for {member.display()}"):
                new_gh = st.text_input(
                    "GitHub username",
                    value=member.github_login or "",
                    placeholder="e.g. octocat",
                    key=f"gh_{member.member_slack_user_id}",
                )
                if st.button("Save", key=f"save_gh_{member.member_slack_user_id}"):
                    run(_update_github_login(
                        slack_user_id, slack_team_id,
                        member.member_slack_user_id,
                        new_gh.strip() or None,
                    ))
                    st.success("GitHub handle updated.")
                    st.rerun()

        if cols[4].button("✕", key=f"remove_{member.member_slack_user_id}"):
            run(_remove_member(slack_user_id, slack_team_id, member.member_slack_user_id))
            st.success(f"Removed {member.display()}")
            st.rerun()

st.markdown("---")

# ─── Add members ──────────────────────────────────────────────────────────────

st.subheader("Add Members")

with st.expander("Browse workspace users", expanded=len(team_members) == 0):
    if st.button("Load workspace users", type="secondary"):
        with st.spinner("Fetching users from Slack..."):
            try:
                ws_users = run(_get_workspace_users(slack_user_id, slack_team_id))
                st.session_state["_ws_users"] = ws_users
                st.session_state.pop("_ws_users_error", None)
            except Exception as exc:
                import traceback
                st.session_state["_ws_users_error"] = (str(exc), traceback.format_exc())
                st.session_state.pop("_ws_users", None)

    ws_users: list[dict] = st.session_state.get("_ws_users", [])

    if ws_users:
        already_added = {m.member_slack_user_id for m in team_members}
        options = [
            u for u in ws_users
            if u["slack_user_id"] != slack_user_id
            and u["slack_user_id"] not in already_added
        ]

        if not options:
            st.info("All workspace users are already on your team.")
        else:
            selected: list[dict] = st.multiselect(
                f"Select users to add ({len(options)} available)",
                options=options,
                format_func=lambda u: (
                    f"{u['display_name']}  ·  {u['email']}"
                    if u.get("email") else u["display_name"]
                ),
                key="members_to_add",
            )

            if selected:
                st.markdown("**GitHub handles** _(optional — can be added or changed later)_")
                github_inputs: dict[str, str] = {}
                for u in selected:
                    label = u["display_name"]
                    if u.get("email"):
                        label += f"  ·  {u['email']}"
                    github_inputs[u["slack_user_id"]] = st.text_input(
                        label,
                        placeholder="GitHub username (e.g. octocat)",
                        key=f"new_gh_{u['slack_user_id']}",
                    )

                if st.button("Add selected members", type="primary"):
                    members_to_add = [
                        {**u, "github_login": github_inputs.get(u["slack_user_id"], "").strip() or None}
                        for u in selected
                    ]
                    count = run(_add_members(slack_user_id, slack_team_id, members_to_add))
                    if count:
                        st.success(f"Added {count} member(s) to your team.")
                    else:
                        st.info("Those members were already in your team.")
                    st.session_state.pop("_ws_users", None)
                    st.rerun()

    elif "_ws_users_error" in st.session_state:
        msg, tb = st.session_state["_ws_users_error"]
        st.error(f"Failed to load workspace users: **{msg}**")
        st.code(tb, language="text")
    elif "_ws_users" in st.session_state:
        st.warning("Slack returned 0 users. This is unexpected — the workspace may have no visible members.")

st.markdown("---")

# ─── Help ─────────────────────────────────────────────────────────────────────

with st.expander("How does team tracking work?"):
    st.markdown("""
    **You don't need your team members to sign in.**

    When you sync Slack (on the **Sync Data** page), the ingester reads every
    message from every public channel you're a member of — including messages
    written by your team members.  Each message is stored with the original
    author's Slack user ID.

    **GitHub handle** — enter a team member's GitHub username when adding them.
    This links their GitHub commits and PRs to their Slack identity in reports.
    If they later connect their own GitHub account via OAuth, that takes
    precedence automatically.

    **GitHub source** in the table shows:
    - *OAuth* — the member connected GitHub themselves (token on file)
    - *Manual* — you supplied their handle; read-only public data only
    - *—* — no GitHub handle set yet
    """)
