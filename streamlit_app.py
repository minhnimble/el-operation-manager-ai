"""
Engineering Operations Manager — Streamlit UI

This is the main entry point. It also handles OAuth callbacks from
Slack and GitHub (both redirect back to the root URL with ?code=...).

Deploy to Streamlit Community Cloud for a free public HTTPS URL —
use that URL as the OAuth callback in your Slack and GitHub app settings.
"""

import asyncio
import os
import streamlit as st

# Inject Streamlit Cloud secrets into os.environ before any app imports.
# Locally the app reads from .env; on Streamlit Cloud it reads from st.secrets.
# pydantic-settings picks up whichever is present via os.environ.
for _key, _val in st.secrets.items():
    if isinstance(_val, str):
        os.environ.setdefault(_key.upper(), _val)

from app.database import AsyncSessionLocal
from app.slack.oauth import exchange_code, save_slack_token
from app.github.oauth import link_github_to_user
from app.tasks.ingestion_tasks import trigger_github_sync, trigger_backfill

st.set_page_config(
    page_title="Engineering Operations Manager",
    page_icon="⚙️",
    layout="wide",
)


def run(coro):
    return asyncio.run(coro)


# ─── OAuth Callback Handler ───────────────────────────────────────────────────
# Both Slack and GitHub redirect back to the root Streamlit URL.
# We detect which one via the `state` query param prefix.

params = st.query_params

if "code" in params:
    code = params["code"]
    state = params.get("state", "")

    # Slack callback — state starts with "slack:"
    if state.startswith("slack:"):
        try:
            # exchange_code is sync; save_slack_token is async (DB only)
            token_data = exchange_code(code)

            async def _slack_cb():
                async with AsyncSessionLocal() as db:
                    return await save_slack_token(db, token_data)

            token = run(_slack_cb())
            st.query_params.clear()
            st.session_state["slack_user_id"] = token.slack_user_id
            st.session_state["slack_team_id"] = token.slack_team_id
            st.session_state["slack_display_name"] = token.slack_display_name
            st.success(f"✅ Slack connected as **{token.slack_display_name}** (team: {token.slack_team_name})")
        except Exception as e:
            st.error(f"Slack connection failed: {e}")

    # GitHub callback — state format is "github:{team_id}:{user_id}"
    elif state.startswith("github:"):
        try:
            _, slack_team_id, slack_user_id = state.split(":", 2)

            async def _github_cb():
                async with AsyncSessionLocal() as db:
                    link = await link_github_to_user(
                        db=db,
                        slack_user_id=slack_user_id,
                        slack_team_id=slack_team_id,
                        code=code,
                    )
                    return link

            link = run(_github_cb())
            trigger_github_sync.delay(
                slack_user_id=slack_user_id,
                slack_team_id=slack_team_id,
                days_back=30,
            )
            st.query_params.clear()
            st.success(f"✅ GitHub connected as **@{link.github_login}** — initial sync queued.")
        except Exception as e:
            st.error(f"GitHub connection failed: {e}")


# ─── Home Page ────────────────────────────────────────────────────────────────

st.title("⚙️ Engineering Operations Manager")
st.caption("Slack + GitHub activity intelligence for engineering leaders.")

st.markdown("---")

col1, col2, col3, col4 = st.columns(4)
col1.page_link("pages/1_Connect.py",        label="🔗 Connect Accounts",  use_container_width=True)
col2.page_link("pages/2_Work_Report.py",    label="📊 Work Report",       use_container_width=True)
col3.page_link("pages/3_Team_Overview.py",  label="👥 Team Overview",     use_container_width=True)
col4.page_link("pages/4_Sync.py",           label="🔄 Sync Data",         use_container_width=True)

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
