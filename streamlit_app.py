"""
Engineering Operations Manager — Streamlit UI

OAuth callbacks from Slack and GitHub both redirect to this root page.
We detect the provider via the `state` query param prefix.
"""

import asyncio
import traceback

import nest_asyncio
import streamlit as st

from app.streamlit_env import load_streamlit_secrets_into_env

# Inject Streamlit Cloud secrets if available; fall back to local `.env`.
load_streamlit_secrets_into_env()

# Allow nested event loops — required for asyncio.run() inside Streamlit Cloud
nest_asyncio.apply()

from app.database import AsyncSessionLocal
from app.slack.oauth import exchange_code, save_slack_token
from app.github.oauth import link_github_to_user
from app.config import get_settings

settings = get_settings()


def _db_host() -> str:
    """Extract host from DATABASE_URL for diagnostics (no credentials)."""
    try:
        url = settings.database_url
        # postgresql+asyncpg://user:pass@host:port/db
        host_part = url.split("@")[-1].split("/")[0]
        return host_part
    except Exception:
        return "(unable to parse)"

st.set_page_config(
    page_title="Home",
    page_icon="⚙️",
    layout="wide",
)


def run_async(coro):
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(coro)


# ─── OAuth Callback Handler ───────────────────────────────────────────────────

params = st.query_params

if "code" in params:
    code = params["code"]
    state = params.get("state", "")

    # ── Slack callback ─────────────────────────────────────────────────────────
    if state.startswith("slack:"):
        with st.status("Connecting Slack account...", expanded=True) as status:
            try:
                st.write("Exchanging OAuth code with Slack...")
                token_data = exchange_code(code)
                st.write("✓ Token received")

                st.write(f"Saving to database (host: `{_db_host()}`)...")
                async def _slack_cb():
                    async with AsyncSessionLocal() as db:
                        token = await save_slack_token(db, token_data)
                        await db.commit()
                        return token

                token = run_async(_slack_cb())
                st.write("✓ Account saved")

                status.update(label="Slack connected!", state="complete")
                st.query_params.clear()
                st.session_state["slack_user_id"] = token.slack_user_id
                st.session_state["slack_team_id"] = token.slack_team_id
                st.session_state["slack_display_name"] = token.slack_display_name
                st.success(
                    f"✅ Signed in as **{token.slack_display_name}** "
                    f"(team: {token.slack_team_name})"
                )
                st.switch_page("pages/1_Connect.py")

            except Exception as e:
                status.update(label="Slack connection failed", state="error")
                st.error(f"**Error:** {e}")
                st.code(traceback.format_exc(), language="text")

    # ── GitHub callback ────────────────────────────────────────────────────────
    elif state.startswith("github:"):
        with st.status("Connecting GitHub account...", expanded=True) as status:
            try:
                _, slack_team_id, slack_user_id = state.split(":", 2)

                st.write("Exchanging OAuth code with GitHub...")
                async def _github_cb():
                    async with AsyncSessionLocal() as db:
                        link = await link_github_to_user(
                            db=db,
                            slack_user_id=slack_user_id,
                            slack_team_id=slack_team_id,
                            code=code,
                        )
                        await db.commit()
                        return link

                link = run_async(_github_cb())
                st.write("✓ GitHub account linked")

                status.update(label="GitHub connected!", state="complete")
                st.query_params.clear()
                st.success(
                    f"✅ GitHub connected as **@{link.github_login}**"
                )
                st.switch_page("pages/1_Connect.py")

            except Exception as e:
                status.update(label="GitHub connection failed", state="error")
                st.error(f"**Error:** {e}")
                st.code(traceback.format_exc(), language="text")


# ─── Home Page ────────────────────────────────────────────────────────────────

st.title("⚙️ Engineering Operations Manager")
st.caption("Slack + GitHub activity intelligence for engineering leaders.")

st.markdown("---")

col1, col2, col3, col4 = st.columns(4)
col1.page_link("pages/1_Connect.py",       label="🔗 Connect Accounts", use_container_width=True)
col2.page_link("pages/2_Work_Report.py",   label="📊 Work Report",      use_container_width=True)
col3.page_link("pages/3_Team_Overview.py", label="👥 Team Overview",    use_container_width=True)
col4.page_link("pages/4_Sync.py",          label="🔄 Sync Data",        use_container_width=True)

st.markdown("---")

st.markdown("""
### What this tool does

- **Tracks your team** — add engineers to your roster; they don't need to sign in
- **Captures Slack activity** — standups, discussions, and channel messages from every channel you're in
- **Pulls GitHub activity** — commits, PRs, reviews, and issues via OAuth or a manually set GitHub handle
- **Classifies work** using Claude AI — surfaces feature work, bug fixes, architecture, mentorship, and incidents from standup text
- **Generates shareable reports** — metrics, activity feed, AI insights, and a one-click copy summary per team member

### Getting started

1. **Connect Accounts** — sign in with Slack (required); optionally link your own GitHub account
2. **Team Overview** — add your team members; enter their GitHub handles if they haven't connected OAuth
3. **Sync Data** — pull Slack messages and GitHub activity for yourself or a specific team member
4. **Work Report** — generate a report for any team member, browse their activity feed, and copy a summary to share
""")
