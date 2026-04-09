"""
Custom Slack Bolt installation store backed by our PostgreSQL database.
"""

from logging import Logger

from slack_sdk.oauth.installation_store import InstallationStore
from slack_sdk.oauth.installation_store.models.installation import Installation
from sqlalchemy.orm import Session

from app.database import AsyncSessionLocal
from app.models.installation import SlackInstallation
import asyncio
from sqlalchemy import select


class DBInstallationStore(InstallationStore):
    """Synchronous adapter that runs async DB calls in a new event loop."""

    def save(self, installation: Installation) -> None:
        asyncio.run(self._async_save(installation))

    async def _async_save(self, installation: Installation) -> None:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SlackInstallation).where(
                    SlackInstallation.team_id == installation.team_id
                )
            )
            existing = result.scalar_one_or_none()

            if existing:
                existing.bot_token = installation.bot_token or existing.bot_token
                existing.bot_user_id = installation.bot_user_id or existing.bot_user_id
                existing.team_name = installation.team_name or existing.team_name
                existing.installer_user_id = installation.user_id
                existing.installer_user_token = installation.user_token
            else:
                record = SlackInstallation(
                    team_id=installation.team_id,
                    team_name=installation.team_name,
                    enterprise_id=installation.enterprise_id,
                    enterprise_name=installation.enterprise_name,
                    bot_token=installation.bot_token or "",
                    bot_user_id=installation.bot_user_id or "",
                    bot_scopes=",".join(installation.bot_scopes or []),
                    installer_user_id=installation.user_id,
                    installer_user_token=installation.user_token,
                )
                db.add(record)
            await db.commit()

    def find_installation(
        self,
        *,
        enterprise_id: str | None,
        team_id: str | None,
        is_enterprise_install: bool | None = False,
        user_id: str | None = None,
    ) -> Installation | None:
        return asyncio.run(self._async_find(team_id))

    async def _async_find(self, team_id: str | None) -> Installation | None:
        if not team_id:
            return None
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SlackInstallation).where(
                    SlackInstallation.team_id == team_id
                )
            )
            record = result.scalar_one_or_none()
            if not record:
                return None
            return Installation(
                team_id=record.team_id,
                team_name=record.team_name,
                bot_token=record.bot_token,
                bot_user_id=record.bot_user_id,
                enterprise_id=record.enterprise_id,
            )

    def find_bot(self, *, enterprise_id, team_id, is_enterprise_install=None):
        from slack_sdk.oauth.installation_store.models.bot import Bot
        installation = self.find_installation(
            enterprise_id=enterprise_id,
            team_id=team_id,
            is_enterprise_install=is_enterprise_install,
        )
        if not installation:
            return None
        return Bot(
            team_id=installation.team_id,
            bot_token=installation.bot_token,
            bot_user_id=installation.bot_user_id,
        )
