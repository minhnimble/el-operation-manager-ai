"""drop github_access_token and github_token_scope from user_github_links

Revision ID: d5a8b1c0e9f2
Revises: c4d2e5f6a7b8
Create Date: 2026-04-22

GitHub auth migrated to a single server-wide PAT (`GITHUB_PAT` env/secret).
Per-user PAT storage in the database is no longer used — drop the columns to
remove the secret-at-rest surface.

`github_login` is kept; it's the routing key used by the Search API.
"""

import sqlalchemy as sa
from alembic import op

revision = "d5a8b1c0e9f2"
down_revision = "c4d2e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user_github_links") as batch:
        batch.drop_column("github_access_token")
        batch.drop_column("github_token_scope")


def downgrade() -> None:
    with op.batch_alter_table("user_github_links") as batch:
        batch.add_column(sa.Column("github_token_scope", sa.String(length=512), nullable=True))
        batch.add_column(sa.Column("github_access_token", sa.String(length=512), nullable=True))
