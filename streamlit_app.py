"""
Engineering Operations Manager — Streamlit UI

OAuth callbacks from Slack and GitHub both redirect to this root page.
We detect the provider via the `state` query param prefix.
"""

import asyncio
import os
import traceback

import nest_asyncio
import streamlit as st

# ── Inject Streamlit Cloud secrets into os.environ ────────────────────────────
# Must happen before any app imports so pydantic-settings picks them up.
for _key, _val in st.secrets.items():
    if isinstance(_val, str):
        os.environ.setdefault(_key.upper(), _val)

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

- **Captures** standup messages and channel activity from Slack
- **Pulls** commits, PRs, reviews, and issues from GitHub
- **Normalizes** everything into a unified activity model
- **Classifies** work items using Claude AI
- **Generates** structured work reports for engineering leaders

### Getting started

1. Go to **Connect Accounts** and sign in with Slack
2. Link your GitHub account
3. Trigger a **Sync** to pull your history
4. Open **Work Report** to generate your first report
""")
