"""add trip location names

Revision ID: 20260611_0002
Revises: 20260611_0001
Create Date: 2026-06-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260611_0002"
down_revision: str | None = "20260611_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("trips", sa.Column("origin_name", sa.String(length=160), nullable=True))
    op.add_column("trips", sa.Column("destination_name", sa.String(length=160), nullable=True))

    op.get_bind().execute(
        sa.text(
            """
            UPDATE trips
            SET
                origin_name = COALESCE(
                    (SELECT sites.name FROM sites WHERE sites.id = trips.origin_site_id),
                    'Unknown'
                ),
                destination_name = COALESCE(
                    (SELECT sites.name FROM sites WHERE sites.id = trips.destination_site_id),
                    'Unknown'
                )
            """
        )
    )


def downgrade() -> None:
    op.drop_column("trips", "destination_name")
    op.drop_column("trips", "origin_name")
