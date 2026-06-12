from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from math import asin, cos, radians, sin, sqrt

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from mileage_logger.config import get_settings
from mileage_logger.models import (
    UNKNOWN_LOCATION_NAME,
    OwnTracksLocation,
    PersonalTripPattern,
    Site,
    Trip,
    normalize_location_name,
)
from mileage_logger.services.timezone import (
    datetime_to_local_date,
    local_day_bounds,
    local_day_end_for_datetime,
)

METERS_PER_MILE = Decimal("1609.344")
EARTH_RADIUS_M = Decimal("6371008.8")
AUTO_TRIP_SOURCE = "auto"
MANUAL_TRIP_SOURCE = "manual"
FALSE_STOP_MERGED_SOURCE = "false_stop_merged"
PERSONAL_TRIP_SOURCE = "personal"
WAYPOINT_EXIT_DISTANCE_M = Decimal("500")
WAYPOINT_DRIVING_AWAY_MARGIN_M = Decimal("50")
WAYPOINT_DRIVING_AWAY_MIN_DELTA_M = Decimal("25")
WAYPOINT_DRIVING_AWAY_MIN_OUTSIDE_REPORTS = 2


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
    matches = [
        (distance_meters(site, location), site)
        for site in sites
        if distance_meters(site, location) <= Decimal(site.radius_m)
    ]
    if not matches:
        return None
    return min(matches, key=lambda item: item[0])[1]


def _region_names(location: OwnTracksLocation) -> list[str]:
    payload = location.raw_payload or {}
    if payload.get("_type") == "transition" and str(payload.get("event", "")).lower() == "leave":
        return []
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
        if site is not None:
            return site
    return nearest_site(location, sites)


def _is_confirmed_waypoint_exit(
    site: Site,
    location: OwnTracksLocation,
    *,
    outside_reports: int,
    previous_outside_distance_m: Decimal | None,
) -> tuple[bool, Decimal]:
    distance_m = distance_meters(site, location)
    if distance_m >= WAYPOINT_EXIT_DISTANCE_M:
        return True, distance_m
    if previous_outside_distance_m is None:
        return False, distance_m
    moving_away = (
        outside_reports >= WAYPOINT_DRIVING_AWAY_MIN_OUTSIDE_REPORTS
        and distance_m > Decimal(site.radius_m) + WAYPOINT_DRIVING_AWAY_MARGIN_M
        and distance_m - previous_outside_distance_m >= WAYPOINT_DRIVING_AWAY_MIN_DELTA_M
    )
    return moving_away, distance_m


def _align_datetime(reference: datetime, value: datetime) -> datetime:
    if reference.tzinfo is None and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    if reference.tzinfo is not None and value.tzinfo is None:
        return value.replace(tzinfo=reference.tzinfo)
    return value


def _day_end_for_location(location: OwnTracksLocation) -> datetime:
    day_end = local_day_end_for_datetime(location.captured_at)
    return _align_datetime(location.captured_at, day_end)


def _qualifying_stops(
    locations: list[OwnTracksLocation],
    sites: list[Site],
    *,
    minimum_stop_duration: timedelta,
    final_observed_until: datetime | None = None,
) -> list[StopVisit]:
    if not locations:
        return []

    sites_by_name = {site.name.casefold(): site for site in sites}
    visits: list[StopVisit] = []
    candidate_site: Site | None = None
    candidate_start: OwnTracksLocation | None = None
    candidate_end: OwnTracksLocation | None = None
    outside_reports = 0
    previous_outside_distance_m: Decimal | None = None

    def start_candidate(site: Site | None, location: OwnTracksLocation) -> None:
        nonlocal candidate_site, candidate_start, candidate_end
        nonlocal outside_reports, previous_outside_distance_m
        candidate_site = site
        candidate_start = location
        candidate_end = location
        outside_reports = 0
        previous_outside_distance_m = None

    def clear_candidate() -> None:
        nonlocal candidate_site, candidate_start, candidate_end
        nonlocal outside_reports, previous_outside_distance_m
        candidate_site = None
        candidate_start = None
        candidate_end = None
        outside_reports = 0
        previous_outside_distance_m = None

    def close_candidate(
        observed_until: datetime | None = None,
        *,
        extend_open_visit: bool = False,
    ) -> None:
        if candidate_site is None or candidate_start is None or candidate_end is None:
            return
        if extend_open_visit and observed_until is not None:
            stop_observed_until = observed_until
        else:
            stop_observed_until = candidate_end.captured_at
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

    for location in locations:
        current_site = site_for_location(location, sites, sites_by_name)
        if candidate_site is None or candidate_start is None or candidate_end is None:
            if current_site is not None:
                start_candidate(current_site, location)
            continue

        if datetime_to_local_date(location.captured_at) != datetime_to_local_date(
            candidate_start.captured_at
        ):
            close_candidate(_day_end_for_location(candidate_start), extend_open_visit=True)
            if current_site is not None:
                start_candidate(current_site, location)
            else:
                clear_candidate()
            continue

        if current_site is not None and candidate_site.id == current_site.id:
            candidate_end = location
            outside_reports = 0
            previous_outside_distance_m = None
            continue

        outside_reports += 1
        exit_confirmed, current_distance_m = _is_confirmed_waypoint_exit(
            candidate_site,
            location,
            outside_reports=outside_reports,
            previous_outside_distance_m=previous_outside_distance_m,
        )
        previous_outside_distance_m = current_distance_m
        if not exit_confirmed:
            continue

        close_candidate(location.captured_at)
        if current_site is not None:
            start_candidate(current_site, location)
        else:
            clear_candidate()

    close_candidate(final_observed_until, extend_open_visit=True)
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


def _trip_notes(minimum_minutes: int) -> str:
    return f"Auto-generated from OwnTracks waypoint visits of at least {minimum_minutes} minutes."


def _stop_location_name(stop: StopVisit) -> str:
    if stop.site is not None:
        return stop.site.name
    return UNKNOWN_LOCATION_NAME


def _personal_trip_patterns(db: Session) -> list[PersonalTripPattern]:
    return list(db.scalars(select(PersonalTripPattern).order_by(PersonalTripPattern.id.asc())))


def _endpoint_matches_personal_pattern(
    *,
    pattern_site_id: int | None,
    trip_site_id: int | None,
    pattern_latitude: Decimal,
    pattern_longitude: Decimal,
    trip_latitude: Decimal,
    trip_longitude: Decimal,
    radius_m: int,
) -> bool:
    if pattern_site_id is not None:
        return trip_site_id == pattern_site_id
    distance_m = haversine_miles(
        pattern_latitude,
        pattern_longitude,
        trip_latitude,
        trip_longitude,
    ) * METERS_PER_MILE
    return distance_m <= Decimal(radius_m)


def _trip_matches_personal_pattern(
    trip: Trip,
    pattern: PersonalTripPattern,
    *,
    radius_m: int,
) -> bool:
    return _endpoint_matches_personal_pattern(
        pattern_site_id=pattern.origin_site_id,
        trip_site_id=trip.origin_site_id,
        pattern_latitude=pattern.origin_latitude,
        pattern_longitude=pattern.origin_longitude,
        trip_latitude=trip.start_latitude,
        trip_longitude=trip.start_longitude,
        radius_m=radius_m,
    ) and _endpoint_matches_personal_pattern(
        pattern_site_id=pattern.destination_site_id,
        trip_site_id=trip.destination_site_id,
        pattern_latitude=pattern.destination_latitude,
        pattern_longitude=pattern.destination_longitude,
        trip_latitude=trip.end_latitude,
        trip_longitude=trip.end_longitude,
        radius_m=radius_m,
    )


def _apply_personal_trip_patterns(
    trip: Trip,
    patterns: list[PersonalTripPattern],
    *,
    radius_m: int,
) -> None:
    matches_personal_pattern = any(
        _trip_matches_personal_pattern(trip, pattern, radius_m=radius_m) for pattern in patterns
    )
    if matches_personal_pattern:
        trip.include_in_report = False
        trip.notes = _append_note(trip.notes, "Auto-marked personal by matching route.")


def date_bounds(day: date) -> tuple[datetime, datetime]:
    return local_day_bounds(day)


def _date_range_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    start_dt, _ = local_day_bounds(start_date)
    _, end_dt = local_day_bounds(end_date)
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
    if end_date >= datetime_to_local_date(current_dt):
        return current_dt
    _, end_dt = local_day_bounds(end_date)
    return end_dt


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
    source_dates = sorted({datetime_to_local_date(location.captured_at) for location in locations})
    db.execute(
        delete(Trip)
        .where(Trip.source == AUTO_TRIP_SOURCE)
        .where(Trip.trip_date.in_(source_dates))
    )

    minimum_stop_duration = timedelta(minutes=settings.owntracks_stop_minutes)
    stops = _qualifying_stops(
        locations,
        sites,
        minimum_stop_duration=minimum_stop_duration,
        final_observed_until=_final_observed_until(end_date, as_of=as_of),
    )
    personal_patterns = _personal_trip_patterns(db)

    generated: list[Trip] = []
    for origin, destination in zip(stops, stops[1:], strict=False):
        if origin.site is None or destination.site is None:
            continue
        trip_date = datetime_to_local_date(origin.ended_at)
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
            origin_site_id=origin.site.id,
            destination_site_id=destination.site.id,
            started_at=started_at,
            ended_at=ended_at,
            start_latitude=origin.ended_location.latitude,
            start_longitude=origin.ended_location.longitude,
            end_latitude=destination.started_location.latitude,
            end_longitude=destination.started_location.longitude,
            origin_name=_stop_location_name(origin),
            destination_name=_stop_location_name(destination),
            miles=miles,
            include_in_report=True,
            source=AUTO_TRIP_SOURCE,
            notes=_trip_notes(settings.owntracks_stop_minutes),
        )
        _apply_personal_trip_patterns(
            trip,
            personal_patterns,
            radius_m=settings.owntracks_unknown_stop_radius_m,
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


def update_trip_location_names(trip: Trip, origin_name: str, destination_name: str) -> None:
    trip.origin_name = normalize_location_name(origin_name)
    trip.destination_name = normalize_location_name(destination_name)
    mark_trip_manually_reviewed(trip)


def _append_note(existing_notes: str | None, note: str) -> str:
    existing = (existing_notes or "").strip()
    if existing == note or existing.endswith(f" {note}"):
        return existing
    return f"{existing} {note}".strip() if existing else note


def _personal_pattern_for_trip(trip: Trip) -> PersonalTripPattern:
    return PersonalTripPattern(
        origin_site_id=trip.origin_site_id,
        destination_site_id=trip.destination_site_id,
        origin_name=trip.origin_display_name,
        destination_name=trip.destination_display_name,
        origin_latitude=trip.start_latitude,
        origin_longitude=trip.start_longitude,
        destination_latitude=trip.end_latitude,
        destination_longitude=trip.end_longitude,
    )


def _get_or_create_personal_pattern(
    db: Session, trip: Trip, *, radius_m: int
) -> PersonalTripPattern:
    for pattern in _personal_trip_patterns(db):
        if _trip_matches_personal_pattern(trip, pattern, radius_m=radius_m):
            return pattern
    pattern = _personal_pattern_for_trip(trip)
    db.add(pattern)
    db.flush()
    return pattern


def mark_trip_personal(db: Session, trip_id: int) -> Trip:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise FalseStopMergeError("Trip not found")

    settings = get_settings()
    pattern = _get_or_create_personal_pattern(
        db,
        trip,
        radius_m=settings.owntracks_unknown_stop_radius_m,
    )
    trip.include_in_report = False
    trip.source = PERSONAL_TRIP_SOURCE
    trip.notes = _append_note(trip.notes, "Marked personal.")

    future_trips = list(
        db.scalars(
            select(Trip)
            .where(Trip.id != trip.id)
            .where(Trip.started_at > trip.started_at)
            .order_by(Trip.started_at.asc(), Trip.id.asc())
        )
    )
    for future_trip in future_trips:
        if not _trip_matches_personal_pattern(
            future_trip,
            pattern,
            radius_m=settings.owntracks_unknown_stop_radius_m,
        ):
            continue
        future_trip.include_in_report = False
        future_trip.notes = _append_note(
            future_trip.notes,
            "Auto-marked personal by matching route.",
        )

    db.commit()
    db.refresh(trip)
    return trip


def mark_trip_work(db: Session, trip_id: int) -> Trip:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise FalseStopMergeError("Trip not found")

    trip.include_in_report = True
    if trip.source in {AUTO_TRIP_SOURCE, PERSONAL_TRIP_SOURCE}:
        trip.source = MANUAL_TRIP_SOURCE
    trip.notes = _append_note(trip.notes, "Marked work.")
    db.commit()
    db.refresh(trip)
    return trip


def toggle_trip_personal(db: Session, trip_id: int) -> Trip:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise FalseStopMergeError("Trip not found")
    if trip.include_in_report:
        return mark_trip_personal(db, trip_id)
    return mark_trip_work(db, trip_id)


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

    false_stop_name = false_stop_trip.destination_display_name
    merged_miles = (false_stop_trip.miles + next_trip.miles).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )

    next_trip.trip_date = false_stop_trip.trip_date
    next_trip.origin_site_id = false_stop_trip.origin_site_id
    next_trip.origin_name = false_stop_trip.origin_display_name
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
    today_start_dt, _ = local_day_bounds(datetime_to_local_date(current_dt))
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
