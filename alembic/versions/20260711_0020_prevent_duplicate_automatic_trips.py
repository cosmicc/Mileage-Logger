"""Prevent duplicate automatic trip generation.

Revision ID: 20260711_0020
Revises: 20260702_0019
Create Date: 2026-07-11 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260711_0020"
down_revision: str | None = "20260702_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Keep the oldest exact automatic trip and enforce its generation signature."""

    op.execute(
        """
        DELETE FROM trips
        WHERE id IN (
            SELECT id
            FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            trip_date,
                            origin_site_id,
                            destination_site_id,
                            miles,
                            start_odometer_miles,
                            end_odometer_miles
                        ORDER BY id ASC
                    ) AS duplicate_rank
                FROM trips
                WHERE
                    source = 'auto'
                    AND origin_site_id IS NOT NULL
                    AND destination_site_id IS NOT NULL
                    AND start_odometer_miles IS NOT NULL
                    AND end_odometer_miles IS NOT NULL
            ) AS ranked_automatic_trips
            WHERE duplicate_rank > 1
        )
        """
    )
    op.execute(
        """
        DELETE FROM trips
        WHERE id IN (
            SELECT id
            FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            origin_site_id,
                            destination_site_id,
                            started_at,
                            ended_at
                        ORDER BY id ASC
                    ) AS duplicate_rank
                FROM trips
                WHERE
                    source = 'auto'
                    AND origin_site_id IS NOT NULL
                    AND destination_site_id IS NOT NULL
            ) AS ranked_automatic_trips
            WHERE duplicate_rank > 1
        )
        """
    )
    op.create_index(
        "uq_trips_auto_generation_signature",
        "trips",
        ["origin_site_id", "destination_site_id", "started_at", "ended_at"],
        unique=True,
        postgresql_where=sa.text("source = 'auto'"),
        sqlite_where=sa.text("source = 'auto'"),
    )
    op.create_index(
        "uq_trips_auto_recorded_values",
        "trips",
        [
            "trip_date",
            "origin_site_id",
            "destination_site_id",
            "miles",
            "start_odometer_miles",
            "end_odometer_miles",
        ],
        unique=True,
        postgresql_where=sa.text(
            "source = 'auto' AND start_odometer_miles IS NOT NULL "
            "AND end_odometer_miles IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "source = 'auto' AND start_odometer_miles IS NOT NULL "
            "AND end_odometer_miles IS NOT NULL"
        ),
    )


def downgrade() -> None:
    """Remove database-level automatic-trip uniqueness enforcement."""

    op.drop_index("uq_trips_auto_recorded_values", table_name="trips")
    op.drop_index("uq_trips_auto_generation_signature", table_name="trips")
