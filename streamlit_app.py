"""
Engineering Operations Manager — Streamlit UI

OAuth callback from Slack redirects to this root page.
GitHub uses a PAT pasted in the Connect page — no callback needed.
"""

import asyncio
import traceback

import nest_asyncio
import streamlit as st

from app.streamlit_env import load_streamlit_secrets_into_env

# Inject Streamlit Cloud secrets if available; fall back to local `.env`.
load_streamlit_secrets_into_env()

from app.ui.session_cookie import restore_session_from_cookie, make_session_token, _URL_PARAM

# Allow nested event loops — required for asyncio.run() inside Streamlit Cloud
nest_asyncio.apply()

from app.database import AsyncSessionLocal
from app.slack.oauth import exchange_code, save_slack_token
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
from app.ui.page_utils import inject_page_load_bar
inject_page_load_bar()
# Also restore here so the home page nav reflects the logged-in state


def run_async(coro):
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(coro)


# ─── Restore session from cookie (survives page reloads & OAuth redirects) ───
restore_session_from_cookie()

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
                # Embed a short-lived signed token in the redirect so Connect
                # page can restore session_state after the navigation.
                _sess_token = make_session_token(
                    token.slack_user_id,
                    token.slack_team_id,
                    token.slack_display_name or "",
                )
                st.query_params[_URL_PARAM] = _sess_token
                st.switch_page("pages/1_Connect.py")

            except Exception as e:
                status.update(label="Slack connection failed", state="error")
                st.error(f"**Error:** {e}")
                st.code(traceback.format_exc(), language="text")

    # GitHub OAuth callback removed — GitHub now uses PAT (paste in
    # Connect Accounts page). Stale `state=github:*` URLs are ignored.


# ─── Home Page ────────────────────────────────────────────────────────────────

st.title("⚙️ Engineering Operations Manager")
st.caption("Slack + GitHub activity intelligence for engineering leaders.")

st.markdown("---")

col1, col2, col3, col4, col5 = st.columns(5)
col1.page_link("pages/1_Connect.py",         label="🔗 Connect Accounts",   use_container_width=True)
col2.page_link("pages/2_Work_Report.py",     label="📊 Work Report",        use_container_width=True)
col3.page_link("pages/3_Team_Overview.py",   label="👥 Team Overview",      use_container_width=True)
col4.page_link("pages/4_Sync.py",            label="🔄 Sync Data",          use_container_width=True)
col5.page_link("pages/5_Notion_Dev_Track.py", label="📋 Notion Dev Track",  use_container_width=True)

st.markdown("---")

st.markdown("""
### What this tool does

- **Team roster** — add engineers; no sign-in needed for them.
- **Slack ingest** — standups + channel messages from every joined channel.
- **GitHub ingest** — commits, PRs, reviews, issues (PAT or handle).
- **Batch + background sync** — self / team / subset, per-member progress, runs off-page.
- **Flexible date ranges** — last N days or custom window (default 3 months).
- **Claude AI classification** — features, bugs, architecture, mentorship, incidents.
- **Shareable reports** — metrics, activity feed, insights, one-click copy.
- **Cleanup tools** — purge ignored-channel data or stale-member data.
- **Notion Dev Track Sync** — pulls per-dev skill tracks from Notion → Google Sheet; preview diff (cells + Focus Areas) before apply, per-member or bulk.
- **Focus Areas detection** — bullet added when any unchecked to-do is V-ing, `In-progress:`, `In-review:`, `New objective:`, or `To-review objective:` (handles adverb+V-ing like `Actively raising…`).
- **Toggleable-aware writes** — bullets land under the `## Focus Areas` heading, not the page bottom.
- **GMT+7 display** — all user-facing times in GMT+7; DB stays UTC.

### Getting started

1. **Connect Accounts** — Slack sign-in (required); optional GitHub PAT.
2. **Team Overview** — add members; set GitHub handles.
3. **Sync Data** — pick members + date range; run Slack/GitHub in background.
4. **Work Report** — generate per-member report; copy summary.
5. **Notion Dev Track** — fetch Notion DB, preview diffs, sync to Sheet.
""")
