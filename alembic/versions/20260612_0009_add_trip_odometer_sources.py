"""add trip odometer sources

Revision ID: 20260612_0009
Revises: 20260612_0008
Create Date: 2026-06-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260612_0009"
down_revision: str | None = "20260612_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("trips", sa.Column("start_odometer_source", sa.String(length=40), nullable=True))
    op.add_column("trips", sa.Column("end_odometer_source", sa.String(length=40), nullable=True))
    op.execute(
        """
        UPDATE trips
        SET
            start_odometer_source = CASE
                WHEN start_odometer_miles IS NULL THEN NULL
                WHEN mileage_source = 'fordpass_odometer' THEN 'fordpass'
                WHEN mileage_source = 'estimated_odometer' THEN 'estimated'
                WHEN mileage_source = 'manual' THEN 'manual'
                ELSE NULL
            END,
            end_odometer_source = CASE
                WHEN end_odometer_miles IS NULL THEN NULL
                WHEN mileage_source = 'fordpass_odometer' THEN 'fordpass'
                WHEN mileage_source = 'estimated_odometer' THEN 'estimated'
                WHEN mileage_source = 'manual' THEN 'manual'
                ELSE NULL
            END
        """
    )


def downgrade() -> None:
    op.drop_column("trips", "end_odometer_source")
    op.drop_column("trips", "start_odometer_source")
