"""
Slack user OAuth flow — Sign in with Slack.

Flow:
  1. User visits /auth/slack  → redirect to Slack authorization page
  2. Slack redirects to /auth/slack/callback?code=...
  3. Exchange code for user access token
  4. Store token → use it to query Slack APIs on behalf of the user
"""

import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import get_settings
from app.models.slack_token import SlackUserToken
from app.models.user import User

logger = logging.getLogger(__name__)
settings = get_settings()

SLACK_OAUTH_URL = "https://slack.com/oauth/v2/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
SLACK_USER_INFO_URL = "https://slack.com/api/users.identity"

# Scopes needed to read channel history and user info
USER_SCOPES = [
    "channels:history",
    "channels:read",
    "users:read",
    "users:read.email",
    "groups:history",   # private channels the user is in (optional)
    "identity.basic",
    "identity.email",
]


def build_auth_url(state: str) -> str:
    # Prefix with "slack:" so the callback page can tell Slack apart from GitHub
    scopes = ",".join(USER_SCOPES)
    return (
        f"{SLACK_OAUTH_URL}"
        f"?client_id={settings.slack_client_id}"
        f"&user_scope={scopes}"
        f"&state=slack:{state}"
        f"&redirect_uri={settings.app_base_url}"
    )


async def exchange_code(code: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            SLACK_TOKEN_URL,
            data={
                "client_id": settings.slack_client_id,
                "client_secret": settings.slack_client_secret,
                "code": code,
                "redirect_uri": f"{settings.app_base_url}/auth/slack/callback",
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise ValueError(f"Slack OAuth error: {data.get('error')}")
        return data


async def get_user_identity(user_token: str) -> dict:
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {user_token}"}
    ) as client:
        resp = await client.get(SLACK_USER_INFO_URL, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise ValueError(f"Slack identity error: {data.get('error')}")
        return data


async def save_slack_token(
    db: AsyncSession, token_data: dict
) -> SlackUserToken:
    """Persist the token and upsert the User record."""
    authed_user = token_data.get("authed_user", {})
    user_token = authed_user.get("access_token")
    slack_user_id = authed_user.get("id")
    team_id = token_data.get("team", {}).get("id", "")
    team_name = token_data.get("team", {}).get("name")

    if not user_token or not slack_user_id:
        raise ValueError("Missing user token or user ID in OAuth response")

    # Fetch identity for display name / email
    identity = {}
    try:
        identity = await get_user_identity(user_token)
    except Exception as e:
        logger.warning("Could not fetch Slack identity: %s", e)

    display_name = identity.get("user", {}).get("name")
    email = identity.get("user", {}).get("email")

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
        token_record.token_scopes = ",".join(authed_user.get("scope", "").split(","))
        token_record.slack_team_name = team_name
        token_record.slack_display_name = display_name
        token_record.slack_email = email
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
        user.slack_email = email or user.slack_email
        user.slack_team_id = team_id
        user.opted_in = True
    else:
        db.add(User(
            slack_user_id=slack_user_id,
            slack_team_id=team_id,
            slack_display_name=display_name,
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
