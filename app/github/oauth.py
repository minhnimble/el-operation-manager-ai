"""
GitHub OAuth flow.

HTTP calls use synchronous httpx to avoid DNS resolution issues
when called from Streamlit's async/sync mixed context.
"""

import logging

import requests
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import get_settings
from app.models.user import User, UserGitHubLink

logger = logging.getLogger(__name__)
settings = get_settings()


def exchange_code_for_token(code: str) -> dict:
    """Exchange OAuth code for GitHub access token."""
    resp = requests.post(
        "https://github.com/login/oauth/access_token",
        json={
            "client_id": settings.github_client_id,
            "client_secret": settings.github_client_secret,
            "code": code,
        },
        headers={"Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_github_user(access_token: str) -> dict:
    """Fetch authenticated GitHub user profile."""
    resp = requests.get(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


async def link_github_to_user(
    db: AsyncSession,
    slack_user_id: str,
    slack_team_id: str,
    code: str,
) -> UserGitHubLink:
    """Exchange OAuth code, fetch GitHub profile, store link (async — DB only)."""
    # Sync HTTP calls — safe to call from async context
    token_data = exchange_code_for_token(code)
    access_token = token_data.get("access_token")
    if not access_token:
        raise ValueError(f"GitHub OAuth error: {token_data.get('error_description')}")

    gh_user = get_github_user(access_token)

    # Ensure User row exists
    user_result = await db.execute(
        select(User).where(User.slack_user_id == slack_user_id)
    )
    if not user_result.scalar_one_or_none():
        db.add(User(
            slack_user_id=slack_user_id,
            slack_team_id=slack_team_id,
            opted_in=True,
        ))
        await db.flush()

    # Upsert GitHub link
    link_result = await db.execute(
        select(UserGitHubLink).where(
            UserGitHubLink.slack_user_id == slack_user_id,
            UserGitHubLink.slack_team_id == slack_team_id,
        )
    )
    link = link_result.scalar_one_or_none()

    if link:
        link.github_user_id = gh_user["id"]
        link.github_login = gh_user["login"]
        link.github_access_token = access_token
        link.github_token_scope = token_data.get("scope", "")
    else:
        link = UserGitHubLink(
            slack_user_id=slack_user_id,
            slack_team_id=slack_team_id,
            github_user_id=gh_user["id"],
            github_login=gh_user["login"],
            github_access_token=access_token,
            github_token_scope=token_data.get("scope", ""),
        )
        db.add(link)

    await db.flush()
    logger.info("Linked GitHub %s to Slack user %s", gh_user["login"], slack_user_id)
    return link
