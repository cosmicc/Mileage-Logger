"""initial schema

Revision ID: 20260611_0001
Revises:
Create Date: 2026-06-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260611_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "owntracks_locations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user", sa.String(length=120), nullable=True),
        sa.Column("device", sa.String(length=120), nullable=True),
        sa.Column("topic", sa.String(length=255), nullable=True),
        sa.Column("tracker_id", sa.String(length=16), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latitude", sa.Numeric(precision=10, scale=7), nullable=False),
        sa.Column("longitude", sa.Numeric(precision=10, scale=7), nullable=False),
        sa.Column("accuracy_m", sa.Integer(), nullable=True),
        sa.Column("battery_percent", sa.Integer(), nullable=True),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
    )
    op.create_index("ix_owntracks_locations_captured_at", "owntracks_locations", ["captured_at"])

    op.create_table(
        "sites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=160), nullable=False, unique=True),
        sa.Column("latitude", sa.Numeric(precision=10, scale=7), nullable=False),
        sa.Column("longitude", sa.Numeric(precision=10, scale=7), nullable=False),
        sa.Column("radius_m", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "trips",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trip_date", sa.Date(), nullable=False),
        sa.Column("origin_site_id", sa.Integer(), sa.ForeignKey("sites.id"), nullable=True),
        sa.Column("destination_site_id", sa.Integer(), sa.ForeignKey("sites.id"), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("start_latitude", sa.Numeric(precision=10, scale=7), nullable=False),
        sa.Column("start_longitude", sa.Numeric(precision=10, scale=7), nullable=False),
        sa.Column("end_latitude", sa.Numeric(precision=10, scale=7), nullable=False),
        sa.Column("end_longitude", sa.Numeric(precision=10, scale=7), nullable=False),
        sa.Column("miles", sa.Numeric(precision=9, scale=2), nullable=False),
        sa.Column("include_in_report", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_trips_trip_date", "trips", ["trip_date"])

    op.create_table(
        "gas_price_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("observed_on", sa.Date(), nullable=False),
        sa.Column("state", sa.String(length=2), nullable=False),
        sa.Column("grade", sa.String(length=40), nullable=False),
        sa.Column("price_per_gallon", sa.Numeric(precision=6, scale=3), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("source_detail", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("observed_on", "state", "grade", "source", name="uq_gas_snapshot"),
    )

    op.create_table(
        "monthly_gas_prices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=2), nullable=False),
        sa.Column("average_price_per_gallon", sa.Numeric(precision=6, scale=3), nullable=False),
        sa.Column("buffer_per_gallon", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("effective_rate", sa.Numeric(precision=6, scale=3), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("source_detail", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("year", "month", "state", name="uq_monthly_gas_price"),
    )

    op.create_table(
        "monthly_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("total_miles", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column(
            "gas_price_id",
            sa.Integer(),
            sa.ForeignKey("monthly_gas_prices.id"),
            nullable=False,
        ),
        sa.Column("reimbursement_total", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("pdf_path", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("year", "month", name="uq_monthly_report"),
    )


def downgrade() -> None:
    op.drop_table("monthly_reports")
    op.drop_table("monthly_gas_prices")
    op.drop_table("gas_price_snapshots")
    op.drop_index("ix_trips_trip_date", table_name="trips")
    op.drop_table("trips")
    op.drop_table("sites")
    op.drop_index("ix_owntracks_locations_captured_at", table_name="owntracks_locations")
    op.drop_table("owntracks_locations")
