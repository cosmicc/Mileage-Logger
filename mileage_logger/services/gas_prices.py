import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mileage_logger.config import get_settings
from mileage_logger.models import GasPriceSnapshot, MonthlyGasPrice


class GasPriceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class GasPriceReading:
    state: str
    grade: str
    price_per_gallon: Decimal
    source: str
    source_detail: str
    observed_on: date


class GasPriceProvider:
    source_name = "base"

    def current_regular_price(self, state: str) -> GasPriceReading:
        raise NotImplementedError


class AaaMichiganGasPriceProvider(GasPriceProvider):
    source_name = "aaa_current"
    url = "https://gasprices.aaa.com/?state=MI"

    def current_regular_price(self, state: str) -> GasPriceReading:
        if state.upper() != "MI":
            raise GasPriceUnavailable("AAA provider is configured only for Michigan")

        response = httpx.get(self.url, timeout=20)
        response.raise_for_status()
        match = re.search(r"Current Avg\.\s*\$([0-9]+\.[0-9]+)", response.text)
        if not match:
            raise GasPriceUnavailable("Could not locate the Michigan current regular gas price")

        return GasPriceReading(
            state="MI",
            grade="regular",
            price_per_gallon=Decimal(match.group(1)).quantize(Decimal("0.001")),
            source=self.source_name,
            source_detail=self.url,
            observed_on=date.today(),
        )


class EiaSeriesProvider(GasPriceProvider):
    source_name = "eia_series"

    def current_regular_price(self, state: str) -> GasPriceReading:
        settings = get_settings()
        if not settings.eia_api_key or not settings.eia_series_id:
            raise GasPriceUnavailable("EIA_API_KEY and EIA_SERIES_ID are required")

        url = f"https://api.eia.gov/v2/seriesid/{settings.eia_series_id}"
        response = httpx.get(url, params={"api_key": settings.eia_api_key}, timeout=20)
        response.raise_for_status()
        data = response.json()
        rows = data.get("response", {}).get("data", [])
        if not rows:
            raise GasPriceUnavailable("EIA returned no rows for the configured series")
        row = rows[0]
        value = row.get("value")
        if value is None:
            raise GasPriceUnavailable("EIA latest row has no value")

        return GasPriceReading(
            state=state.upper(),
            grade="regular",
            price_per_gallon=Decimal(str(value)).quantize(Decimal("0.001")),
            source=self.source_name,
            source_detail=f"EIA series {settings.eia_series_id}",
            observed_on=date.today(),
        )


def configured_provider() -> GasPriceProvider:
    source = get_settings().gas_price_source
    if source == "eia_series":
        return EiaSeriesProvider()
    return AaaMichiganGasPriceProvider()


def save_daily_snapshot(db: Session, reading: GasPriceReading) -> GasPriceSnapshot:
    stmt = (
        select(GasPriceSnapshot)
        .where(GasPriceSnapshot.observed_on == reading.observed_on)
        .where(GasPriceSnapshot.state == reading.state)
        .where(GasPriceSnapshot.grade == reading.grade)
        .where(GasPriceSnapshot.source == reading.source)
    )
    snapshot = db.scalar(stmt)
    if snapshot is None:
        snapshot = GasPriceSnapshot(
            observed_on=reading.observed_on,
            state=reading.state,
            grade=reading.grade,
            price_per_gallon=reading.price_per_gallon,
            source=reading.source,
            source_detail=reading.source_detail,
        )
        db.add(snapshot)
    else:
        snapshot.price_per_gallon = reading.price_per_gallon
        snapshot.source_detail = reading.source_detail
    db.commit()
    db.refresh(snapshot)
    return snapshot


def fetch_and_save_current_snapshot(db: Session) -> GasPriceSnapshot:
    settings = get_settings()
    reading = configured_provider().current_regular_price(settings.gas_price_state)
    return save_daily_snapshot(db, reading)


def upsert_manual_monthly_price(
    db: Session,
    *,
    year: int,
    month: int,
    state: str,
    average_price_per_gallon: Decimal,
    buffer_per_gallon: Decimal,
    source_detail: str = "manual",
) -> MonthlyGasPrice:
    state = state.upper()
    effective_rate = (average_price_per_gallon + buffer_per_gallon).quantize(Decimal("0.001"))
    stmt = (
        select(MonthlyGasPrice)
        .where(MonthlyGasPrice.year == year)
        .where(MonthlyGasPrice.month == month)
        .where(MonthlyGasPrice.state == state)
    )
    monthly = db.scalar(stmt)
    if monthly is None:
        monthly = MonthlyGasPrice(
            year=year,
            month=month,
            state=state,
            average_price_per_gallon=average_price_per_gallon,
            buffer_per_gallon=buffer_per_gallon,
            effective_rate=effective_rate,
            source="manual",
            source_detail=source_detail,
        )
        db.add(monthly)
    else:
        monthly.average_price_per_gallon = average_price_per_gallon
        monthly.buffer_per_gallon = buffer_per_gallon
        monthly.effective_rate = effective_rate
        monthly.source = "manual"
        monthly.source_detail = source_detail
    db.commit()
    db.refresh(monthly)
    return monthly


def monthly_price_from_snapshots(db: Session, year: int, month: int, state: str) -> Decimal | None:
    start = date(year, month, 1)
    end = date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    stmt = (
        select(func.avg(GasPriceSnapshot.price_per_gallon))
        .where(GasPriceSnapshot.observed_on >= start)
        .where(GasPriceSnapshot.observed_on < end)
        .where(GasPriceSnapshot.state == state.upper())
        .where(GasPriceSnapshot.grade == "regular")
    )
    value = db.scalar(stmt)
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def get_or_create_monthly_price(db: Session, year: int, month: int) -> MonthlyGasPrice:
    settings = get_settings()
    state = settings.gas_price_state.upper()
    stmt = (
        select(MonthlyGasPrice)
        .where(MonthlyGasPrice.year == year)
        .where(MonthlyGasPrice.month == month)
        .where(MonthlyGasPrice.state == state)
    )
    monthly = db.scalar(stmt)
    if monthly is not None:
        return monthly

    today = datetime.now(UTC).date()
    if today.year == year and today.month == month:
        fetch_and_save_current_snapshot(db)

    average = monthly_price_from_snapshots(db, year, month, state)
    if average is None:
        raise GasPriceUnavailable(
            "No monthly gas price is available. Add one manually or collect daily snapshots."
        )

    return upsert_manual_monthly_price(
        db,
        year=year,
        month=month,
        state=state,
        average_price_per_gallon=average,
        buffer_per_gallon=settings.gas_price_buffer,
        source_detail="average of stored daily snapshots",
    )
