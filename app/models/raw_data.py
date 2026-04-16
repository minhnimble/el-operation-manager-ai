"""
Raw data tables — store original payloads before normalization.
Useful for debugging, re-processing, and audit trails.
"""

from datetime import datetime
from typing import Any
from sqlalchemy import String, DateTime, Text, Boolean, JSON, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class SlackMessage(Base):
    __tablename__ = "slack_messages"
    __table_args__ = (
        Index("ix_slack_messages_user_ts", "slack_user_id", "message_ts"),
        Index("ix_slack_messages_channel", "channel_id", "message_ts"),
        Index("ix_slack_messages_channel_ts", "channel_id", "timestamp"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    slack_team_id: Mapped[str] = mapped_column(String(64), index=True)
    slack_user_id: Mapped[str] = mapped_column(String(64), index=True)
    channel_id: Mapped[str] = mapped_column(String(64))
    channel_name: Mapped[str | None] = mapped_column(String(256))
    message_ts: Mapped[str] = mapped_column(String(64), unique=True)
    thread_ts: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str | None] = mapped_column(Text)
    is_standup_channel: Mapped[bool] = mapped_column(Boolean, default=False)
    is_thread_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)


class GitHubActivity(Base):
    __tablename__ = "github_activities"
    __table_args__ = (
        Index("ix_github_activities_user_ts", "github_login", "activity_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    slack_team_id: Mapped[str] = mapped_column(String(64), index=True)
    slack_user_id: Mapped[str] = mapped_column(String(64), index=True)
    github_login: Mapped[str] = mapped_column(String(256), index=True)
    activity_type: Mapped[str] = mapped_column(String(64))  # commit, pr, review, issue
    repo_full_name: Mapped[str] = mapped_column(String(256))
    ref_id: Mapped[str] = mapped_column(String(256))  # sha / pr_number / issue_number
    title: Mapped[str | None] = mapped_column(String(512))
    url: Mapped[str | None] = mapped_column(String(1024))
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    activity_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
