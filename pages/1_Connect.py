"""
Connect Accounts page.

Slack uses Sign-in-with-Slack OAuth (callback handled in streamlit_app.py).
GitHub uses a Personal Access Token (PAT) pasted directly here — no OAuth.
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
from app.github.oauth import link_github_login
from app.slack.oauth import build_auth_url

st.set_page_config(page_title="Connect Accounts", page_icon="🔗", layout="wide")
from app.ui.page_utils import inject_page_load_bar
from app.ui.session_cookie import restore_session_from_cookie
inject_page_load_bar()
restore_session_from_cookie()
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

    # Match the rest of the app's button system (see .streamlit/config.toml):
    #   Primary  → filled blue (theme primaryColor #4C9BE8), white text.
    #   Secondary → transparent bg, white text, white border — visible on the
    #               dark theme without competing with the primary CTA.
    if primary:
        bg, fg, border = "#4C9BE8", "#ffffff", "#4C9BE8"
        hover_bg, hover_border = "#3b8ad6", "#3b8ad6"
    else:
        bg, fg, border = "transparent", "#ffffff", "#ffffff"
        hover_bg, hover_border = "rgba(255,255,255,0.08)", "#ffffff"

    components.html(
        f"""
        <style>
          .el-oauth-btn {{
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
            cursor:pointer;
            transition: background 120ms ease, border-color 120ms ease;
          }}
          .el-oauth-btn:hover {{
            background:{hover_bg};
            border-color:{hover_border};
          }}
        </style>
        <a href="#" class="el-oauth-btn"
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
             return false;">
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


async def _get_github_status(slack_user_id: str, slack_team_id: str) -> dict:
    """Fetch just the GitHub link. The Slack half of the old combined
    helper was dead weight on this page — Slack state comes from session_state."""
    async with AsyncSessionLocal() as db:
        github_result = await db.execute(
            select(UserGitHubLink).where(
                UserGitHubLink.slack_user_id == slack_user_id,
                UserGitHubLink.slack_team_id == slack_team_id,
            )
        )
        github_link = github_result.scalar_one_or_none()

    return {
        "github_connected": github_link is not None and github_link.github_login is not None,
        "github_login": github_link.github_login if github_link else None,
    }


st.title("🔗 Connect Accounts")
st.caption("Connect your Slack and GitHub accounts to enable activity tracking.")

# Destructive styling for Disconnect buttons. Streamlit adds `st-key-{key}`
# as a class on the element container when a button has `key=`, which lets us
# scope the override to just these two buttons instead of all secondary ones.
st.markdown(
    """
    <style>
      .st-key-disconnect_slack button,
      .st-key-disconnect_github button {
        background-color: #d32f2f !important;
        border-color: #d32f2f !important;
        color: #ffffff !important;
      }
      .st-key-disconnect_slack button:hover,
      .st-key-disconnect_github button:hover {
        background-color: #b71c1c !important;
        border-color: #b71c1c !important;
        color: #ffffff !important;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

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

_server_pat_set = bool((settings.github_pat or "").strip())

if not _server_pat_set:
    st.error(
        "❌ **`GITHUB_PAT` is not configured.** Set it in env / Streamlit "
        "secrets (scopes: `repo` + `read:org`) and reboot. GitHub sync is "
        "disabled until then."
    )
else:
    st.success(
        "✅ Server-wide GitHub PAT loaded from env. "
        "Map your GitHub login below so the app knows whose PRs to query."
    )

if not slack_user_id:
    st.warning("Connect Slack first to enable GitHub linking.")
else:
    from app.ui.page_utils import loading_section

    _cache_key = (slack_user_id, slack_team_id)
    _cached = st.session_state.get("_gh_status_cache")
    _force_refresh = st.session_state.pop("_gh_just_disconnected", False)

    if _cached and _cached["key"] == _cache_key and not _force_refresh:
        status = _cached["status"]
    elif _force_refresh:
        status = run(_get_github_status(slack_user_id, slack_team_id))
        st.session_state["_gh_status_cache"] = {"key": _cache_key, "status": status}
    else:
        with loading_section("Checking connection status…", n_skeleton_lines=2):
            status = run(_get_github_status(slack_user_id, slack_team_id))
        st.session_state["_gh_status_cache"] = {"key": _cache_key, "status": status}

    async def _save_login(login: str) -> str:
        async with AsyncSessionLocal() as db:
            link = await link_github_login(
                db=db,
                slack_user_id=slack_user_id,
                slack_team_id=slack_team_id,
                github_login=login,
            )
            await db.commit()
            return link.github_login

    if status["github_connected"]:
        gh_slot = st.empty()
        with gh_slot.container():
            col1, col2 = st.columns([4, 1])
            col1.success(f"✅ Linked to GitHub login **@{status['github_login']}**")
            disconnect_clicked = col2.button("Disconnect", key="disconnect_github")

        with st.expander("Change GitHub login"):
            new_login = st.text_input(
                "GitHub username",
                key="_gh_login_update",
                placeholder="octocat",
            )
            if st.button("Save", key="_gh_login_save_update"):
                try:
                    with st.spinner("Validating with GitHub…"):
                        login = run(_save_login(new_login))
                    st.session_state.pop("_gh_status_cache", None)
                    st.success(f"✅ Updated — now linked to **@{login}**")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed: {e}")

        if disconnect_clicked:
            gh_slot.empty()
            with gh_slot.container():
                with st.spinner("Removing GitHub link…"):
                    run(_disconnect_github(slack_user_id, slack_team_id))
            st.session_state.pop("_gh_status_cache", None)
            st.session_state["_gh_just_disconnected"] = True
            st.rerun()
    else:
        st.info(
            "Enter your **GitHub username** so the app knows whose PRs and "
            "reviews to fetch using the server PAT."
        )
        login = st.text_input(
            "GitHub username",
            key="_gh_login_new",
            placeholder="octocat",
            disabled=not _server_pat_set,
        )
        if st.button(
            "Link GitHub",
            type="primary",
            key="_gh_login_save_new",
            disabled=not _server_pat_set,
        ):
            if not login.strip():
                st.warning("Enter a GitHub username first.")
            else:
                try:
                    with st.spinner("Validating with GitHub…"):
                        login_saved = run(_save_login(login))
                    st.session_state.pop("_gh_status_cache", None)
                    st.success(f"✅ Linked to **@{login_saved}**")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed: {e}")

st.markdown("---")

# ─── Help ─────────────────────────────────────────────────────────────────────

with st.expander("How does this work?"):
    st.markdown("""
    **Slack** uses Sign in with Slack (user OAuth). We request read-only access
    to public channels you're a member of. No messages are read in real time —
    data is pulled on demand when you trigger a sync.

    **GitHub** uses a single server-wide Personal Access Token (PAT) configured
    via the `GITHUB_PAT` env var / Streamlit secret. Needed scopes: `repo` +
    `read:org`. Rotate by updating the secret and rebooting.

    No PAT is stored in the database. This page only records the
    slack→github_login mapping — used as a routing key for Search API queries.

    Neither connection shares your data with third parties.
    All data is stored in your own database.
    """)
