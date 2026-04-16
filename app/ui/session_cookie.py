"""
Persistent Slack session via a signed browser cookie.

Why this is needed
------------------
Streamlit session_state lives only for the duration of a single WebSocket
connection.  Any full-page navigation — GitHub OAuth redirect, browser
refresh, closing and reopening the tab — creates a new connection and wipes
session_state, so the user appears logged out even though their Slack token
is still in the DB.

How it works
------------
Writing cookies mid-script is impossible in Streamlit — the WebSocket
architecture sends no HTTP response headers after the initial handshake, and
components.html iframes are preempted by st.switch_page / st.rerun before
they render.

Instead we use a **URL-token hand-off**:

1. After Slack OAuth, we embed a short-lived signed token in the redirect URL
   as ?_s=<token> (via `redirect_with_session`).
2. On every page load, `restore_session_from_url_token` checks for ?_s=,
   verifies + unpacks it, populates session_state, then immediately clears
   the query param from the URL — so it's a one-time pass-through, not a
   persistent URL.
3. After that first load the session lives in session_state for the WebSocket
   lifetime.  On the NEXT fresh load (refresh, new tab, OAuth redirect) the
   DB lookup in `restore_session_from_db` refills session_state from the
   SlackUserToken row — no cookie needed.

This means the only "persistent storage" is the existing SlackUserToken table,
which we already write.  The URL token is just a fast hand-off mechanism to
avoid a DB query on every single page render.

Session lifetime
----------------
While the browser tab stays open: session_state keeps everything in memory.
After a refresh / new tab / OAuth redirect: one DB lookup on the first render
refills the session from the stored token.  Users stay logged in as long as
their SlackUserToken row exists (i.e. until they click Disconnect).
"""

from __future__ import annotations

import logging

import streamlit as st
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

logger = logging.getLogger(__name__)

_URL_PARAM = "_s"          # query-param name for the session hand-off token
_TOKEN_MAX_AGE = 120       # seconds — short-lived; only survives one redirect
_SEP = "|"


def _secret() -> str:
    try:
        from app.config import get_settings
        s = get_settings().app_secret_key
        if s and s not in ("change-me", "change-me-in-production"):
            return s
    except Exception:
        pass
    return "dev-only-insecure-fallback-key"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret(), salt="elm-slack-session-v2")


# ── Write: embed token in redirect URL ───────────────────────────────────────

def make_session_token(
    slack_user_id: str,
    slack_team_id: str,
    slack_display_name: str,
) -> str:
    """Return a short-lived signed token encoding the three session fields."""
    payload = _SEP.join([slack_user_id, slack_team_id, slack_display_name])
    return _serializer().dumps(payload)


# ── Read: restore from URL token (one-time, cleared immediately) ─────────────

def restore_session_from_url_token() -> bool:
    """Check for ?_s= in the URL, verify it, and populate session_state.

    Immediately clears the param so it doesn't linger in the browser URL bar
    or get re-processed on the next rerun.

    Returns True if the session was restored from the token.
    """
    if st.session_state.get("slack_user_id"):
        # Already populated — but still clear the param if present
        if _URL_PARAM in st.query_params:
            del st.query_params[_URL_PARAM]
        return True

    token = st.query_params.get(_URL_PARAM, "")
    if not token:
        return False

    # Clear it immediately regardless of validity
    del st.query_params[_URL_PARAM]

    try:
        payload = _serializer().loads(token, max_age=_TOKEN_MAX_AGE)
    except SignatureExpired:
        logger.info("Session URL token expired")
        return False
    except BadSignature:
        logger.warning("Session URL token has invalid signature — ignoring")
        return False
    except Exception as e:
        logger.warning("Session URL token parse error: %s", e)
        return False

    parts = payload.split(_SEP, 2)
    if len(parts) != 3:
        return False

    uid, tid, name = parts
    if not uid or not tid:
        return False

    st.session_state["slack_user_id"] = uid
    st.session_state["slack_team_id"] = tid
    st.session_state["slack_display_name"] = name
    logger.debug("Session restored from URL token for user %s", uid)
    return True


# ── Read: restore from DB (survives browser refresh / new tab) ───────────────

def restore_session_from_db() -> bool:
    """Re-populate session_state from the DB for the most-recently signed-in user.

    Called on every page load when session_state is empty.  Looks up the most
    recent SlackUserToken row for any user — suitable for a single-user or
    small-team setup where the EM is the only one logging in on this browser.

    Returns True if a token was found and session was restored.
    """
    if st.session_state.get("slack_user_id"):
        return True

    try:
        import asyncio
        from sqlalchemy import select
        from app.database import AsyncSessionLocal
        from app.models.slack_token import SlackUserToken

        async def _lookup():
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(SlackUserToken)
                    .order_by(SlackUserToken.id.desc())
                    .limit(1)
                )
                return result.scalar_one_or_none()

        token = asyncio.run(_lookup())
        if not token:
            return False

        st.session_state["slack_user_id"] = token.slack_user_id
        st.session_state["slack_team_id"] = token.slack_team_id
        st.session_state["slack_display_name"] = token.slack_display_name or ""
        logger.debug("Session restored from DB for user %s", token.slack_user_id)
        return True
    except Exception as e:
        logger.debug("DB session restore failed: %s", e)
        return False


def restore_session_from_cookie() -> bool:
    """Entry point called on every page load.

    Tries URL token first (fast, one-time hand-off after OAuth),
    then falls back to DB lookup (survives refresh / new tab).
    """
    if restore_session_from_url_token():
        return True
    return restore_session_from_db()


# ── Compat stubs (no longer needed but kept so imports don't break) ──────────

def set_session_cookie(*args, **kwargs) -> None:  # noqa: ARG001
    """No-op — session persistence is now handled via URL token + DB lookup."""


def clear_session_cookie() -> None:
    """No-op — clearing session means deleting the SlackUserToken row (done in disconnect flow)."""
