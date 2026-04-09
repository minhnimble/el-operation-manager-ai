"""Stores Slack user OAuth tokens obtained via Sign in with Slack."""

from datetime import datetime
from sqlalchemy import String, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class SlackUserToken(Base):
    __tablename__ = "slack_user_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    slack_user_id: Mapped[str] = mapped_column(String(64), index=True)
    slack_team_id: Mapped[str] = mapped_column(String(64), index=True)
    slack_team_name: Mapped[str | None] = mapped_column(String(256))
    slack_display_name: Mapped[str | None] = mapped_column(String(256))
    slack_email: Mapped[str | None] = mapped_column(String(256))
    access_token: Mapped[str] = mapped_column(String(512))
    token_scopes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
