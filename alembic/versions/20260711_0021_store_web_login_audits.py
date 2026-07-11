"""Store web login audits in the application database.

Revision ID: 20260711_0021
Revises: 20260711_0020
Create Date: 2026-07-11 12:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260711_0021"
down_revision: str | None = "20260711_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the database-backed web login audit table."""

    op.create_table(
        "web_login_audits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entry_id", sa.String(length=64), nullable=False),
        sa.Column("event", sa.String(length=40), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_web_login_audits_entry_id",
        "web_login_audits",
        ["entry_id"],
        unique=True,
    )
    op.create_index(
        "ix_web_login_audits_event",
        "web_login_audits",
        ["event"],
        unique=False,
    )
    op.create_index(
        "ix_web_login_audits_occurred_at",
        "web_login_audits",
        ["occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_web_login_audits_event_occurred_at",
        "web_login_audits",
        ["event", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove database-backed web login audit storage."""

    op.drop_table("web_login_audits")
