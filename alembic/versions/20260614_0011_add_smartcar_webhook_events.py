"""add smartcar webhook events

Revision ID: 20260614_0011
Revises: 20260614_0010
Create Date: 2026-06-14
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260614_0011"
down_revision: str | None = "20260614_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("smartcar_webhook_events"):
        return

    op.create_table(
        "smartcar_webhook_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(length=80),
            nullable=False,
            comment="Smartcar eventId value used to reject duplicate event processing.",
        ),
        sa.Column(
            "event_type",
            sa.String(length=80),
            nullable=False,
            comment="Smartcar eventType value such as VEHICLE_STATE or VEHICLE_ERROR.",
        ),
        sa.Column(
            "user_id",
            sa.String(length=80),
            nullable=True,
            comment="Smartcar user identifier from data.user.id.",
        ),
        sa.Column(
            "vehicle_id",
            sa.String(length=80),
            nullable=True,
            comment="Smartcar vehicle identifier from data.vehicle.id.",
        ),
        sa.Column(
            "vehicle_make",
            sa.String(length=80),
            nullable=True,
            comment="Vehicle make included in the webhook vehicle object.",
        ),
        sa.Column(
            "vehicle_model",
            sa.String(length=120),
            nullable=True,
            comment="Vehicle model included in the webhook vehicle object.",
        ),
        sa.Column(
            "vehicle_year",
            sa.Integer(),
            nullable=True,
            comment="Vehicle model year included in the webhook vehicle object.",
        ),
        sa.Column(
            "vehicle_mode",
            sa.String(length=40),
            nullable=True,
            comment="Smartcar vehicle mode such as live, test, or simulated.",
        ),
        sa.Column(
            "vehicle_powertrain_type",
            sa.String(length=40),
            nullable=True,
            comment="Vehicle powertrain type such as ICE, HEV, PHEV, or BEV.",
        ),
        sa.Column(
            "webhook_id",
            sa.String(length=80),
            nullable=True,
            comment="Smartcar webhook identifier from meta.webhookId.",
        ),
        sa.Column(
            "webhook_name",
            sa.String(length=160),
            nullable=True,
            comment="Smartcar webhook display name from meta.webhookName.",
        ),
        sa.Column(
            "delivery_id",
            sa.String(length=80),
            nullable=True,
            comment="Smartcar delivery identifier from meta.deliveryId.",
        ),
        sa.Column(
            "delivered_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Timestamp Smartcar says it delivered the webhook event.",
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Timestamp this app accepted the verified webhook event.",
        ),
        sa.Column(
            "odometer_miles",
            sa.Numeric(precision=12, scale=3),
            nullable=True,
            comment="Webhook odometer reading converted to miles for trip mileage.",
        ),
        sa.Column(
            "odometer_raw_value",
            sa.Numeric(precision=14, scale=3),
            nullable=True,
            comment="Original odometer value before unit conversion.",
        ),
        sa.Column(
            "odometer_unit",
            sa.String(length=20),
            nullable=True,
            comment="Original Smartcar odometer unit, usually km or mi.",
        ),
        sa.Column(
            "odometer_recorded_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="OEM or retrieval timestamp attached to the odometer signal.",
        ),
        sa.Column(
            "fuel_percent",
            sa.Numeric(precision=6, scale=2),
            nullable=True,
            comment="Fuel level percentage when Smartcar includes a fuel signal.",
        ),
        sa.Column(
            "fuel_unit",
            sa.String(length=20),
            nullable=True,
            comment="Unit attached to the Smartcar fuel level signal.",
        ),
        sa.Column(
            "is_locked",
            sa.Boolean(),
            nullable=True,
            comment="Door lock state from the Smartcar closure-islocked signal.",
        ),
        sa.Column(
            "is_online",
            sa.Boolean(),
            nullable=True,
            comment="Connectivity state from the Smartcar connectivitystatus-isonline signal.",
        ),
        sa.Column(
            "nickname",
            sa.String(length=160),
            nullable=True,
            comment="Vehicle nickname from the Smartcar vehicle identification signal.",
        ),
        sa.Column(
            "vin",
            sa.String(length=40),
            nullable=True,
            comment="Vehicle VIN from the Smartcar vehicle identification signal.",
        ),
        sa.Column(
            "firmware_version",
            sa.String(length=120),
            nullable=True,
            comment="Current firmware version when Smartcar includes that signal.",
        ),
        sa.Column(
            "triggers",
            sa.JSON(),
            nullable=False,
            comment="Webhook trigger objects from the Smartcar payload.",
        ),
        sa.Column(
            "raw_payload",
            sa.JSON(),
            nullable=False,
            comment="Complete verified Smartcar payload for audit and future parsing.",
        ),
        sa.UniqueConstraint("event_id", name="uq_smartcar_webhook_events_event_id"),
        sa.UniqueConstraint("delivery_id", name="uq_smartcar_webhook_events_delivery_id"),
    )
    op.create_index(
        "ix_smartcar_webhook_events_event_type",
        "smartcar_webhook_events",
        ["event_type"],
    )
    op.create_index(
        "ix_smartcar_webhook_events_vehicle_id",
        "smartcar_webhook_events",
        ["vehicle_id"],
    )
    op.create_index(
        "ix_smartcar_webhook_events_webhook_id",
        "smartcar_webhook_events",
        ["webhook_id"],
    )

    op.create_table(
        "smartcar_webhook_signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "event_id",
            sa.Integer(),
            sa.ForeignKey("smartcar_webhook_events.id", ondelete="CASCADE"),
            nullable=False,
            comment="Database id for the parent Smartcar webhook delivery.",
        ),
        sa.Column(
            "code",
            sa.String(length=160),
            nullable=True,
            comment="Smartcar signal code such as odometer-traveleddistance.",
        ),
        sa.Column(
            "name",
            sa.String(length=160),
            nullable=True,
            comment="Smartcar signal display name such as TraveledDistance.",
        ),
        sa.Column(
            "group",
            sa.String(length=160),
            nullable=True,
            comment="Smartcar signal group such as Odometer or VehicleIdentification.",
        ),
        sa.Column(
            "status",
            sa.String(length=80),
            nullable=True,
            comment="Smartcar signal status value, normally SUCCESS for usable data.",
        ),
        sa.Column(
            "value",
            sa.JSON(),
            nullable=True,
            comment="Signal body.value exactly as Smartcar sent it.",
        ),
        sa.Column(
            "unit",
            sa.String(length=40),
            nullable=True,
            comment="Signal body.unit value when Smartcar includes one.",
        ),
        sa.Column(
            "oem_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="OEM update timestamp from signal meta.oemUpdatedAt.",
        ),
        sa.Column(
            "retrieved_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Smartcar retrieval timestamp from signal meta.retrievedAt.",
        ),
        sa.Column(
            "body",
            sa.JSON(),
            nullable=False,
            comment="Complete Smartcar signal body object.",
        ),
        sa.Column(
            "meta",
            sa.JSON(),
            nullable=False,
            comment="Complete Smartcar signal meta object.",
        ),
        sa.Column(
            "raw_signal",
            sa.JSON(),
            nullable=False,
            comment="Complete Smartcar signal object for audit and future parsing.",
        ),
    )
    op.create_index(
        "ix_smartcar_webhook_signals_event_id",
        "smartcar_webhook_signals",
        ["event_id"],
    )
    op.create_index(
        "ix_smartcar_webhook_signals_code",
        "smartcar_webhook_signals",
        ["code"],
    )
    op.create_index(
        "ix_smartcar_webhook_signals_name",
        "smartcar_webhook_signals",
        ["name"],
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("smartcar_webhook_signals"):
        op.drop_table("smartcar_webhook_signals")
    if inspector.has_table("smartcar_webhook_events"):
        op.drop_table("smartcar_webhook_events")
