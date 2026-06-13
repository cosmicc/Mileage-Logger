"""add trip odometer fields

Revision ID: 20260612_0006
Revises: 20260612_0005
Create Date: 2026-06-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260612_0006"
down_revision: str | None = "20260612_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("trips", sa.Column("start_odometer_miles", sa.Numeric(12, 3), nullable=True))
    op.add_column("trips", sa.Column("end_odometer_miles", sa.Numeric(12, 3), nullable=True))
    op.add_column(
        "trips",
        sa.Column(
            "mileage_source",
            sa.String(length=40),
            nullable=False,
            server_default="waypoint_distance",
        ),
    )
    op.alter_column("trips", "mileage_source", server_default=None)


def downgrade() -> None:
    op.drop_column("trips", "mileage_source")
    op.drop_column("trips", "end_odometer_miles")
    op.drop_column("trips", "start_odometer_miles")
