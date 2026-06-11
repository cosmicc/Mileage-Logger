from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class SiteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    latitude: Decimal
    longitude: Decimal
    radius_m: int = Field(default=150, ge=1)


class SiteRead(SiteCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    active: bool


class LocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user: str | None
    device: str | None
    captured_at: datetime
    latitude: Decimal
    longitude: Decimal
    accuracy_m: int | None


class TripUpdate(BaseModel):
    miles: Decimal | None = Field(default=None, ge=0)
    include_in_report: bool | None = None
    notes: str | None = None


class TripRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trip_date: date
    started_at: datetime
    ended_at: datetime
    miles: Decimal
    include_in_report: bool
    source: str
    notes: str


class MonthlyGasPriceCreate(BaseModel):
    year: int = Field(ge=2000)
    month: int = Field(ge=1, le=12)
    average_price_per_gallon: Decimal = Field(ge=0)
    buffer_per_gallon: Decimal = Field(default=Decimal("0.50"), ge=0)
    state: str = "MI"
    source_detail: str = "manual"
