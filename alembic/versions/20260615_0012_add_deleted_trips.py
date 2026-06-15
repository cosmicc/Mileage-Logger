"""add deleted trips

Revision ID: 20260615_0012
Revises: 20260614_0011
Create Date: 2026-06-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260615_0012"
down_revision: str | None = "20260614_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("deleted_trips"):
        return

    op.create_table(
        "deleted_trips",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "deleted_trip_id",
            sa.Integer(),
            nullable=True,
            comment="Original trips.id value for operator audit after the visible row is deleted.",
        ),
        sa.Column(
            "trip_date",
            sa.Date(),
            nullable=False,
            comment="Local trip date for the deleted trip.",
        ),
        sa.Column(
            "origin_site_id",
            sa.Integer(),
            sa.ForeignKey("sites.id"),
            nullable=True,
            comment="Origin waypoint id used in the automatic generation signature.",
        ),
        sa.Column(
            "destination_site_id",
            sa.Integer(),
            sa.ForeignKey("sites.id"),
            nullable=True,
            comment="Destination waypoint id used in the automatic generation signature.",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Trip start timestamp used in the automatic generation signature.",
        ),
        sa.Column(
            "ended_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Trip end timestamp used in the automatic generation signature.",
        ),
        sa.Column(
            "origin_name",
            sa.String(length=160),
            nullable=True,
            comment="Displayed origin name at deletion time.",
        ),
        sa.Column(
            "destination_name",
            sa.String(length=160),
            nullable=True,
            comment="Displayed destination name at deletion time.",
        ),
        sa.Column(
            "miles",
            sa.Numeric(precision=9, scale=2),
            nullable=True,
            comment="Displayed trip miles at deletion time.",
        ),
        sa.Column(
            "source",
            sa.String(length=40),
            nullable=True,
            comment="Trip source at deletion time.",
        ),
        sa.Column(
            "mileage_source",
            sa.String(length=40),
            nullable=True,
            comment="Mileage source at deletion time.",
        ),
        sa.Column(
            "reason",
            sa.String(length=80),
            nullable=False,
            comment="Reason this automatic trip signature should stay suppressed.",
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Timestamp the visible trip row was deleted.",
        ),
        sa.Column(
            "notes",
            sa.Text(),
            nullable=False,
            comment="Trip notes copied at deletion time.",
        ),
        sa.UniqueConstraint(
            "origin_site_id",
            "destination_site_id",
            "started_at",
            "ended_at",
            name="uq_deleted_trip_generation_signature",
        ),
    )
    op.create_index("ix_deleted_trips_trip_date", "deleted_trips", ["trip_date"])
    op.create_index("ix_deleted_trips_started_at", "deleted_trips", ["started_at"])
    op.create_index("ix_deleted_trips_ended_at", "deleted_trips", ["ended_at"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("deleted_trips"):
        op.drop_table("deleted_trips")
