"""add owntracks waypoint region ids

Revision ID: 20260612_0004
Revises: 20260611_0003
Create Date: 2026-06-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260612_0004"
down_revision: str | None = "20260611_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sites", sa.Column("owntracks_region_id", sa.String(length=80), nullable=True))
    op.create_index(
        "ix_sites_owntracks_region_id",
        "sites",
        ["owntracks_region_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_sites_owntracks_region_id", table_name="sites")
    op.drop_column("sites", "owntracks_region_id")
