from datetime import datetime
from sqlalchemy import String, DateTime, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TeamMember(Base):
    """Records which Slack users an engineering manager is tracking.

    The *manager* is the user who signed in with Slack (the EM).
    The *member* is any workspace user the EM chose to add to their team.
    Members do NOT need to sign in — their messages are captured automatically
    when the EM syncs, because SlackIngester stores all message authors.
    """

    __tablename__ = "team_members"
    __table_args__ = (
        UniqueConstraint("manager_slack_user_id", "member_slack_user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # The EM who owns this team
    manager_slack_user_id: Mapped[str] = mapped_column(String(64), index=True)
    manager_slack_team_id: Mapped[str] = mapped_column(String(64), index=True)

    # The team member being tracked
    member_slack_user_id: Mapped[str] = mapped_column(String(64), index=True)
    member_slack_team_id: Mapped[str] = mapped_column(String(64))
    member_display_name: Mapped[str | None] = mapped_column(String(256))
    member_real_name: Mapped[str | None] = mapped_column(String(256))
    member_avatar_url: Mapped[str | None] = mapped_column(String(512))

    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def display(self) -> str:
        return (
            self.member_display_name
            or self.member_real_name
            or self.member_slack_user_id
        )

    def __repr__(self) -> str:
        return f"<TeamMember manager={self.manager_slack_user_id} member={self.member_slack_user_id}>"
