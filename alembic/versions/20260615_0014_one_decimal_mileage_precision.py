"""use one decimal mileage precision

Revision ID: 20260615_0014
Revises: 20260615_0013
Create Date: 2026-06-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260615_0014"
down_revision: str | None = "20260615_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _round_existing_values() -> None:
    """Round stored mileage values before narrowing database numeric scales."""

    op.execute("UPDATE trips SET miles = ROUND(miles, 1)")
    op.execute("UPDATE trips SET start_odometer_miles = ROUND(start_odometer_miles, 1)")
    op.execute("UPDATE trips SET end_odometer_miles = ROUND(end_odometer_miles, 1)")
    op.execute("UPDATE deleted_trips SET miles = ROUND(miles, 1)")
    op.execute(
        """
        UPDATE trip_processing_checkpoints
        SET odometer_anchor_miles = ROUND(odometer_anchor_miles, 1)
        """
    )


def upgrade() -> None:
    """Store trip distances and odometers at one decimal place."""

    _round_existing_values()
    with op.batch_alter_table("trips") as batch_op:
        batch_op.alter_column("miles", existing_type=sa.Numeric(9, 2), type_=sa.Numeric(9, 1))
        batch_op.alter_column(
            "start_odometer_miles",
            existing_type=sa.Numeric(12, 3),
            type_=sa.Numeric(12, 1),
        )
        batch_op.alter_column(
            "end_odometer_miles",
            existing_type=sa.Numeric(12, 3),
            type_=sa.Numeric(12, 1),
        )

    with op.batch_alter_table("deleted_trips") as batch_op:
        batch_op.alter_column("miles", existing_type=sa.Numeric(9, 2), type_=sa.Numeric(9, 1))

    with op.batch_alter_table("trip_processing_checkpoints") as batch_op:
        batch_op.alter_column(
            "odometer_anchor_miles",
            existing_type=sa.Numeric(12, 3),
            type_=sa.Numeric(12, 1),
        )


def downgrade() -> None:
    """Restore the earlier numeric scales without recreating rounded precision."""

    with op.batch_alter_table("trip_processing_checkpoints") as batch_op:
        batch_op.alter_column(
            "odometer_anchor_miles",
            existing_type=sa.Numeric(12, 1),
            type_=sa.Numeric(12, 3),
        )

    with op.batch_alter_table("deleted_trips") as batch_op:
        batch_op.alter_column("miles", existing_type=sa.Numeric(9, 1), type_=sa.Numeric(9, 2))

    with op.batch_alter_table("trips") as batch_op:
        batch_op.alter_column(
            "end_odometer_miles",
            existing_type=sa.Numeric(12, 1),
            type_=sa.Numeric(12, 3),
        )
        batch_op.alter_column(
            "start_odometer_miles",
            existing_type=sa.Numeric(12, 1),
            type_=sa.Numeric(12, 3),
        )
        batch_op.alter_column("miles", existing_type=sa.Numeric(9, 1), type_=sa.Numeric(9, 2))
