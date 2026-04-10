"""
WorkUnit — the core normalized abstraction.

Every piece of engineering activity (Slack message, commit, PR, review, standup)
gets normalized into a WorkUnit. This is the single source of truth for analytics.
"""

import enum
from datetime import datetime
from typing import Any
from sqlalchemy import String, DateTime, Text, Enum, JSON, Index, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class WorkUnitSource(str, enum.Enum):
    SLACK = "slack"
    GITHUB = "github"


class WorkUnitType(str, enum.Enum):
    # Slack types
    STANDUP = "standup"
    DISCUSSION = "discussion"
    ANNOUNCEMENT = "announcement"
    THREAD_REPLY = "thread_reply"

    # GitHub types
    COMMIT = "commit"
    PR_OPENED = "pr_opened"
    PR_MERGED = "pr_merged"
    PR_REVIEW = "pr_review"
    ISSUE_OPENED = "issue_opened"
    ISSUE_CLOSED = "issue_closed"
    ISSUE_COMMENT = "issue_comment"


class WorkCategory(str, enum.Enum):
    """AI-classified work category."""
    FEATURE = "feature"
    BUG_FIX = "bug_fix"
    ARCHITECTURE = "architecture"
    MENTORSHIP = "mentorship"
    INCIDENT = "incident"
    REVIEW = "review"
    DOCUMENTATION = "documentation"
    PLANNING = "planning"
    OPERATIONAL = "operational"
    UNKNOWN = "unknown"


class WorkUnit(Base):
    __tablename__ = "work_units"
    __table_args__ = (
        Index("ix_work_units_user_ts", "slack_user_id", "timestamp"),
        Index("ix_work_units_team_ts", "slack_team_id", "timestamp"),
        Index("ix_work_units_type", "type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # Identity
    slack_user_id: Mapped[str] = mapped_column(String(64), index=True)
    slack_team_id: Mapped[str] = mapped_column(String(64), index=True)
    github_login: Mapped[str | None] = mapped_column(String(256))

    # Classification
    source: Mapped[WorkUnitSource] = mapped_column(Enum(WorkUnitSource))
    type: Mapped[WorkUnitType] = mapped_column(Enum(WorkUnitType))
    category: Mapped[WorkCategory | None] = mapped_column(
        Enum(WorkCategory), nullable=True
    )

    # Content
    title: Mapped[str | None] = mapped_column(String(512))
    body: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(1024))

    # Source references
    slack_channel_id: Mapped[str | None] = mapped_column(String(64))
    slack_message_ts: Mapped[str | None] = mapped_column(String(64))
    github_repo: Mapped[str | None] = mapped_column(String(256))
    github_ref_id: Mapped[str | None] = mapped_column(String(256))

    # Timing
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # AI extraction results
    ai_extracted_items: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    ai_confidence: Mapped[float | None] = mapped_column(nullable=True)

    # Raw metadata blob — named 'extra_data' to avoid conflict with SQLAlchemy's reserved 'metadata'
    extra_data: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<WorkUnit type={self.type} user={self.slack_user_id} "
            f"ts={self.timestamp}>"
        )
