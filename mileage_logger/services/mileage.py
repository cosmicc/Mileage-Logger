from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from math import asin, cos, radians, sin, sqrt

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from mileage_logger.config import get_settings
from mileage_logger.models import OwnTracksLocation, Site, Trip
from mileage_logger.services.places import create_site_from_google_place

METERS_PER_MILE = Decimal("1609.344")
EARTH_RADIUS_M = Decimal("6371008.8")
AUTO_TRIP_SOURCE = "auto"
MANUAL_TRIP_SOURCE = "manual"
FALSE_STOP_MERGED_SOURCE = "false_stop_merged"


class FalseStopMergeError(ValueError):
    pass


@dataclass
class StopVisit:
    site: Site | None
    started_location: OwnTracksLocation
    ended_location: OwnTracksLocation
    observed_until: datetime | None = None

    @property
    def started_at(self) -> datetime:
        return self.started_location.captured_at

    @property
    def ended_at(self) -> datetime:
        return self.ended_location.captured_at

    @property
    def duration(self) -> timedelta:
        observed_until = self.observed_until or self.ended_at
        return observed_until - self.started_at


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


def _region_names(location: OwnTracksLocation) -> list[str]:
    payload = location.raw_payload or {}
    names: list[str] = []
    description = payload.get("desc")
    if description:
        names.append(str(description))
    regions = payload.get("inregions")
    if isinstance(regions, list):
        names.extend(str(region) for region in regions if str(region).strip())
    return [name.strip() for name in names if name.strip()]


def site_for_location(
    location: OwnTracksLocation,
    sites: list[Site],
    sites_by_name: dict[str, Site],
) -> Site | None:
    for region_name in _region_names(location):
        site = sites_by_name.get(region_name.casefold())
        if site is not None and site.active:
            return site
    return nearest_site(location, sites)


def _distance_between_locations_meters(
    first: OwnTracksLocation,
    second: OwnTracksLocation,
) -> Decimal:
    return (
        haversine_miles(
            first.latitude,
            first.longitude,
            second.latitude,
            second.longitude,
        )
        * METERS_PER_MILE
    )


def _same_stop(
    candidate_site: Site | None,
    candidate_anchor: OwnTracksLocation,
    current_site: Site | None,
    location: OwnTracksLocation,
    unknown_stop_radius_m: int,
) -> bool:
    if candidate_site is not None and current_site is not None:
        return candidate_site.id == current_site.id
    if candidate_site is None and current_site is None:
        return _distance_between_locations_meters(
            candidate_anchor,
            location,
        ) <= Decimal(unknown_stop_radius_m)
    return False


def _align_datetime(reference: datetime, value: datetime) -> datetime:
    if reference.tzinfo is None and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    if reference.tzinfo is not None and value.tzinfo is None:
        return value.replace(tzinfo=reference.tzinfo)
    return value


def _day_end_for_location(location: OwnTracksLocation) -> datetime:
    day_end = datetime.combine(location.captured_at.date() + timedelta(days=1), time.min, tzinfo=UTC)
    return _align_datetime(location.captured_at, day_end)


def _qualifying_stops(
    locations: list[OwnTracksLocation],
    sites: list[Site],
    *,
    minimum_stop_duration: timedelta,
    unknown_stop_radius_m: int,
    final_observed_until: datetime | None = None,
) -> list[StopVisit]:
    if not locations:
        return []

    sites_by_name = {site.name.casefold(): site for site in sites}
    visits: list[StopVisit] = []
    candidate_site = site_for_location(locations[0], sites, sites_by_name)
    candidate_start = locations[0]
    candidate_end = locations[0]

    def close_candidate(observed_until: datetime | None = None) -> None:
        stop_observed_until = (
            observed_until
            if candidate_site is not None and observed_until is not None
            else candidate_end.captured_at
        )
        stop_observed_until = _align_datetime(candidate_start.captured_at, stop_observed_until)
        if stop_observed_until < candidate_end.captured_at:
            stop_observed_until = candidate_end.captured_at
        if stop_observed_until - candidate_start.captured_at >= minimum_stop_duration:
            visits.append(
                StopVisit(
                    site=candidate_site,
                    started_location=candidate_start,
                    ended_location=candidate_end,
                    observed_until=stop_observed_until,
                )
            )

    for location in locations[1:]:
        current_site = site_for_location(location, sites, sites_by_name)
        if location.captured_at.date() != candidate_start.captured_at.date():
            close_candidate(_day_end_for_location(candidate_start))
            candidate_site = current_site
            candidate_start = location
            candidate_end = location
            continue

        if _same_stop(
            candidate_site,
            candidate_start,
            current_site,
            location,
            unknown_stop_radius_m,
        ):
            candidate_end = location
            continue

        close_candidate(location.captured_at)
        candidate_site = current_site
        candidate_start = location
        candidate_end = location

    close_candidate(final_observed_until)
    return visits


def _path_miles_between(
    locations: list[OwnTracksLocation],
    start_location: OwnTracksLocation,
    end_location: OwnTracksLocation,
) -> Decimal:
    route_locations = [
        location
        for location in locations
        if start_location.captured_at <= location.captured_at <= end_location.captured_at
    ]
    if not route_locations or route_locations[0].id != start_location.id:
        route_locations.insert(0, start_location)
    if route_locations[-1].id != end_location.id:
        route_locations.append(end_location)

    total = Decimal("0.00")
    for previous, current in zip(route_locations, route_locations[1:], strict=False):
        total += haversine_miles(
            previous.latitude,
            previous.longitude,
            current.latitude,
            current.longitude,
        )
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _trip_overlaps_window(trip: Trip, started_at: datetime, ended_at: datetime) -> bool:
    return trip.started_at < ended_at and trip.ended_at > started_at


def _preserved_trips_for_range(db: Session, start_date: date, end_date: date) -> list[Trip]:
    return list(
        db.scalars(
            select(Trip)
            .where(Trip.source != AUTO_TRIP_SOURCE)
            .where(Trip.trip_date >= start_date)
            .where(Trip.trip_date <= end_date)
            .order_by(Trip.trip_date.asc(), Trip.started_at.asc())
        )
    )


def _overlaps_preserved_trip(
    preserved_trips: list[Trip],
    *,
    trip_date: date,
    started_at: datetime,
    ended_at: datetime,
) -> bool:
    return any(
        trip.trip_date == trip_date and _trip_overlaps_window(trip, started_at, ended_at)
        for trip in preserved_trips
    )


def _trip_notes(origin: StopVisit, destination: StopVisit, minimum_minutes: int) -> str:
    notes: list[str] = [f"Auto-generated from stops of at least {minimum_minutes} minutes."]
    if origin.site is None:
        notes.append("Origin was an unknown stationary stop.")
    if destination.site is None:
        notes.append("Destination was an unknown stationary stop.")
    return " ".join(notes)


def _enrich_unknown_stops(db: Session, stops: list[StopVisit]) -> None:
    for stop in stops:
        if stop.site is not None:
            continue
        stop.site = create_site_from_google_place(
            db,
            stop.started_location.latitude,
            stop.started_location.longitude,
        )


def date_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    return start, end


def _date_range_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    start_dt = datetime.combine(start_date, time.min, tzinfo=UTC)
    end_dt = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=UTC)
    return start_dt, end_dt


def _locations_for_range(db: Session, start_date: date, end_date: date) -> list[OwnTracksLocation]:
    start_dt, end_dt = _date_range_bounds(start_date, end_date)
    stmt = (
        select(OwnTracksLocation)
        .where(OwnTracksLocation.captured_at >= start_dt)
        .where(OwnTracksLocation.captured_at < end_dt)
        .order_by(OwnTracksLocation.captured_at.asc())
    )
    return list(db.scalars(stmt))


def _final_observed_until(end_date: date, as_of: datetime | None = None) -> datetime:
    current_dt = as_of or datetime.now(UTC)
    if end_date >= current_dt.date():
        return current_dt
    return datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=UTC)


def generate_trips(
    db: Session,
    start_date: date,
    end_date: date,
    *,
    as_of: datetime | None = None,
) -> list[Trip]:
    settings = get_settings()
    sites = list(db.scalars(select(Site).order_by(Site.name.asc())))
    locations = _locations_for_range(db, start_date, end_date)
    if not locations:
        return []

    preserved_trips = _preserved_trips_for_range(db, start_date, end_date)
    db.execute(
        delete(Trip)
        .where(Trip.source == AUTO_TRIP_SOURCE)
        .where(Trip.trip_date >= start_date)
        .where(Trip.trip_date <= end_date)
    )

    minimum_stop_duration = timedelta(minutes=settings.owntracks_stop_minutes)
    stops = _qualifying_stops(
        locations,
        sites,
        minimum_stop_duration=minimum_stop_duration,
        unknown_stop_radius_m=settings.owntracks_unknown_stop_radius_m,
        final_observed_until=_final_observed_until(end_date, as_of=as_of),
    )
    _enrich_unknown_stops(db, stops)

    generated: list[Trip] = []
    for origin, destination in zip(stops, stops[1:], strict=False):
        trip_date = origin.ended_at.date()
        started_at = origin.ended_at
        ended_at = destination.started_at
        if _overlaps_preserved_trip(
            preserved_trips,
            trip_date=trip_date,
            started_at=started_at,
            ended_at=ended_at,
        ):
            continue

        miles = _path_miles_between(
            locations,
            origin.ended_location,
            destination.started_location,
        )
        if miles < settings.min_trip_miles:
            continue

        trip = Trip(
            trip_date=trip_date,
            origin_site_id=origin.site.id if origin.site is not None else None,
            destination_site_id=destination.site.id if destination.site is not None else None,
            started_at=started_at,
            ended_at=ended_at,
            start_latitude=origin.ended_location.latitude,
            start_longitude=origin.ended_location.longitude,
            end_latitude=destination.started_location.latitude,
            end_longitude=destination.started_location.longitude,
            miles=miles,
            include_in_report=True,
            source=AUTO_TRIP_SOURCE,
            notes=_trip_notes(origin, destination, settings.owntracks_stop_minutes),
        )
        db.add(trip)
        generated.append(trip)

    db.commit()
    for trip in generated:
        db.refresh(trip)
    return generated


def mark_trip_manually_reviewed(trip: Trip) -> None:
    if trip.source == AUTO_TRIP_SOURCE:
        trip.source = MANUAL_TRIP_SOURCE


def _append_note(existing_notes: str | None, note: str) -> str:
    existing = (existing_notes or "").strip()
    return f"{existing} {note}".strip() if existing else note


def merge_false_stop_into_next_trip(db: Session, trip_id: int) -> Trip:
    false_stop_trip = db.get(Trip, trip_id)
    if false_stop_trip is None:
        raise FalseStopMergeError("Trip not found")

    next_trip = db.scalar(
        select(Trip)
        .where(Trip.id != false_stop_trip.id)
        .where(Trip.trip_date == false_stop_trip.trip_date)
        .where(Trip.started_at >= false_stop_trip.ended_at)
        .order_by(Trip.started_at.asc(), Trip.id.asc())
        .limit(1)
    )
    if next_trip is None:
        raise FalseStopMergeError("No later trip is available to merge into")

    false_stop_name = (
        false_stop_trip.destination_site.name if false_stop_trip.destination_site else "Unknown"
    )
    merged_miles = (false_stop_trip.miles + next_trip.miles).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )

    next_trip.trip_date = false_stop_trip.trip_date
    next_trip.origin_site_id = false_stop_trip.origin_site_id
    next_trip.started_at = false_stop_trip.started_at
    next_trip.start_latitude = false_stop_trip.start_latitude
    next_trip.start_longitude = false_stop_trip.start_longitude
    next_trip.miles = merged_miles
    next_trip.include_in_report = True
    next_trip.source = FALSE_STOP_MERGED_SOURCE
    next_trip.notes = _append_note(
        next_trip.notes,
        f"Merged false stop at {false_stop_name} from trip {false_stop_trip.id}.",
    )

    db.delete(false_stop_trip)
    db.commit()
    db.refresh(next_trip)
    return next_trip


def purge_processed_owntracks_locations(
    db: Session,
    start_date: date,
    end_date: date,
    *,
    now: datetime | None = None,
) -> int:
    start_dt, range_end_dt = _date_range_bounds(start_date, end_date)
    current_dt = now or datetime.now(UTC)
    today_start_dt = datetime.combine(current_dt.date(), time.min, tzinfo=UTC)
    purge_before = min(range_end_dt, today_start_dt)
    if purge_before <= start_dt:
        return 0

    result = db.execute(
        delete(OwnTracksLocation)
        .where(OwnTracksLocation.captured_at >= start_dt)
        .where(OwnTracksLocation.captured_at < purge_before)
    )
    db.commit()
    rowcount = result.rowcount or 0
    return rowcount if rowcount > 0 else 0


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
