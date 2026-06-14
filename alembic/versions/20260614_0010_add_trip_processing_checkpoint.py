"""add trip processing checkpoint

Revision ID: 20260614_0010
Revises: 20260612_0009
Create Date: 2026-06-14
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260614_0010"
down_revision: str | None = "20260612_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("trip_processing_checkpoints"):
        return

    op.create_table(
        "trip_processing_checkpoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("last_owntracks_location_id", sa.Integer(), nullable=True),
        sa.Column("odometer_anchor_miles", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("odometer_anchor_recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", name="uq_trip_processing_checkpoint_name"),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("trip_processing_checkpoints"):
        return

    op.drop_table("trip_processing_checkpoints")
