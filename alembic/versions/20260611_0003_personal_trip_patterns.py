"""add personal trip patterns

Revision ID: 20260611_0003
Revises: 20260611_0002
Create Date: 2026-06-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260611_0003"
down_revision: str | None = "20260611_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "personal_trip_patterns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("origin_site_id", sa.Integer(), sa.ForeignKey("sites.id"), nullable=True),
        sa.Column("destination_site_id", sa.Integer(), sa.ForeignKey("sites.id"), nullable=True),
        sa.Column("origin_name", sa.String(length=160), nullable=False),
        sa.Column("destination_name", sa.String(length=160), nullable=False),
        sa.Column("origin_latitude", sa.Numeric(precision=10, scale=7), nullable=False),
        sa.Column("origin_longitude", sa.Numeric(precision=10, scale=7), nullable=False),
        sa.Column("destination_latitude", sa.Numeric(precision=10, scale=7), nullable=False),
        sa.Column("destination_longitude", sa.Numeric(precision=10, scale=7), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("personal_trip_patterns")
