"""
Slack user OAuth flow — Sign in with Slack (OAuth v2).

HTTP calls use synchronous httpx to avoid DNS resolution issues
when called from Streamlit's async/sync mixed context.
"""

import logging
from urllib.parse import quote

import requests
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import get_settings
from app.models.slack_token import SlackUserToken
from app.models.user import User

logger = logging.getLogger(__name__)
settings = get_settings()

SLACK_OAUTH_URL = "https://slack.com/oauth/v2/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
SLACK_USERS_INFO_URL = "https://slack.com/api/users.info"

USER_SCOPES = [
    "channels:history",   # read public channel messages
    "channels:read",      # list public channels
    "groups:history",     # read private channel messages
    "groups:read",        # list private channels
    "users:read",         # resolve user profiles
    "users:read.email",   # resolve user emails
]


def build_auth_url(state: str) -> str:
    scopes = ",".join(USER_SCOPES)
    return (
        f"{SLACK_OAUTH_URL}"
        f"?client_id={settings.slack_client_id}"
        f"&user_scope={scopes}"
        f"&state=slack:{state}"
        f"&redirect_uri={quote(settings.app_base_url, safe='')}"
    )


def exchange_code(code: str) -> dict:
    """Exchange OAuth code for user token."""
    resp = requests.post(
        SLACK_TOKEN_URL,
        data={
            "client_id": settings.slack_client_id,
            "client_secret": settings.slack_client_secret,
            "code": code,
            "redirect_uri": settings.app_base_url,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise ValueError(f"Slack OAuth error: {data.get('error')}")
    return data


def get_user_info(user_token: str, user_id: str) -> dict:
    """Fetch user profile via users.info."""
    resp = requests.get(
        SLACK_USERS_INFO_URL,
        params={"user": user_id},
        headers={"Authorization": f"Bearer {user_token}"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise ValueError(f"Slack users.info error: {data.get('error')}")
    return data.get("user", {})


async def save_slack_token(
    db: AsyncSession, token_data: dict
) -> SlackUserToken:
    """Persist the token and upsert the User record (async — DB only)."""
    authed_user = token_data.get("authed_user", {})
    user_token = authed_user.get("access_token")
    slack_user_id = authed_user.get("id")
    team_id = token_data.get("team", {}).get("id", "")
    team_name = token_data.get("team", {}).get("name")

    if not user_token or not slack_user_id:
        raise ValueError("Missing user token or user ID in OAuth response")

    # Sync HTTP call — safe to call from async context
    display_name = None
    real_name = None
    email = None
    try:
        user_info = get_user_info(user_token, slack_user_id)
        profile = user_info.get("profile", {})
        display_name = (
            profile.get("display_name")
            or profile.get("real_name")
            or user_info.get("name")
        )
        real_name = profile.get("real_name") or display_name
        email = profile.get("email")
    except Exception as e:
        logger.warning("Could not fetch Slack user info: %s", e)

    # Upsert SlackUserToken
    result = await db.execute(
        select(SlackUserToken).where(
            SlackUserToken.slack_user_id == slack_user_id,
            SlackUserToken.slack_team_id == team_id,
        )
    )
    token_record = result.scalar_one_or_none()

    if token_record:
        token_record.access_token = user_token
        token_record.token_scopes = authed_user.get("scope", "")
        token_record.slack_team_name = team_name
        token_record.slack_display_name = display_name or token_record.slack_display_name
        token_record.slack_email = email or token_record.slack_email
    else:
        token_record = SlackUserToken(
            slack_user_id=slack_user_id,
            slack_team_id=team_id,
            slack_team_name=team_name,
            slack_display_name=display_name,
            slack_email=email,
            access_token=user_token,
            token_scopes=authed_user.get("scope", ""),
        )
        db.add(token_record)

    # Upsert User
    user_result = await db.execute(
        select(User).where(User.slack_user_id == slack_user_id)
    )
    user = user_result.scalar_one_or_none()
    if user:
        user.slack_display_name = display_name or user.slack_display_name
        user.slack_real_name = real_name or user.slack_real_name
        user.slack_email = email or user.slack_email
        user.slack_team_id = team_id
        user.opted_in = True
    else:
        db.add(User(
            slack_user_id=slack_user_id,
            slack_team_id=team_id,
            slack_display_name=display_name,
            slack_real_name=real_name,
            slack_email=email,
            opted_in=True,
        ))

    await db.flush()
    logger.info("Saved Slack token for user %s (team %s)", slack_user_id, team_id)
    return token_record


async def get_token_for_user(
    db: AsyncSession, slack_user_id: str, slack_team_id: str
) -> str | None:
    result = await db.execute(
        select(SlackUserToken).where(
            SlackUserToken.slack_user_id == slack_user_id,
            SlackUserToken.slack_team_id == slack_team_id,
        )
    )
    record = result.scalar_one_or_none()
    return record.access_token if record else None
