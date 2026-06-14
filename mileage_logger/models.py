from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

UNKNOWN_LOCATION_NAME = "Unknown"
AUTOMATIC_TRIP_PROCESSING_CHECKPOINT = "automatic_trip_processing"


def normalize_location_name(value: str | None) -> str:
    cleaned = (value or "").strip()
    return cleaned or UNKNOWN_LOCATION_NAME


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class OwnTracksLocation(Base):
    __tablename__ = "owntracks_locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user: Mapped[str | None] = mapped_column(String(120), nullable=True)
    device: Mapped[str | None] = mapped_column(String(120), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tracker_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    latitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    longitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    accuracy_m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    battery_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSON)


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    owntracks_region_id: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    latitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    longitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    radius_m: Mapped[int] = mapped_column(Integer, default=150)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_visited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    origin_trips: Mapped[list["Trip"]] = relationship(
        back_populates="origin_site", foreign_keys="Trip.origin_site_id"
    )
    destination_trips: Mapped[list["Trip"]] = relationship(
        back_populates="destination_site", foreign_keys="Trip.destination_site_id"
    )


class Trip(Base):
    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_date: Mapped[date] = mapped_column(Date, index=True)
    origin_site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"), nullable=True)
    destination_site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    start_latitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    start_longitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    end_latitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    end_longitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    origin_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    destination_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    miles: Mapped[Decimal] = mapped_column(Numeric(9, 2))
    start_odometer_miles: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 3), nullable=True
    )
    end_odometer_miles: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    start_odometer_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    end_odometer_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    mileage_source: Mapped[str] = mapped_column(String(40), default="waypoint_distance")
    source: Mapped[str] = mapped_column(String(40), default="auto")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    origin_site: Mapped[Site | None] = relationship(
        back_populates="origin_trips", foreign_keys=[origin_site_id]
    )
    destination_site: Mapped[Site | None] = relationship(
        back_populates="destination_trips", foreign_keys=[destination_site_id]
    )

    @property
    def origin_display_name(self) -> str:
        if self.origin_name and self.origin_name.strip():
            return self.origin_name.strip()
        if self.origin_site is not None:
            return self.origin_site.name
        return UNKNOWN_LOCATION_NAME

    @property
    def destination_display_name(self) -> str:
        if self.destination_name and self.destination_name.strip():
            return self.destination_name.strip()
        if self.destination_site is not None:
            return self.destination_site.name
        return UNKNOWN_LOCATION_NAME


class TripProcessingCheckpoint(Base):
    __tablename__ = "trip_processing_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    last_owntracks_location_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    odometer_anchor_miles: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    odometer_anchor_recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class SmartcarWebhookEvent(Base):
    """Stored Smartcar webhook delivery after HMAC verification succeeds."""

    __tablename__ = "smartcar_webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(
        String(80),
        unique=True,
        index=True,
        comment="Smartcar eventId value used to reject duplicate event processing.",
    )
    event_type: Mapped[str] = mapped_column(
        String(80),
        index=True,
        comment="Smartcar eventType value such as VEHICLE_STATE or VEHICLE_ERROR.",
    )
    user_id: Mapped[str | None] = mapped_column(
        String(80),
        nullable=True,
        comment="Smartcar user identifier from data.user.id.",
    )
    vehicle_id: Mapped[str | None] = mapped_column(
        String(80),
        index=True,
        nullable=True,
        comment="Smartcar vehicle identifier from data.vehicle.id.",
    )
    vehicle_make: Mapped[str | None] = mapped_column(
        String(80),
        nullable=True,
        comment="Vehicle make included in the webhook vehicle object.",
    )
    vehicle_model: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        comment="Vehicle model included in the webhook vehicle object.",
    )
    vehicle_year: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Vehicle model year included in the webhook vehicle object.",
    )
    vehicle_mode: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
        comment="Smartcar vehicle mode such as live, test, or simulated.",
    )
    vehicle_powertrain_type: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
        comment="Vehicle powertrain type such as ICE, HEV, PHEV, or BEV.",
    )
    webhook_id: Mapped[str | None] = mapped_column(
        String(80),
        index=True,
        nullable=True,
        comment="Smartcar webhook identifier from meta.webhookId.",
    )
    webhook_name: Mapped[str | None] = mapped_column(
        String(160),
        nullable=True,
        comment="Smartcar webhook display name from meta.webhookName.",
    )
    delivery_id: Mapped[str | None] = mapped_column(
        String(80),
        unique=True,
        nullable=True,
        comment="Smartcar delivery identifier from meta.deliveryId.",
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp Smartcar says it delivered the webhook event.",
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        comment="Timestamp this app accepted the verified webhook event.",
    )
    odometer_miles: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 3),
        nullable=True,
        comment="Webhook odometer reading converted to miles for trip mileage.",
    )
    odometer_raw_value: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 3),
        nullable=True,
        comment="Original odometer value before unit conversion.",
    )
    odometer_unit: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="Original Smartcar odometer unit, usually km or mi.",
    )
    odometer_recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="OEM or retrieval timestamp attached to the odometer signal.",
    )
    fuel_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 2),
        nullable=True,
        comment="Fuel level percentage when Smartcar includes a fuel signal.",
    )
    fuel_unit: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="Unit attached to the Smartcar fuel level signal.",
    )
    is_locked: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
        comment="Door lock state from the Smartcar closure-islocked signal.",
    )
    is_online: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
        comment="Connectivity state from the Smartcar connectivitystatus-isonline signal.",
    )
    nickname: Mapped[str | None] = mapped_column(
        String(160),
        nullable=True,
        comment="Vehicle nickname from the Smartcar vehicle identification signal.",
    )
    vin: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
        comment="Vehicle VIN from the Smartcar vehicle identification signal.",
    )
    firmware_version: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        comment="Current firmware version when Smartcar includes that signal.",
    )
    triggers: Mapped[list] = mapped_column(
        JSON,
        default=list,
        comment="Webhook trigger objects from the Smartcar payload.",
    )
    raw_payload: Mapped[dict] = mapped_column(
        JSON,
        comment="Complete verified Smartcar payload for audit and future parsing.",
    )

    signal_rows: Mapped[list["SmartcarWebhookSignal"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
    )


class SmartcarWebhookSignal(Base):
    """Stored Smartcar signal belonging to a verified webhook delivery."""

    __tablename__ = "smartcar_webhook_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("smartcar_webhook_events.id", ondelete="CASCADE"),
        index=True,
        comment="Database id for the parent Smartcar webhook delivery.",
    )
    code: Mapped[str | None] = mapped_column(
        String(160),
        index=True,
        nullable=True,
        comment="Smartcar signal code such as odometer-traveleddistance.",
    )
    name: Mapped[str | None] = mapped_column(
        String(160),
        index=True,
        nullable=True,
        comment="Smartcar signal display name such as TraveledDistance.",
    )
    group: Mapped[str | None] = mapped_column(
        String(160),
        nullable=True,
        comment="Smartcar signal group such as Odometer or VehicleIdentification.",
    )
    status: Mapped[str | None] = mapped_column(
        String(80),
        nullable=True,
        comment="Smartcar signal status value, normally SUCCESS for usable data.",
    )
    value: Mapped[object | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Signal body.value exactly as Smartcar sent it.",
    )
    unit: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
        comment="Signal body.unit value when Smartcar includes one.",
    )
    oem_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="OEM update timestamp from signal meta.oemUpdatedAt.",
    )
    retrieved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Smartcar retrieval timestamp from signal meta.retrievedAt.",
    )
    body: Mapped[dict] = mapped_column(
        JSON,
        default=dict,
        comment="Complete Smartcar signal body object.",
    )
    meta: Mapped[dict] = mapped_column(
        JSON,
        default=dict,
        comment="Complete Smartcar signal meta object.",
    )
    raw_signal: Mapped[dict] = mapped_column(
        JSON,
        comment="Complete Smartcar signal object for audit and future parsing.",
    )

    event: Mapped[SmartcarWebhookEvent] = relationship(back_populates="signal_rows")


class GasPriceSnapshot(Base):
    __tablename__ = "gas_price_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    observed_on: Mapped[date] = mapped_column(Date)
    state: Mapped[str] = mapped_column(String(2))
    grade: Mapped[str] = mapped_column(String(40), default="regular")
    price_per_gallon: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    source: Mapped[str] = mapped_column(String(80))
    source_detail: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MonthlyGasPrice(Base):
    __tablename__ = "monthly_gas_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(2), default="MI")
    average_price_per_gallon: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    buffer_per_gallon: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0.50"))
    effective_rate: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    source: Mapped[str] = mapped_column(String(80))
    source_detail: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
