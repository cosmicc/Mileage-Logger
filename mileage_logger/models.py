from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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
    latitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    longitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    radius_m: Mapped[int] = mapped_column(Integer, default=150)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

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
    miles: Mapped[Decimal] = mapped_column(Numeric(9, 2))
    include_in_report: Mapped[bool] = mapped_column(Boolean, default=True)
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


class MonthlyReport(Base):
    __tablename__ = "monthly_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    total_miles: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    gas_price_id: Mapped[int] = mapped_column(ForeignKey("monthly_gas_prices.id"))
    reimbursement_total: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    pdf_path: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    gas_price: Mapped[MonthlyGasPrice] = relationship()
