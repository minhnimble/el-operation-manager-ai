from datetime import datetime
from sqlalchemy import String, DateTime, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    slack_user_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    slack_team_id: Mapped[str] = mapped_column(String(64), index=True)
    slack_display_name: Mapped[str | None] = mapped_column(String(256))
    slack_real_name: Mapped[str | None] = mapped_column(String(256))
    slack_email: Mapped[str | None] = mapped_column(String(256))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    opted_in: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    github_link: Mapped["UserGitHubLink | None"] = relationship(
        back_populates="user", uselist=False
    )

    def __repr__(self) -> str:
        return f"<User slack={self.slack_user_id} name={self.slack_display_name}>"


class UserGitHubLink(Base):
    """Maps a Slack user to their GitHub handle and stores their OAuth token."""

    __tablename__ = "user_github_links"
    __table_args__ = (UniqueConstraint("slack_user_id", "slack_team_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    slack_user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.slack_user_id"), index=True
    )
    slack_team_id: Mapped[str] = mapped_column(String(64))
    github_user_id: Mapped[int | None] = mapped_column()
    github_login: Mapped[str | None] = mapped_column(String(256))
    github_access_token: Mapped[str | None] = mapped_column(String(512))
    github_token_scope: Mapped[str | None] = mapped_column(String(512))
    linked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped[User] = relationship(back_populates="github_link")
