"""add work_unit dedup constraints

Revision ID: b3f1c2d4e5a6
Revises: a0396ef840cb
Create Date: 2026-04-15

Adds unique constraints on work_units to prevent duplicates when sync runs
multiple times on the same day:
  - Slack: unique on slack_message_ts (one WorkUnit per Slack message)
  - GitHub: unique on (github_ref_id, github_repo, type) (one WorkUnit per activity)
"""

from alembic import op
import sqlalchemy as sa

revision = "b3f1c2d4e5a6"
down_revision = "a0396ef840cb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Remove any duplicate Slack WorkUnits before adding the constraint,
    # keeping the lowest id for each slack_message_ts.
    op.execute("""
        DELETE FROM work_units
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM work_units
            WHERE slack_message_ts IS NOT NULL
            GROUP BY slack_message_ts
        )
        AND slack_message_ts IS NOT NULL
    """)

    # Remove any duplicate GitHub WorkUnits before adding the constraint.
    op.execute("""
        DELETE FROM work_units
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM work_units
            WHERE github_ref_id IS NOT NULL
              AND github_repo IS NOT NULL
            GROUP BY github_ref_id, github_repo, type
        )
        AND github_ref_id IS NOT NULL
        AND github_repo IS NOT NULL
    """)

    op.create_index(
        "uq_work_units_slack_message_ts",
        "work_units",
        ["slack_message_ts"],
        unique=True,
        postgresql_where=sa.text("slack_message_ts IS NOT NULL"),
    )

    op.create_index(
        "uq_work_units_github_ref",
        "work_units",
        ["github_ref_id", "github_repo", "type"],
        unique=True,
        postgresql_where=sa.text("github_ref_id IS NOT NULL AND github_repo IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_work_units_github_ref", table_name="work_units")
    op.drop_index("uq_work_units_slack_message_ts", table_name="work_units")
