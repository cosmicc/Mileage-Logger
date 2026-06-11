from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from math import asin, cos, radians, sin, sqrt

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from mileage_logger.config import get_settings
from mileage_logger.models import OwnTracksLocation, Site, Trip

METERS_PER_MILE = Decimal("1609.344")
EARTH_RADIUS_M = Decimal("6371008.8")


def haversine_miles(
    lat1: Decimal | float,
    lon1: Decimal | float,
    lat2: Decimal | float,
    lon2: Decimal | float,
) -> Decimal:
    lat1_f = radians(float(lat1))
    lat2_f = radians(float(lat2))
    dlat = lat2_f - lat1_f
    dlon = radians(float(lon2) - float(lon1))
    a = sin(dlat / 2) ** 2 + cos(lat1_f) * cos(lat2_f) * sin(dlon / 2) ** 2
    meters = float(EARTH_RADIUS_M) * 2 * asin(sqrt(a))
    miles = Decimal(str(meters)) / METERS_PER_MILE
    return miles.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def distance_meters(site: Site, location: OwnTracksLocation) -> Decimal:
    return (
        haversine_miles(
            site.latitude,
            site.longitude,
            location.latitude,
            location.longitude,
        )
        * METERS_PER_MILE
    )


def nearest_site(location: OwnTracksLocation, sites: list[Site]) -> Site | None:
    active_sites = [site for site in sites if site.active]
    matches = [
        (distance_meters(site, location), site)
        for site in active_sites
        if distance_meters(site, location) <= Decimal(site.radius_m)
    ]
    if not matches:
        return None
    return min(matches, key=lambda item: item[0])[1]


def date_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    return start, end


def _locations_for_range(db: Session, start_date: date, end_date: date) -> list[OwnTracksLocation]:
    start_dt = datetime.combine(start_date, time.min, tzinfo=UTC)
    end_dt = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=UTC)
    stmt = (
        select(OwnTracksLocation)
        .where(OwnTracksLocation.captured_at >= start_dt)
        .where(OwnTracksLocation.captured_at < end_dt)
        .order_by(OwnTracksLocation.captured_at.asc())
    )
    return list(db.scalars(stmt))


def generate_trips(db: Session, start_date: date, end_date: date) -> list[Trip]:
    settings = get_settings()
    sites = list(db.scalars(select(Site).order_by(Site.name.asc())))
    locations = _locations_for_range(db, start_date, end_date)

    db.execute(
        delete(Trip)
        .where(Trip.source == "auto")
        .where(Trip.trip_date >= start_date)
        .where(Trip.trip_date <= end_date)
    )

    generated: list[Trip] = []
    origin_site: Site | None = None
    previous_site: Site | None = None
    start_location: OwnTracksLocation | None = None
    previous_location: OwnTracksLocation | None = None
    accumulated_miles = Decimal("0.00")

    for location in locations:
        current_site = nearest_site(location, sites)
        if previous_location is None:
            previous_location = location
            previous_site = current_site
            continue

        if location.captured_at.date() != previous_location.captured_at.date():
            origin_site = None
            start_location = None
            accumulated_miles = Decimal("0.00")
            previous_location = location
            previous_site = current_site
            continue

        segment_miles = haversine_miles(
            previous_location.latitude,
            previous_location.longitude,
            location.latitude,
            location.longitude,
        )

        if origin_site is not None:
            accumulated_miles += segment_miles
        elif previous_site is not None and (
            current_site is None or current_site.id != previous_site.id
        ):
            origin_site = previous_site
            start_location = previous_location
            accumulated_miles = segment_miles

        if (
            origin_site is not None
            and current_site is not None
            and current_site.id != origin_site.id
            and start_location is not None
            and accumulated_miles >= settings.min_trip_miles
        ):
            trip = Trip(
                trip_date=location.captured_at.date(),
                origin_site_id=origin_site.id,
                destination_site_id=current_site.id,
                started_at=start_location.captured_at,
                ended_at=location.captured_at,
                start_latitude=start_location.latitude,
                start_longitude=start_location.longitude,
                end_latitude=location.latitude,
                end_longitude=location.longitude,
                miles=accumulated_miles.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                include_in_report=True,
                source="auto",
                notes="",
            )
            db.add(trip)
            generated.append(trip)
            origin_site = None
            start_location = None
            accumulated_miles = Decimal("0.00")

        previous_location = location
        previous_site = current_site or previous_site

    db.commit()
    for trip in generated:
        db.refresh(trip)
    return generated


def included_monthly_miles(db: Session, year: int, month: int) -> Decimal:
    start_date = date(year, month, 1)
    end_date = date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    stmt = (
        select(Trip)
        .where(Trip.trip_date >= start_date)
        .where(Trip.trip_date < end_date)
        .where(Trip.include_in_report.is_(True))
        .order_by(Trip.trip_date.asc(), Trip.started_at.asc())
    )
    total = sum((trip.miles for trip in db.scalars(stmt)), Decimal("0.00"))
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
