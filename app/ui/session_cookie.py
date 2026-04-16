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
* On login:   `set_session_cookie(user_id, team_id, display_name)` writes a
              signed, time-stamped cookie via a tiny JS snippet injected into
              the page.

* On every page load: `restore_session_from_cookie()` reads the cookie via
              `st.context.cookies` (available server-side in Streamlit 1.40+),
              verifies the signature, and re-populates session_state if the
              three Slack keys are absent.

* On logout:  `clear_session_cookie()` overwrites the cookie with an empty
              max-age=0 value to delete it.

Security
--------
The cookie value is signed with itsdangerous.URLSafeTimedSerializer using the
app's SECRET_KEY.  Tampering with the value (or the user_id field) invalidates
the signature and is silently ignored.  The token expires after SESSION_MAX_AGE
seconds (default: 30 days) so stolen cookies become useless eventually.

The cookie is marked SameSite=Lax; no Secure flag is added here because
Streamlit Cloud terminates TLS at the load balancer and serves the app over
plain HTTP internally — browsers already see HTTPS at the outer layer.
"""

from __future__ import annotations

import logging

import streamlit as st
import streamlit.components.v1 as components
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

logger = logging.getLogger(__name__)

COOKIE_NAME = "elm_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days in seconds
_SEP = "|"


def _secret() -> str:
    """Return the signing secret from app config.

    Falls back to a fixed fallback string in dev so the module works without
    a full config setup.  In production the SECRET_KEY env var must be set.
    """
    try:
        from app.config import get_settings
        s = get_settings().app_secret_key
        if s and s != "change-me":
            return s
    except Exception:
        pass
    return "dev-only-insecure-fallback-key"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret(), salt="elm-slack-session")


# ── Write ─────────────────────────────────────────────────────────────────────

def set_session_cookie(
    slack_user_id: str,
    slack_team_id: str,
    slack_display_name: str,
) -> None:
    """Persist the Slack identity in a signed browser cookie.

    Uses a 0-height components.html block to set the cookie via JS.
    Must be called after a successful Slack OAuth exchange, before
    st.switch_page / st.rerun.
    """
    payload = _SEP.join([slack_user_id, slack_team_id, slack_display_name])
    signed = _serializer().dumps(payload)
    # Escape single-quotes in the signed token (base64url is safe, but belt+suspenders)
    signed_safe = signed.replace("'", "\\'")
    js = (
        f"document.cookie = '{COOKIE_NAME}={signed_safe}; "
        f"path=/; max-age={SESSION_MAX_AGE}; SameSite=Lax';"
    )
    # height=0 makes the iframe invisible; the JS still executes.
    components.html(f"<script>{js}</script>", height=0)
    logger.debug("Session cookie set for user %s", slack_user_id)


def clear_session_cookie() -> None:
    """Delete the session cookie by setting max-age=0."""
    js = (
        f"document.cookie = '{COOKIE_NAME}=; "
        f"path=/; max-age=0; SameSite=Lax';"
    )
    components.html(f"<script>{js}</script>", height=0)
    logger.debug("Session cookie cleared")


# ── Read ──────────────────────────────────────────────────────────────────────

def restore_session_from_cookie() -> bool:
    """Re-populate session_state from the signed cookie if the session is empty.

    Returns True if the session was successfully restored, False otherwise.
    Safe to call on every page load — it's a no-op when session_state already
    has the Slack keys (avoids pointless crypto work on every Streamlit rerun).
    """
    # Already populated — nothing to do.
    if st.session_state.get("slack_user_id"):
        return True

    try:
        raw = st.context.cookies.get(COOKIE_NAME, "")
    except Exception:
        return False

    if not raw:
        return False

    try:
        payload = _serializer().loads(raw, max_age=SESSION_MAX_AGE)
    except SignatureExpired:
        logger.info("Session cookie expired — user must re-login")
        return False
    except BadSignature:
        logger.warning("Session cookie has invalid signature — ignoring")
        return False
    except Exception as e:
        logger.warning("Unexpected cookie parse error: %s", e)
        return False

    parts = payload.split(_SEP, 2)
    if len(parts) != 3:
        logger.warning("Session cookie payload malformed")
        return False

    uid, tid, name = parts
    if not uid or not tid:
        return False

    st.session_state["slack_user_id"] = uid
    st.session_state["slack_team_id"] = tid
    st.session_state["slack_display_name"] = name
    logger.debug("Session restored from cookie for user %s", uid)
    return True
