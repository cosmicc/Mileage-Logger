"""add owntracks monthly summaries

Revision ID: 20260701_0018
Revises: 20260627_0017
Create Date: 2026-07-01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260701_0018"
down_revision: str | None = "20260627_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Store monthly OwnTracks-derived totals before raw location rows are purged."""

    op.create_table(
        "owntracks_monthly_summaries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("total_miles", sa.Numeric(12, 1), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("year", "month", name="uq_owntracks_monthly_summary_month"),
    )


def downgrade() -> None:
    """Remove monthly OwnTracks-derived summary totals."""

    op.drop_table("owntracks_monthly_summaries")
