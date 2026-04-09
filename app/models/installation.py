from datetime import datetime
from sqlalchemy import String, DateTime, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class SlackInstallation(Base):
    """Stores Slack workspace installation data (OAuth tokens, bot tokens)."""

    __tablename__ = "slack_installations"
    __table_args__ = (UniqueConstraint("team_id", "enterprise_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[str] = mapped_column(String(64), index=True)
    team_name: Mapped[str | None] = mapped_column(String(256))
    enterprise_id: Mapped[str | None] = mapped_column(String(64))
    enterprise_name: Mapped[str | None] = mapped_column(String(256))
    bot_token: Mapped[str] = mapped_column(String(512))
    bot_user_id: Mapped[str] = mapped_column(String(64))
    bot_scopes: Mapped[str | None] = mapped_column(Text)
    installer_user_id: Mapped[str | None] = mapped_column(String(64))
    installer_user_token: Mapped[str | None] = mapped_column(String(512))
    installed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def __repr__(self) -> str:
        return f"<SlackInstallation team={self.team_id} name={self.team_name}>"
