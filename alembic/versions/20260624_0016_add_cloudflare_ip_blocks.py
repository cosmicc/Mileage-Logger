"""add cloudflare ip blocks

Revision ID: 20260624_0016
Revises: 20260616_0015
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260624_0016"
down_revision: str | None = "20260616_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Track app-managed Cloudflare blocks and hidden Diagnostics login entries."""

    op.create_table(
        "cloudflare_ip_blocks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ip_address", sa.String(length=45), nullable=False),
        sa.Column("cloudflare_rule_id", sa.String(length=80), nullable=False),
        sa.Column(
            "source",
            sa.String(length=40),
            nullable=False,
            comment="How this app-managed Cloudflare IP block was created.",
        ),
        sa.Column("reason", sa.String(length=160), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cloudflare_rule_id"),
    )
    op.create_index(
        op.f("ix_cloudflare_ip_blocks_ip_address"),
        "cloudflare_ip_blocks",
        ["ip_address"],
        unique=True,
    )

    op.create_table(
        "hidden_login_failures",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entry_id", sa.String(length=64), nullable=False),
        sa.Column("client_ip", sa.String(length=45), nullable=False),
        sa.Column("occurred_at_utc", sa.String(length=40), nullable=False),
        sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_hidden_login_failures_entry_id"),
        "hidden_login_failures",
        ["entry_id"],
        unique=True,
    )


def downgrade() -> None:
    """Remove Cloudflare block and hidden login failure tracking tables."""

    op.drop_index(op.f("ix_hidden_login_failures_entry_id"), table_name="hidden_login_failures")
    op.drop_table("hidden_login_failures")
    op.drop_index(op.f("ix_cloudflare_ip_blocks_ip_address"), table_name="cloudflare_ip_blocks")
    op.drop_table("cloudflare_ip_blocks")
