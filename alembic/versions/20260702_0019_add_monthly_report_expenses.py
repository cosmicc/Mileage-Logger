"""Add monthly report extra expenses.

Revision ID: 20260702_0019
Revises: 20260701_0018
Create Date: 2026-07-02 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260702_0019"
down_revision: str | None = "20260701_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the manual monthly PDF expense table."""

    op.create_table(
        "monthly_report_expenses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "expense_date",
            sa.Date(),
            nullable=False,
            comment="Local expense date used to select the monthly PDF report.",
        ),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column(
            "reason",
            sa.String(length=160),
            nullable=False,
            comment="Operator-entered expense reason shown on the monthly PDF report.",
        ),
        sa.Column(
            "amount",
            sa.Numeric(precision=10, scale=2),
            nullable=False,
            comment="Expense amount added to the monthly reimbursement total.",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_monthly_report_expenses_expense_date"),
        "monthly_report_expenses",
        ["expense_date"],
        unique=False,
    )
    op.create_index(
        "ix_monthly_report_expenses_year_month",
        "monthly_report_expenses",
        ["year", "month"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the manual monthly PDF expense table."""

    op.drop_index("ix_monthly_report_expenses_year_month", table_name="monthly_report_expenses")
    op.drop_index(
        op.f("ix_monthly_report_expenses_expense_date"),
        table_name="monthly_report_expenses",
    )
    op.drop_table("monthly_report_expenses")
