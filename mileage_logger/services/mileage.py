import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from math import asin, cos, radians, sin, sqrt

from sqlalchemy import select
from sqlalchemy.orm import Session

from mileage_logger.config import get_settings
from mileage_logger.models import (
    AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
    DeletedTrip,
    OwnTracksLocation,
    Site,
    Trip,
    TripProcessingCheckpoint,
    normalize_location_name,
)
from mileage_logger.services.timezone import (
    datetime_to_local_date,
    datetime_to_utc,
    local_day_bounds,
)

METERS_PER_MILE = Decimal("1609.344")
EARTH_RADIUS_M = Decimal("6371008.8")
DISTANCE_PRECISION = Decimal("0.1")
ODOMETER_PRECISION = Decimal("0.1")
AUTO_TRIP_SOURCE = "auto"
MANUAL_TRIP_SOURCE = "manual"
MILEAGE_SOURCE_OWNTRACKS_PATH = "owntracks_path"
MILEAGE_SOURCE_ESTIMATED_ODOMETER = "estimated_odometer"
MILEAGE_SOURCE_WAYPOINT_DISTANCE = "waypoint_distance"
MILEAGE_SOURCE_MANUAL = "manual"
ODOMETER_SOURCE_MANUAL = "manual"
ODOMETER_SOURCE_ESTIMATED = "estimated"
ODOMETER_SOURCE_PREVIOUS_TRIP = "previous_trip"
ODOMETER_SOURCE_OWNTRACKS_ROLLING = "owntracks_rolling"
HOME_WAYPOINT_NAME = "Home"
WAYPOINT_TRIP_NOTE = "Auto-generated from OwnTracks waypoint transitions."
MISSING_LEAVE_NOTE = "Missing leave event inferred from previous waypoint."
MANUAL_TRIP_NOTE = "Manually added from Trips page."
trip_logger = logging.getLogger("mileage_logger.trip_calculation")
TripGenerationKey = tuple[int, int, datetime, datetime]


@dataclass(frozen=True)
class WaypointTransition:
    event: str
    site: Site
    location: OwnTracksLocation


@dataclass(frozen=True)
class MileageCalculation:
    miles: Decimal
    mileage_source: str
    start_odometer_miles: Decimal | None = None
    end_odometer_miles: Decimal | None = None
    start_odometer_source: str | None = None
    end_odometer_source: str | None = None


@dataclass(frozen=True)
class OdometerReading:
    miles: Decimal
    source: str


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
    return miles.quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)


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


def _date_range_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    start_dt, _ = local_day_bounds(start_date)
    _, end_dt = local_day_bounds(end_date)
    return start_dt, end_dt


def date_bounds(day: date) -> tuple[datetime, datetime]:
    return local_day_bounds(day)


def _locations_for_range(
    db: Session,
    start_date: date,
    end_date: date,
    *,
    end_padding: timedelta | None = None,
) -> list[OwnTracksLocation]:
    start_dt, end_dt = _date_range_bounds(start_date, end_date)
    if end_padding is not None:
        end_dt += end_padding
    stmt = (
        select(OwnTracksLocation)
        .where(OwnTracksLocation.captured_at >= start_dt)
        .where(OwnTracksLocation.captured_at < end_dt)
        .order_by(OwnTracksLocation.captured_at.asc(), OwnTracksLocation.id.asc())
    )
    return list(db.scalars(stmt))


def _transition_event(location: OwnTracksLocation) -> str | None:
    payload = location.raw_payload or {}
    if payload.get("_type") != "transition":
        return None

    event = str(payload.get("event") or "").strip().casefold()
    if event in {"enter", "arrive", "arrival"}:
        return "enter"
    if event in {"leave", "exit", "departure"}:
        return "leave"
    return None


def _region_id(location: OwnTracksLocation) -> str | None:
    region_id = (location.raw_payload or {}).get("rid")
    if region_id is None:
        return None
    return str(region_id).strip() or None


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


def _location_sort_key(location: OwnTracksLocation) -> tuple[datetime, int]:
    """Return a stable chronological key based on OwnTracks event time."""

    return datetime_to_utc(location.captured_at), location.id or 0


def site_indexes(sites: list[Site]) -> tuple[dict[str, Site], dict[str, Site]]:
    """Build saved-waypoint lookup maps for OwnTracks region and name matching."""

    sites_by_name = {site.name.casefold(): site for site in sites}
    sites_by_region_id = {
        site.owntracks_region_id: site
        for site in sites
        if site.owntracks_region_id is not None
    }
    return sites_by_name, sites_by_region_id


def site_for_location(
    location: OwnTracksLocation,
    sites: list[Site],
    sites_by_name: dict[str, Site],
    sites_by_region_id: dict[str, Site],
) -> Site | None:
    region_id = _region_id(location)
    if region_id is not None and region_id in sites_by_region_id:
        return sites_by_region_id[region_id]

    for region_name in _region_names(location):
        site = sites_by_name.get(region_name.casefold())
        if site is not None:
            return site
    return nearest_site(location, sites)


def same_saved_waypoint(
    first_location: OwnTracksLocation,
    second_location: OwnTracksLocation,
    sites: list[Site],
    sites_by_name: dict[str, Site],
    sites_by_region_id: dict[str, Site],
) -> bool:
    """Return true when two OwnTracks rows are inside the same saved waypoint."""

    first_site = site_for_location(first_location, sites, sites_by_name, sites_by_region_id)
    second_site = site_for_location(second_location, sites, sites_by_name, sites_by_region_id)
    return first_site is not None and second_site is not None and first_site.id == second_site.id


def owntracks_segment_miles(
    first_location: OwnTracksLocation,
    second_location: OwnTracksLocation,
    sites: list[Site],
    sites_by_name: dict[str, Site],
    sites_by_region_id: dict[str, Site],
) -> Decimal:
    """Return one OwnTracks movement segment while ignoring same-waypoint GPS drift."""

    if same_saved_waypoint(
        first_location,
        second_location,
        sites,
        sites_by_name,
        sites_by_region_id,
    ):
        return Decimal("0.0")
    return haversine_miles(
        first_location.latitude,
        first_location.longitude,
        second_location.latitude,
        second_location.longitude,
    )


def _site_matches_location(
    site: Site,
    location: OwnTracksLocation,
    sites: list[Site],
    sites_by_name: dict[str, Site],
    sites_by_region_id: dict[str, Site],
) -> bool:
    """Return true when a stored OwnTracks row still places the device inside a site."""

    matched_site = site_for_location(location, sites, sites_by_name, sites_by_region_id)
    return matched_site is not None and matched_site.id == site.id


def _enter_transition_confirmed(
    enter_location: OwnTracksLocation,
    site: Site,
    locations: list[OwnTracksLocation],
    enter_index: int,
    sites: list[Site],
    sites_by_name: dict[str, Site],
    sites_by_region_id: dict[str, Site],
) -> bool:
    """Confirm an enter event only after later OwnTracks data proves waypoint dwell time."""

    dwell_time = timedelta(minutes=get_settings().owntracks_waypoint_dwell_minutes)
    enter_time = datetime_to_utc(enter_location.captured_at)
    dwell_deadline = enter_time + dwell_time

    for candidate_location in locations[enter_index + 1 :]:
        candidate_time = datetime_to_utc(candidate_location.captured_at)
        candidate_event = _transition_event(candidate_location)
        candidate_site = site_for_location(
            candidate_location,
            sites,
            sites_by_name,
            sites_by_region_id,
        )

        if (
            candidate_event == "leave"
            and candidate_site is not None
            and candidate_site.id == site.id
        ):
            return candidate_time >= dwell_deadline

        if (
            candidate_event == "enter"
            and candidate_site is not None
            and candidate_site.id != site.id
        ):
            return False

        if candidate_time >= dwell_deadline and _site_matches_location(
            site,
            candidate_location,
            sites,
            sites_by_name,
            sites_by_region_id,
        ):
            return True

    return False


def _waypoint_transitions(
    locations: list[OwnTracksLocation],
    sites: list[Site],
) -> list[WaypointTransition]:
    sites_by_name, sites_by_region_id = site_indexes(sites)
    transitions: list[WaypointTransition] = []
    seen: set[tuple[str, int, datetime]] = set()

    ordered_locations = sorted(locations, key=_location_sort_key)
    for location_index, location in enumerate(ordered_locations):
        event = _transition_event(location)
        if event is None:
            continue
        site = site_for_location(location, sites, sites_by_name, sites_by_region_id)
        if site is None:
            continue
        if event == "enter" and not _enter_transition_confirmed(
            location,
            site,
            ordered_locations,
            location_index,
            sites,
            sites_by_name,
            sites_by_region_id,
        ):
            trip_logger.debug(
                "waypoint enter skipped reason=dwell_not_confirmed site=%s captured_at=%s "
                "dwell_minutes=%s",
                site.name,
                location.captured_at.isoformat(),
                get_settings().owntracks_waypoint_dwell_minutes,
            )
            continue
        dedupe_key = (event, site.id, location.captured_at)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        transitions.append(WaypointTransition(event=event, site=site, location=location))

    return transitions


def _is_location_update(location: OwnTracksLocation) -> bool:
    payload = location.raw_payload or {}
    return payload.get("_type") == "location"


def _trip_path_locations(
    locations: list[OwnTracksLocation],
    *,
    started_at: datetime,
    ended_at: datetime,
) -> list[OwnTracksLocation]:
    start_dt = datetime_to_utc(started_at)
    end_dt = datetime_to_utc(ended_at)
    if end_dt < start_dt:
        return []
    return [
        location
        for location in locations
        if start_dt <= datetime_to_utc(location.captured_at) <= end_dt
    ]


def _path_distance_miles(
    path_locations: list[OwnTracksLocation],
    sites: list[Site],
    sites_by_name: dict[str, Site],
    sites_by_region_id: dict[str, Site],
) -> Decimal | None:
    """Return trip path distance using the same movement rule as the rolling odometer."""

    has_location_update = any(
        _is_location_update(location) for location in path_locations
    )
    if len(path_locations) < 2 or not has_location_update:
        return None

    total = Decimal("0.0")
    previous_location = path_locations[0]
    for location in path_locations[1:]:
        total += owntracks_segment_miles(
            previous_location,
            location,
            sites,
            sites_by_name,
            sites_by_region_id,
        )
        previous_location = location
    return total.quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)


def _trip_path_miles(
    locations: list[OwnTracksLocation],
    *,
    started_at: datetime,
    ended_at: datetime,
    sites: list[Site],
    sites_by_name: dict[str, Site],
    sites_by_region_id: dict[str, Site],
) -> Decimal | None:
    return _path_distance_miles(
        _trip_path_locations(locations, started_at=started_at, ended_at=ended_at),
        sites,
        sites_by_name,
        sites_by_region_id,
    )


def _home_site(sites: list[Site]) -> Site | None:
    for site in sites:
        if site.name == HOME_WAYPOINT_NAME:
            return site
    return None


def _trip_notes(*, inferred_leave: bool) -> str:
    if inferred_leave:
        return f"{WAYPOINT_TRIP_NOTE} {MISSING_LEAVE_NOTE}"
    return WAYPOINT_TRIP_NOTE


def _append_note(existing_notes: str | None, note: str) -> str:
    existing = (existing_notes or "").strip()
    if existing == note or existing.endswith(f" {note}"):
        return existing
    return f"{existing} {note}".strip() if existing else note


def _odometer_for_transition(
    _db: Session,
    transition: WaypointTransition,
) -> OdometerReading | None:
    """Return the rolling OwnTracks odometer stamped onto a transition row."""

    if transition.location.odometer_miles is None:
        return None
    return OdometerReading(
        miles=Decimal(transition.location.odometer_miles).quantize(
            ODOMETER_PRECISION,
            rounding=ROUND_HALF_UP,
        ),
        source=transition.location.odometer_source or ODOMETER_SOURCE_OWNTRACKS_ROLLING,
    )


def _is_home_to_home(origin: Site, destination: Site) -> bool:
    return origin.name == HOME_WAYPOINT_NAME and destination.name == HOME_WAYPOINT_NAME


def _trip_overlaps_window(trip: Trip, started_at: datetime, ended_at: datetime) -> bool:
    if started_at == ended_at:
        return trip.started_at <= started_at <= trip.ended_at
    return trip.started_at < ended_at and trip.ended_at > started_at


def _preserved_trips_for_range(db: Session, start_date: date, end_date: date) -> list[Trip]:
    return list(
        db.scalars(
            select(Trip)
            .where(Trip.source != AUTO_TRIP_SOURCE)
            .where(Trip.trip_date >= start_date)
            .where(Trip.trip_date <= end_date)
            .order_by(Trip.trip_date.asc(), Trip.started_at.asc(), Trip.id.asc())
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


def _odometer_miles(
    start_odometer_miles: Decimal | None,
    end_odometer_miles: Decimal | None,
) -> Decimal | None:
    if start_odometer_miles is None or end_odometer_miles is None:
        return None
    miles = end_odometer_miles - start_odometer_miles
    if miles < 0:
        return None
    return miles.quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)


def _odometer_reading_miles(reading: OdometerReading | None) -> Decimal | None:
    return reading.miles if reading is not None else None


def _odometer_reading_source(reading: OdometerReading | None) -> str | None:
    return reading.source if reading is not None else None


def _new_odometer_details_available(
    existing_trip: Trip,
    calculation: MileageCalculation,
) -> bool:
    """Return true when a recalculation adds or corrects stored odometer details."""

    existing_values = (
        existing_trip.start_odometer_miles,
        existing_trip.end_odometer_miles,
        existing_trip.start_odometer_source,
        existing_trip.end_odometer_source,
    )
    calculated_values = (
        calculation.start_odometer_miles,
        calculation.end_odometer_miles,
        calculation.start_odometer_source,
        calculation.end_odometer_source,
    )
    return existing_values != calculated_values and any(
        value is not None for value in calculated_values
    )


def _distance_estimate_miles(origin: Site, destination: Site) -> Decimal:
    return haversine_miles(
        origin.latitude,
        origin.longitude,
        destination.latitude,
        destination.longitude,
    )


def _estimated_odometer_calculation(
    distance_miles: Decimal,
    *,
    start_odometer_miles: Decimal | None,
    end_odometer_miles: Decimal | None,
    start_odometer_source: str | None,
    end_odometer_source: str | None,
    odometer_anchor_miles: Decimal | None,
) -> MileageCalculation | None:
    distance = distance_miles.quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)
    if start_odometer_miles is not None:
        estimated_end = (start_odometer_miles + distance).quantize(
            ODOMETER_PRECISION,
            rounding=ROUND_HALF_UP,
        )
        return MileageCalculation(
            miles=distance,
            mileage_source=MILEAGE_SOURCE_ESTIMATED_ODOMETER,
            start_odometer_miles=start_odometer_miles,
            end_odometer_miles=estimated_end,
            start_odometer_source=start_odometer_source or ODOMETER_SOURCE_PREVIOUS_TRIP,
            end_odometer_source=ODOMETER_SOURCE_ESTIMATED,
        )

    if end_odometer_miles is not None:
        estimated_start = max(end_odometer_miles - distance, Decimal("0.0")).quantize(
            ODOMETER_PRECISION,
            rounding=ROUND_HALF_UP,
        )
        return MileageCalculation(
            miles=distance,
            mileage_source=MILEAGE_SOURCE_ESTIMATED_ODOMETER,
            start_odometer_miles=estimated_start,
            end_odometer_miles=end_odometer_miles,
            start_odometer_source=ODOMETER_SOURCE_ESTIMATED,
            end_odometer_source=end_odometer_source or ODOMETER_SOURCE_MANUAL,
        )

    if odometer_anchor_miles is None:
        return None

    estimated_start = odometer_anchor_miles.quantize(ODOMETER_PRECISION, rounding=ROUND_HALF_UP)
    estimated_end = (estimated_start + distance).quantize(
        ODOMETER_PRECISION,
        rounding=ROUND_HALF_UP,
    )
    return MileageCalculation(
        miles=distance,
        mileage_source=MILEAGE_SOURCE_ESTIMATED_ODOMETER,
        start_odometer_miles=estimated_start,
        end_odometer_miles=estimated_end,
        start_odometer_source=ODOMETER_SOURCE_PREVIOUS_TRIP,
        end_odometer_source=ODOMETER_SOURCE_ESTIMATED,
    )


def _mileage_calculation(
    origin: Site,
    destination: Site,
    *,
    path_miles: Decimal | None,
    start_odometer: OdometerReading | None,
    end_odometer: OdometerReading | None,
    odometer_anchor_miles: Decimal | None,
) -> MileageCalculation:
    start_odometer_miles = _odometer_reading_miles(start_odometer)
    end_odometer_miles = _odometer_reading_miles(end_odometer)
    start_odometer_source = _odometer_reading_source(start_odometer)
    end_odometer_source = _odometer_reading_source(end_odometer)

    if path_miles is not None:
        path_odometer = _estimated_odometer_calculation(
            path_miles,
            start_odometer_miles=start_odometer_miles,
            end_odometer_miles=end_odometer_miles,
            start_odometer_source=start_odometer_source,
            end_odometer_source=end_odometer_source,
            odometer_anchor_miles=odometer_anchor_miles,
        )
        if (
            path_odometer is not None
            and (
                start_odometer_source == ODOMETER_SOURCE_OWNTRACKS_ROLLING
                or end_odometer_source == ODOMETER_SOURCE_OWNTRACKS_ROLLING
            )
        ):
            path_odometer = MileageCalculation(
                miles=path_odometer.miles,
                mileage_source=path_odometer.mileage_source,
                start_odometer_miles=path_odometer.start_odometer_miles,
                end_odometer_miles=path_odometer.end_odometer_miles,
                start_odometer_source=(
                    path_odometer.start_odometer_source or ODOMETER_SOURCE_OWNTRACKS_ROLLING
                ),
                end_odometer_source=ODOMETER_SOURCE_OWNTRACKS_ROLLING,
            )
        return MileageCalculation(
            miles=path_miles.quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP),
            mileage_source=MILEAGE_SOURCE_OWNTRACKS_PATH,
            start_odometer_miles=(
                path_odometer.start_odometer_miles
                if path_odometer is not None
                else start_odometer_miles
            ),
            end_odometer_miles=(
                path_odometer.end_odometer_miles
                if path_odometer is not None
                else end_odometer_miles
            ),
            start_odometer_source=(
                path_odometer.start_odometer_source
                if path_odometer is not None
                else start_odometer_source
            ),
            end_odometer_source=(
                path_odometer.end_odometer_source
                if path_odometer is not None
                else end_odometer_source
            ),
        )

    odometer_miles = _odometer_miles(start_odometer_miles, end_odometer_miles)
    if odometer_miles is not None:
        return MileageCalculation(
            miles=odometer_miles,
            mileage_source=MILEAGE_SOURCE_MANUAL,
            start_odometer_miles=start_odometer_miles,
            end_odometer_miles=end_odometer_miles,
            start_odometer_source=start_odometer_source or ODOMETER_SOURCE_MANUAL,
            end_odometer_source=end_odometer_source or ODOMETER_SOURCE_MANUAL,
        )

    distance_miles = _distance_estimate_miles(origin, destination)
    estimated = _estimated_odometer_calculation(
        distance_miles,
        start_odometer_miles=start_odometer_miles,
        end_odometer_miles=end_odometer_miles,
        start_odometer_source=start_odometer_source,
        end_odometer_source=end_odometer_source,
        odometer_anchor_miles=odometer_anchor_miles,
    )
    if estimated is not None:
        return estimated

    return MileageCalculation(
        miles=distance_miles,
        mileage_source=MILEAGE_SOURCE_WAYPOINT_DISTANCE,
    )


def _site_latitude(site: Site) -> Decimal:
    return Decimal(site.latitude)


def _site_longitude(site: Site) -> Decimal:
    return Decimal(site.longitude)


def _should_skip_for_minimum_miles(origin: Site, destination: Site, miles: Decimal) -> bool:
    if origin.id == destination.id:
        return False
    return miles < get_settings().min_trip_miles


def _mileage_source_rank(value: str | None) -> int:
    if value == MILEAGE_SOURCE_OWNTRACKS_PATH:
        return 4
    if value == MILEAGE_SOURCE_MANUAL:
        return 3
    if value == MILEAGE_SOURCE_ESTIMATED_ODOMETER:
        return 2
    if value == MILEAGE_SOURCE_WAYPOINT_DISTANCE:
        return 1
    return 0


def _calculation_improves_existing_trip(
    existing_trip: Trip,
    calculation: MileageCalculation,
) -> bool:
    existing_rank = _mileage_source_rank(existing_trip.mileage_source)
    new_rank = _mileage_source_rank(calculation.mileage_source)
    if new_rank > existing_rank:
        return True
    if (
        new_rank == existing_rank
        and calculation.mileage_source == MILEAGE_SOURCE_OWNTRACKS_PATH
        and existing_trip.miles != calculation.miles
    ):
        return True
    if new_rank == existing_rank and _new_odometer_details_available(existing_trip, calculation):
        return True
    return False


def _trip_generation_key(
    origin_site_id: int | None,
    destination_site_id: int | None,
    started_at: datetime,
    ended_at: datetime,
) -> TripGenerationKey | None:
    if origin_site_id is None or destination_site_id is None:
        return None
    return (origin_site_id, destination_site_id, started_at, ended_at)


def _apply_trip_values(
    trip: Trip,
    *,
    trip_date: date,
    origin: Site,
    destination: Site,
    started_at: datetime,
    ended_at: datetime,
    calculation: MileageCalculation,
    notes: str,
) -> None:
    trip.trip_date = trip_date
    trip.origin_site_id = origin.id
    trip.destination_site_id = destination.id
    trip.started_at = started_at
    trip.ended_at = ended_at
    trip.start_latitude = _site_latitude(origin)
    trip.start_longitude = _site_longitude(origin)
    trip.end_latitude = _site_latitude(destination)
    trip.end_longitude = _site_longitude(destination)
    trip.origin_name = origin.name
    trip.destination_name = destination.name
    trip.miles = calculation.miles
    trip.start_odometer_miles = calculation.start_odometer_miles
    trip.end_odometer_miles = calculation.end_odometer_miles
    trip.start_odometer_source = calculation.start_odometer_source
    trip.end_odometer_source = calculation.end_odometer_source
    trip.mileage_source = calculation.mileage_source
    trip.source = AUTO_TRIP_SOURCE
    trip.notes = notes


def _add_or_update_trip(
    db: Session,
    generated: list[Trip],
    preserved_trips: list[Trip],
    existing_auto_trips: dict[TripGenerationKey, Trip],
    deleted_trip_keys: set[TripGenerationKey],
    *,
    origin: Site,
    destination: Site,
    started_at: datetime,
    ended_at: datetime,
    inferred_leave: bool,
    path_miles: Decimal | None,
    start_odometer: OdometerReading | None,
    end_odometer: OdometerReading | None,
    odometer_anchor_miles: Decimal | None,
) -> Trip | None:
    if _is_home_to_home(origin, destination):
        trip_logger.debug(
            "trip skipped reason=home_to_home origin=%s destination=%s started_at=%s ended_at=%s",
            origin.name,
            destination.name,
            started_at.isoformat(),
            ended_at.isoformat(),
        )
        return None

    trip_date = datetime_to_local_date(started_at)
    if _overlaps_preserved_trip(
        preserved_trips,
        trip_date=trip_date,
        started_at=started_at,
        ended_at=ended_at,
    ):
        trip_logger.info(
            "trip skipped reason=manual_overlap origin=%s destination=%s trip_date=%s",
            origin.name,
            destination.name,
            trip_date.isoformat(),
        )
        return None

    trip_key = (origin.id, destination.id, started_at, ended_at)
    if trip_key in deleted_trip_keys:
        trip_logger.info(
            "trip skipped reason=user_deleted origin=%s destination=%s started_at=%s ended_at=%s",
            origin.name,
            destination.name,
            started_at.isoformat(),
            ended_at.isoformat(),
        )
        return None

    existing_auto_trip = existing_auto_trips.get(trip_key)
    calculation = _mileage_calculation(
        origin,
        destination,
        path_miles=path_miles,
        start_odometer=start_odometer,
        end_odometer=end_odometer,
        odometer_anchor_miles=odometer_anchor_miles,
    )
    notes = _trip_notes(inferred_leave=inferred_leave)

    if existing_auto_trip is not None and not _calculation_improves_existing_trip(
        existing_auto_trip,
        calculation,
    ):
        trip_logger.debug(
            "trip unchanged origin=%s destination=%s started_at=%s ended_at=%s source=%s",
            origin.name,
            destination.name,
            started_at.isoformat(),
            ended_at.isoformat(),
            existing_auto_trip.mileage_source,
        )
        return existing_auto_trip

    if calculation.mileage_source == MILEAGE_SOURCE_ESTIMATED_ODOMETER:
        notes = _append_note(notes, "Estimated odometer from waypoint distance.")
    elif calculation.mileage_source == MILEAGE_SOURCE_OWNTRACKS_PATH:
        notes = _append_note(notes, "Used OwnTracks location path between waypoint events.")
    elif calculation.mileage_source == MILEAGE_SOURCE_WAYPOINT_DISTANCE:
        notes = _append_note(notes, "Used waypoint distance because odometer data was unavailable.")

    if existing_auto_trip is None and _should_skip_for_minimum_miles(
        origin,
        destination,
        calculation.miles,
    ):
        trip_logger.debug(
            "trip skipped reason=below_minimum origin=%s destination=%s miles=%s",
            origin.name,
            destination.name,
            calculation.miles,
        )
        return None

    if existing_auto_trip is None:
        trip = Trip()
        db.add(trip)
        action = "created"
    else:
        trip = existing_auto_trip
        action = "updated"

    _apply_trip_values(
        trip,
        trip_date=trip_date,
        origin=origin,
        destination=destination,
        started_at=started_at,
        ended_at=ended_at,
        calculation=calculation,
        notes=notes,
    )
    existing_auto_trips[trip_key] = trip
    generated.append(trip)
    trip_logger.info(
        "trip %s date=%s origin=%s destination=%s miles=%s source=%s "
        "start_odometer=%s start_odometer_source=%s end_odometer=%s "
        "end_odometer_source=%s inferred_leave=%s started_at=%s ended_at=%s",
        action,
        trip_date.isoformat(),
        origin.name,
        destination.name,
        calculation.miles,
        calculation.mileage_source,
        calculation.start_odometer_miles,
        calculation.start_odometer_source,
        calculation.end_odometer_miles,
        calculation.end_odometer_source,
        inferred_leave,
        started_at.isoformat(),
        ended_at.isoformat(),
    )
    return trip


def _existing_auto_trips_for_dates(db: Session, source_dates: list[date]) -> dict[
    TripGenerationKey,
    Trip,
]:
    trips = list(
        db.scalars(
            select(Trip)
            .where(Trip.source == AUTO_TRIP_SOURCE)
            .where(Trip.trip_date.in_(source_dates))
            .where(
                Trip.mileage_source.in_(
                    [
                        MILEAGE_SOURCE_MANUAL,
                        MILEAGE_SOURCE_OWNTRACKS_PATH,
                        MILEAGE_SOURCE_ESTIMATED_ODOMETER,
                        MILEAGE_SOURCE_WAYPOINT_DISTANCE,
                    ]
                )
            )
        )
    )
    return {
        (trip.origin_site_id, trip.destination_site_id, trip.started_at, trip.ended_at): trip
        for trip in trips
        if trip.origin_site_id is not None and trip.destination_site_id is not None
    }


def _deleted_trip_keys_for_dates(db: Session, source_dates: list[date]) -> set[TripGenerationKey]:
    deleted_trips = list(
        db.scalars(
            select(DeletedTrip)
            .where(DeletedTrip.trip_date.in_(source_dates))
            .where(DeletedTrip.origin_site_id.is_not(None))
            .where(DeletedTrip.destination_site_id.is_not(None))
        )
    )
    return {
        (
            deleted_trip.origin_site_id,
            deleted_trip.destination_site_id,
            deleted_trip.started_at,
            deleted_trip.ended_at,
        )
        for deleted_trip in deleted_trips
        if deleted_trip.origin_site_id is not None and deleted_trip.destination_site_id is not None
    }


def _update_last_visited_from_confirmed_transitions(transitions: list[WaypointTransition]) -> None:
    """Persist waypoint last-visited timestamps only after dwell-confirmed enter events."""

    for transition in transitions:
        if transition.event != "enter":
            continue
        site = transition.site
        captured_at = transition.location.captured_at
        if site.last_visited_at is None or datetime_to_utc(captured_at) > datetime_to_utc(
            site.last_visited_at
        ):
            site.last_visited_at = captured_at


def _latest_odometer_before(db: Session, before_datetime: datetime) -> Decimal | None:
    """Return the latest stored trip/checkpoint odometer before a trip starts."""

    trip_odometer = db.scalar(
        select(Trip.end_odometer_miles)
        .where(Trip.ended_at < before_datetime)
        .where(Trip.end_odometer_miles.is_not(None))
        .order_by(Trip.ended_at.desc(), Trip.id.desc())
        .limit(1)
    )
    if trip_odometer is not None:
        return trip_odometer

    return db.scalar(
        select(TripProcessingCheckpoint.odometer_anchor_miles)
        .where(TripProcessingCheckpoint.name == AUTOMATIC_TRIP_PROCESSING_CHECKPOINT)
        .where(TripProcessingCheckpoint.odometer_anchor_miles.is_not(None))
        .where(TripProcessingCheckpoint.odometer_anchor_recorded_at <= before_datetime)
        .order_by(TripProcessingCheckpoint.updated_at.desc(), TripProcessingCheckpoint.id.desc())
        .limit(1)
    )


def _month_date_bounds(year: int, month: int) -> tuple[date, date]:
    """Return inclusive start and exclusive end dates for one local report month."""

    start_date = date(year, month, 1)
    end_date = date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    return start_date, end_date


def _month_trips_ordered(db: Session, year: int, month: int) -> list[Trip]:
    """Load month trips in chronological odometer order."""

    start_date, end_date = _month_date_bounds(year, month)
    return list(
        db.scalars(
            select(Trip)
            .where(Trip.trip_date >= start_date)
            .where(Trip.trip_date < end_date)
            .order_by(Trip.trip_date.asc(), Trip.started_at.asc(), Trip.id.asc())
        )
    )


def _latest_checkpoint(db: Session) -> TripProcessingCheckpoint | None:
    """Return the rolling odometer checkpoint when it already exists."""

    return db.scalar(
        select(TripProcessingCheckpoint)
        .where(TripProcessingCheckpoint.name == AUTOMATIC_TRIP_PROCESSING_CHECKPOINT)
        .limit(1)
    )


def _month_resequence_anchor(db: Session, first_trip: Trip) -> Decimal:
    """Choose the stable odometer start point for resequencing a month."""

    prior_odometer = _latest_odometer_before(db, first_trip.started_at)
    if prior_odometer is not None:
        return Decimal(prior_odometer).quantize(ODOMETER_PRECISION, rounding=ROUND_HALF_UP)
    if first_trip.start_odometer_miles is not None:
        return Decimal(first_trip.start_odometer_miles).quantize(
            ODOMETER_PRECISION,
            rounding=ROUND_HALF_UP,
        )

    checkpoint = _latest_checkpoint(db)
    if checkpoint is not None and checkpoint.odometer_anchor_miles is not None:
        return Decimal(checkpoint.odometer_anchor_miles).quantize(
            ODOMETER_PRECISION,
            rounding=ROUND_HALF_UP,
        )
    return Decimal("0.0")


def _update_checkpoint_after_resequence(db: Session, last_trip: Trip) -> None:
    """Move the rolling odometer forward only when the edited month reaches the checkpoint."""

    if last_trip.end_odometer_miles is None:
        return

    checkpoint = _latest_checkpoint(db)
    if checkpoint is None:
        checkpoint = TripProcessingCheckpoint(name=AUTOMATIC_TRIP_PROCESSING_CHECKPOINT)
        db.add(checkpoint)
        db.flush()

    checkpoint_recorded_at = (
        datetime_to_utc(checkpoint.odometer_anchor_recorded_at)
        if checkpoint.odometer_anchor_recorded_at is not None
        else None
    )
    last_trip_ended_at = datetime_to_utc(last_trip.ended_at)
    if checkpoint_recorded_at is not None and checkpoint_recorded_at > last_trip_ended_at:
        return

    checkpoint.odometer_anchor_miles = Decimal(last_trip.end_odometer_miles).quantize(
        ODOMETER_PRECISION,
        rounding=ROUND_HALF_UP,
    )
    checkpoint.odometer_anchor_recorded_at = last_trip_ended_at


def resequence_month_trip_odometers(db: Session, year: int, month: int) -> int:
    """Recalculate every trip odometer in a month from ordered trip distances."""

    month_trips = _month_trips_ordered(db, year, month)
    if not month_trips:
        return 0

    current_odometer = _month_resequence_anchor(db, month_trips[0])
    for trip in month_trips:
        trip_distance = Decimal(trip.miles).quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)
        start_odometer = current_odometer.quantize(ODOMETER_PRECISION, rounding=ROUND_HALF_UP)
        end_odometer = (start_odometer + trip_distance).quantize(
            ODOMETER_PRECISION,
            rounding=ROUND_HALF_UP,
        )
        trip.start_odometer_miles = start_odometer
        trip.end_odometer_miles = end_odometer
        trip.start_odometer_source = ODOMETER_SOURCE_PREVIOUS_TRIP
        trip.end_odometer_source = ODOMETER_SOURCE_ESTIMATED
        current_odometer = end_odometer

    _update_checkpoint_after_resequence(db, month_trips[-1])
    trip_logger.info(
        "Resequenced trip odometers year=%s month=%s trips=%s start_odometer=%s end_odometer=%s",
        year,
        month,
        len(month_trips),
        month_trips[0].start_odometer_miles,
        month_trips[-1].end_odometer_miles,
    )
    return len(month_trips)


def generate_trips(
    db: Session,
    start_date: date,
    end_date: date,
    *,
    as_of: datetime | None = None,
) -> list[Trip]:
    generation_start_dt, generation_end_dt = _date_range_bounds(start_date, end_date)
    dwell_padding = timedelta(minutes=get_settings().owntracks_waypoint_dwell_minutes)
    sites = list(db.scalars(select(Site).order_by(Site.name.asc())))
    sites_by_name, sites_by_region_id = site_indexes(sites)
    locations = _locations_for_range(db, start_date, end_date, end_padding=dwell_padding)
    if not locations:
        trip_logger.debug(
            "trip generation skipped reason=no_locations start_date=%s end_date=%s",
            start_date.isoformat(),
            end_date.isoformat(),
        )
        return []

    transitions = [
        transition
        for transition in _waypoint_transitions(locations, sites)
        if generation_start_dt
        <= datetime_to_utc(transition.location.captured_at)
        < generation_end_dt
    ]
    if not transitions:
        trip_logger.debug(
            "trip generation skipped reason=no_waypoint_transitions start_date=%s end_date=%s",
            start_date.isoformat(),
            end_date.isoformat(),
        )
        return []

    _update_last_visited_from_confirmed_transitions(transitions)
    preserved_trips = _preserved_trips_for_range(db, start_date, end_date)
    source_dates = sorted(
        {datetime_to_local_date(event.location.captured_at) for event in transitions}
    )
    existing_auto_trips = _existing_auto_trips_for_dates(db, source_dates)
    deleted_trip_keys = _deleted_trip_keys_for_dates(db, source_dates)
    home_site = _home_site(sites)
    pending_leave: WaypointTransition | None = None
    last_arrival: WaypointTransition | None = None
    odometer_anchor_miles = _latest_odometer_before(db, transitions[0].location.captured_at)
    generated: list[Trip] = []

    for transition in transitions:
        if transition.event == "leave":
            _odometer_for_transition(db, transition)
            pending_leave = transition
            continue

        end_odometer = _odometer_for_transition(db, transition)
        if pending_leave is not None:
            trip = _add_or_update_trip(
                db,
                generated,
                preserved_trips,
                existing_auto_trips,
                deleted_trip_keys,
                origin=pending_leave.site,
                destination=transition.site,
                started_at=pending_leave.location.captured_at,
                ended_at=transition.location.captured_at,
                inferred_leave=False,
                path_miles=_trip_path_miles(
                    locations,
                    started_at=pending_leave.location.captured_at,
                    ended_at=transition.location.captured_at,
                    sites=sites,
                    sites_by_name=sites_by_name,
                    sites_by_region_id=sites_by_region_id,
                ),
                start_odometer=_odometer_for_transition(db, pending_leave),
                end_odometer=end_odometer,
                odometer_anchor_miles=odometer_anchor_miles,
            )
        elif last_arrival is not None and last_arrival.site.id != transition.site.id:
            trip = _add_or_update_trip(
                db,
                generated,
                preserved_trips,
                existing_auto_trips,
                deleted_trip_keys,
                origin=last_arrival.site,
                destination=transition.site,
                started_at=transition.location.captured_at,
                ended_at=transition.location.captured_at,
                inferred_leave=True,
                path_miles=None,
                start_odometer=_odometer_for_transition(db, last_arrival),
                end_odometer=end_odometer,
                odometer_anchor_miles=odometer_anchor_miles,
            )
        elif last_arrival is None and home_site is not None and home_site.id != transition.site.id:
            trip = _add_or_update_trip(
                db,
                generated,
                preserved_trips,
                existing_auto_trips,
                deleted_trip_keys,
                origin=home_site,
                destination=transition.site,
                started_at=transition.location.captured_at,
                ended_at=transition.location.captured_at,
                inferred_leave=True,
                path_miles=None,
                start_odometer=None,
                end_odometer=end_odometer,
                odometer_anchor_miles=odometer_anchor_miles,
            )
        else:
            trip = None

        if trip is not None and trip.end_odometer_miles is not None:
            odometer_anchor_miles = trip.end_odometer_miles

        last_arrival = transition
        pending_leave = None

    db.commit()
    for trip in generated:
        db.refresh(trip)
    trip_logger.info(
        "trip generation complete start_date=%s end_date=%s transitions=%s generated=%s",
        start_date.isoformat(),
        end_date.isoformat(),
        len(transitions),
        len(generated),
    )
    return generated


def mark_trip_manually_reviewed(trip: Trip) -> None:
    if trip.source == AUTO_TRIP_SOURCE:
        trip.source = MANUAL_TRIP_SOURCE


def suppress_trip_generation_for_deleted_trip(db: Session, trip: Trip) -> DeletedTrip | None:
    """Save an exact source-event tombstone for one deleted automatic trip."""

    trip_key = _trip_generation_key(
        trip.origin_site_id,
        trip.destination_site_id,
        trip.started_at,
        trip.ended_at,
    )
    if trip_key is None:
        return None

    origin_site_id, destination_site_id, started_at, ended_at = trip_key
    deleted_trip = db.scalar(
        select(DeletedTrip)
        .where(DeletedTrip.origin_site_id == origin_site_id)
        .where(DeletedTrip.destination_site_id == destination_site_id)
        .where(DeletedTrip.started_at == started_at)
        .where(DeletedTrip.ended_at == ended_at)
    )
    if deleted_trip is None:
        deleted_trip = DeletedTrip(
            deleted_trip_id=trip.id,
            trip_date=trip.trip_date,
            origin_site_id=origin_site_id,
            destination_site_id=destination_site_id,
            started_at=started_at,
            ended_at=ended_at,
        )
        db.add(deleted_trip)

    deleted_trip.origin_name = trip.origin_display_name
    deleted_trip.destination_name = trip.destination_display_name
    deleted_trip.miles = trip.miles
    deleted_trip.source = trip.source
    deleted_trip.mileage_source = trip.mileage_source
    deleted_trip.reason = "user_deleted"
    deleted_trip.notes = trip.notes or ""
    return deleted_trip


def _preserve_checkpoint_from_deleted_trip(db: Session, trip: Trip) -> None:
    """Keep the rolling odometer current when deleting the newest visible trip."""

    if trip.end_odometer_miles is None:
        return

    checkpoint = _latest_checkpoint(db)
    if checkpoint is None:
        checkpoint = TripProcessingCheckpoint(name=AUTOMATIC_TRIP_PROCESSING_CHECKPOINT)
        db.add(checkpoint)
        db.flush()

    trip_ended_at = datetime_to_utc(trip.ended_at)
    checkpoint_recorded_at = (
        datetime_to_utc(checkpoint.odometer_anchor_recorded_at)
        if checkpoint.odometer_anchor_recorded_at is not None
        else None
    )
    if checkpoint_recorded_at is not None and checkpoint_recorded_at >= trip_ended_at:
        return

    checkpoint.odometer_anchor_miles = Decimal(trip.end_odometer_miles).quantize(
        ODOMETER_PRECISION,
        rounding=ROUND_HALF_UP,
    )
    checkpoint.odometer_anchor_recorded_at = trip_ended_at


def delete_trip(db: Session, trip: Trip) -> DeletedTrip | None:
    _preserve_checkpoint_from_deleted_trip(db, trip)
    deleted_trip = suppress_trip_generation_for_deleted_trip(db, trip)
    db.delete(trip)
    return deleted_trip


def _manual_trip_datetime(trip_date: date) -> datetime:
    start_dt, _ = local_day_bounds(trip_date)
    return start_dt


def create_manual_trip(
    db: Session,
    *,
    trip_date: date,
    origin_name: str,
    destination_name: str,
    miles: Decimal,
) -> Trip:
    started_at = _manual_trip_datetime(trip_date)
    trip = Trip(
        trip_date=trip_date,
        origin_site_id=None,
        destination_site_id=None,
        started_at=started_at,
        ended_at=started_at,
        start_latitude=Decimal("0.0000000"),
        start_longitude=Decimal("0.0000000"),
        end_latitude=Decimal("0.0000000"),
        end_longitude=Decimal("0.0000000"),
        origin_name=normalize_location_name(origin_name),
        destination_name=normalize_location_name(destination_name),
        miles=Decimal(miles).quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP),
        mileage_source=MILEAGE_SOURCE_MANUAL,
        source=MANUAL_TRIP_SOURCE,
        notes=MANUAL_TRIP_NOTE,
    )
    db.add(trip)
    return trip


def update_trip_details(
    trip: Trip,
    origin_name: str,
    destination_name: str,
    miles: Decimal | None = None,
    trip_date: date | None = None,
) -> None:
    if trip_date is not None:
        trip.trip_date = trip_date
        trip.started_at = _manual_trip_datetime(trip_date)
        trip.ended_at = trip.started_at
    trip.origin_name = normalize_location_name(origin_name)
    trip.destination_name = normalize_location_name(destination_name)
    if miles is not None:
        trip.miles = Decimal(miles).quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)
        trip.mileage_source = MILEAGE_SOURCE_MANUAL
    mark_trip_manually_reviewed(trip)


def update_trip_location_names(trip: Trip, origin_name: str, destination_name: str) -> None:
    update_trip_details(trip, origin_name, destination_name)


def monthly_miles(db: Session, year: int, month: int) -> Decimal:
    start_date, end_date = _month_date_bounds(year, month)
    stmt = (
        select(Trip)
        .where(Trip.trip_date >= start_date)
        .where(Trip.trip_date < end_date)
        .order_by(Trip.trip_date.asc(), Trip.started_at.asc(), Trip.id.asc())
    )
    total = sum((trip.miles for trip in db.scalars(stmt)), Decimal("0.0"))
    return total.quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)
