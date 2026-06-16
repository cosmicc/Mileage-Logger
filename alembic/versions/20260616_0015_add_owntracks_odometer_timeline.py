"""add owntracks odometer timeline

Revision ID: 20260616_0015
Revises: 20260615_0014
Create Date: 2026-06-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260616_0015"
down_revision: str | None = "20260615_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Store the rolling OwnTracks odometer on each processed location row."""

    with op.batch_alter_table("owntracks_locations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "odometer_miles",
                sa.Numeric(12, 1),
                nullable=True,
                comment=(
                    "Rolling OwnTracks-derived odometer value after this location row is "
                    "processed."
                ),
            )
        )
        batch_op.add_column(
            sa.Column(
                "odometer_source",
                sa.String(length=40),
                nullable=True,
                comment="Source label for the rolling odometer value stored on this location row.",
            )
        )


def downgrade() -> None:
    """Remove the stored OwnTracks odometer timeline values."""

    with op.batch_alter_table("owntracks_locations") as batch_op:
        batch_op.drop_column("odometer_source")
        batch_op.drop_column("odometer_miles")
