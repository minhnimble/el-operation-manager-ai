"""
Connect Accounts page.

Handles Sign in with Slack and GitHub OAuth linking.
Both providers redirect back to the root URL (streamlit_app.py)
where the code exchange is completed.
"""

import asyncio
import secrets
import streamlit as st

from app.streamlit_env import load_streamlit_secrets_into_env

load_streamlit_secrets_into_env()
from sqlalchemy import select

from sqlalchemy import delete
from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models.slack_token import SlackUserToken
from app.models.user import UserGitHubLink
from app.slack.oauth import build_auth_url

st.set_page_config(page_title="Connect Accounts", page_icon="🔗", layout="wide")
settings = get_settings()


def run(coro):
    return asyncio.run(coro)


def _oauth_button(label: str, url: str, primary: bool = True) -> None:
    """Open the OAuth URL in a new tab and close the current tab.

    Must use components.html — st.markdown passes through DOMPurify which
    strips onclick handlers, so clicks do nothing.  components.html renders
    inside a sandboxed iframe that allows scripts and popups, so onclick fires
    normally and window.open() works.
    """
    import html as _html
    import streamlit.components.v1 as components

    safe_href = _html.escape(url, quote=True)
    safe_label = _html.escape(label)

    bg     = "#ff4b4b" if primary else "transparent"
    fg     = "#ffffff" if primary else "#31333f"
    border = "#ff4b4b" if primary else "#d0d0d0"

    components.html(
        f"""
        <a href="#"
           onclick="
             window.open('{safe_href}', '_blank');
             try {{
               window.top.close();
             }} catch(e) {{}}
             try {{
               window.top.document.body.innerHTML =
                 '<div style=\\'font-family:sans-serif;padding:60px;text-align:center;\\'>'
                 + '<h2>&#x1F517; OAuth opened in new tab</h2>'
                 + '<p>Please complete sign-in there. You can close this tab.</p>'
                 + '</div>';
             }} catch(e) {{}}
             return false;"
           style="
               display:inline-block;
               background:{bg};
               color:{fg};
               border:1px solid {border};
               border-radius:6px;
               padding:8px 20px;
               font-size:14px;
               font-weight:500;
               font-family:sans-serif;
               line-height:1.5;
               text-decoration:none;
               cursor:pointer;">
          {safe_label}
        </a>
        """,
        height=48,
    )


async def _disconnect_slack(slack_user_id: str, slack_team_id: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(SlackUserToken).where(
                SlackUserToken.slack_user_id == slack_user_id,
                SlackUserToken.slack_team_id == slack_team_id,
            )
        )
        await db.commit()


async def _disconnect_github(slack_user_id: str, slack_team_id: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(UserGitHubLink).where(
                UserGitHubLink.slack_user_id == slack_user_id,
                UserGitHubLink.slack_team_id == slack_team_id,
            )
        )
        await db.commit()


async def _get_connection_status(slack_user_id: str, slack_team_id: str) -> dict:
    async with AsyncSessionLocal() as db:
        slack_result = await db.execute(
            select(SlackUserToken).where(
                SlackUserToken.slack_user_id == slack_user_id,
                SlackUserToken.slack_team_id == slack_team_id,
            )
        )
        slack_token = slack_result.scalar_one_or_none()

        github_result = await db.execute(
            select(UserGitHubLink).where(
                UserGitHubLink.slack_user_id == slack_user_id,
                UserGitHubLink.slack_team_id == slack_team_id,
            )
        )
        github_link = github_result.scalar_one_or_none()

    return {
        "slack_connected": slack_token is not None,
        "slack_name": slack_token.slack_display_name if slack_token else None,
        "github_connected": github_link is not None and github_link.github_login is not None,
        "github_login": github_link.github_login if github_link else None,
    }


st.title("🔗 Connect Accounts")
st.caption("Connect your Slack and GitHub accounts to enable activity tracking.")
st.markdown("---")

# ─── Slack ────────────────────────────────────────────────────────────────────

st.subheader("Slack")

slack_user_id = st.session_state.get("slack_user_id")
slack_team_id = st.session_state.get("slack_team_id")

if slack_user_id:
    col1, col2 = st.columns([4, 1])
    col1.success(f"✅ Connected as **{st.session_state.get('slack_display_name', slack_user_id)}**")
    if col2.button("Disconnect", key="disconnect_slack"):
        run(_disconnect_slack(slack_user_id, slack_team_id))
        for key in ("slack_user_id", "slack_team_id", "slack_display_name"):
            st.session_state.pop(key, None)
        st.rerun()

    state = secrets.token_urlsafe(12)
    slack_auth_url = build_auth_url(state=state)
    _oauth_button("Reconnect Slack (refresh token / scopes)", slack_auth_url, primary=False)
else:
    st.info("Sign in with Slack to allow the app to read your channel messages.")
    state = secrets.token_urlsafe(12)
    slack_auth_url = build_auth_url(state=state)
    _oauth_button("Sign in with Slack", slack_auth_url)

st.markdown("---")

# ─── GitHub ───────────────────────────────────────────────────────────────────

st.subheader("GitHub")

if not slack_user_id:
    st.warning("Connect Slack first to enable GitHub linking.")
else:
    status = run(_get_connection_status(slack_user_id, slack_team_id))

    if status["github_connected"]:
        col1, col2 = st.columns([4, 1])
        col1.success(f"✅ Connected as **@{status['github_login']}**")
        if col2.button("Disconnect", key="disconnect_github"):
            run(_disconnect_github(slack_user_id, slack_team_id))
            st.rerun()

        github_state = f"github:{slack_team_id}:{slack_user_id}"
        github_url = (
            f"https://github.com/login/oauth/authorize"
            f"?client_id={settings.github_client_id}"
            f"&scope=read:user,repo"
            f"&state={github_state}"
            f"&redirect_uri={settings.app_base_url}"
        )
        _oauth_button("Reconnect GitHub (refresh token / scopes)", github_url, primary=False)
    else:
        st.info("Link your GitHub account to enable commit and PR tracking.")
        github_state = f"github:{slack_team_id}:{slack_user_id}"
        github_url = (
            f"https://github.com/login/oauth/authorize"
            f"?client_id={settings.github_client_id}"
            f"&scope=read:user,repo"
            f"&state={github_state}"
            f"&redirect_uri={settings.app_base_url}"
        )
        _oauth_button("Connect GitHub", github_url)

st.markdown("---")

# ─── Help ─────────────────────────────────────────────────────────────────────

with st.expander("How does this work?"):
    st.markdown("""
    **Slack** uses Sign in with Slack (user OAuth). We request read-only access
    to public channels you're a member of. No messages are read in real time —
    data is pulled on demand when you trigger a sync.

    **GitHub** uses GitHub OAuth. We request `read:user` and `repo` scopes to
    pull your commits, pull requests, and code reviews.

    Neither connection shares your data with third parties.
    All data is stored in your own database.
    """)
