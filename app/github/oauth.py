"""
GitHub auth — server-wide PAT (`GITHUB_PAT` env/secret).

No per-user tokens stored in the database. The Connect Accounts page only
records the slack→github_login mapping; the actual API calls always use the
server PAT.

Module name kept as `oauth.py` to avoid breaking imports.
"""

import logging

import requests
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import get_settings
from app.models.user import User, UserGitHubLink

logger = logging.getLogger(__name__)


def get_github_user(access_token: str) -> dict:
    """Fetch authenticated GitHub user profile. Validates the token."""
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


def lookup_github_user_by_login(login: str) -> dict:
    """Fetch a public GitHub profile by login using the server PAT."""
    pat = (get_settings().github_pat or "").strip()
    if not pat:
        raise RuntimeError(
            "GITHUB_PAT is not configured. Set it in env / Streamlit secrets."
        )
    resp = requests.get(
        f"https://api.github.com/users/{login}",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10,
    )
    if resp.status_code == 404:
        raise ValueError(f"GitHub user @{login} not found.")
    resp.raise_for_status()
    return resp.json()


async def link_github_login(
    db: AsyncSession,
    slack_user_id: str,
    slack_team_id: str,
    github_login: str,
) -> UserGitHubLink:
    """Validate the GitHub login and store the slack→github mapping.

    No PAT is stored — the server-wide `GITHUB_PAT` is used at sync time.
    """
    github_login = (github_login or "").strip().lstrip("@")
    if not github_login:
        raise ValueError("Empty GitHub login.")

    gh_user = lookup_github_user_by_login(github_login)

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
    else:
        link = UserGitHubLink(
            slack_user_id=slack_user_id,
            slack_team_id=slack_team_id,
            github_user_id=gh_user["id"],
            github_login=gh_user["login"],
        )
        db.add(link)

    await db.flush()
    logger.info("Linked GitHub @%s to Slack user %s", gh_user["login"], slack_user_id)
    return link
