"""add composite indexes for sync dedup and team overview

Revision ID: c4d2e5f6a7b8
Revises: b3f1c2d4e5a6
Create Date: 2026-04-16

Adds indexes to speed up:
  - Batch dedup query in slack_ingester (channel_id + timestamp)
  - Team Overview GitHub login lookup (slack_user_id + slack_team_id)
"""

from alembic import op

revision = "c4d2e5f6a7b8"
down_revision = "b3f1c2d4e5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_slack_messages_channel_ts",
        "slack_messages",
        ["channel_id", "timestamp"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_github_links_user_team",
        "user_github_links",
        ["slack_user_id", "slack_team_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_github_links_user_team", table_name="user_github_links")
    op.drop_index("ix_slack_messages_channel_ts", table_name="slack_messages")
