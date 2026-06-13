"""remove monthly reports

Revision ID: 20260612_0007
Revises: 20260612_0006
Create Date: 2026-06-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260612_0007"
down_revision: str | None = "20260612_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("monthly_reports")


def downgrade() -> None:
    op.create_table(
        "monthly_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("total_miles", sa.Numeric(10, 2), nullable=False),
        sa.Column("gas_price_id", sa.Integer(), nullable=False),
        sa.Column("reimbursement_total", sa.Numeric(10, 2), nullable=False),
        sa.Column("pdf_path", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["gas_price_id"], ["monthly_gas_prices.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
