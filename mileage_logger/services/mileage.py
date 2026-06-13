import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from math import asin, cos, radians, sin, sqrt

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from mileage_logger.config import get_settings
from mileage_logger.models import OwnTracksLocation, Site, Trip, normalize_location_name
from mileage_logger.services.fordpass import current_odometer_miles
from mileage_logger.services.timezone import datetime_to_local_date, local_day_bounds

METERS_PER_MILE = Decimal("1609.344")
EARTH_RADIUS_M = Decimal("6371008.8")
AUTO_TRIP_SOURCE = "auto"
MANUAL_TRIP_SOURCE = "manual"
MILEAGE_SOURCE_FORDPASS_ODOMETER = "fordpass_odometer"
MILEAGE_SOURCE_ESTIMATED_ODOMETER = "estimated_odometer"
MILEAGE_SOURCE_WAYPOINT_DISTANCE = "waypoint_distance"
MILEAGE_SOURCE_MANUAL = "manual"
ODOMETER_SOURCE_FORDPASS = "fordpass"
ODOMETER_SOURCE_ESTIMATED = "estimated"
ODOMETER_SOURCE_PREVIOUS_TRIP = "previous_trip"
HOME_WAYPOINT_NAME = "Home"
WAYPOINT_TRIP_NOTE = "Auto-generated from OwnTracks waypoint transitions."
MISSING_LEAVE_NOTE = "Missing leave event inferred from previous waypoint."
ODOMETER_PAYLOAD_KEY = "mileage_logger_fordpass_odometer_miles"
ODOMETER_ATTEMPTED_PAYLOAD_KEY = "mileage_logger_fordpass_odometer_attempted"
trip_logger = logging.getLogger("mileage_logger.trip_calculation")


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


def _date_range_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    start_dt, _ = local_day_bounds(start_date)
    _, end_dt = local_day_bounds(end_date)
    return start_dt, end_dt


def date_bounds(day: date) -> tuple[datetime, datetime]:
    return local_day_bounds(day)


def _locations_for_range(db: Session, start_date: date, end_date: date) -> list[OwnTracksLocation]:
    start_dt, end_dt = _date_range_bounds(start_date, end_date)
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


def _waypoint_transitions(
    locations: list[OwnTracksLocation],
    sites: list[Site],
) -> list[WaypointTransition]:
    sites_by_name = {site.name.casefold(): site for site in sites}
    sites_by_region_id = {
        site.owntracks_region_id: site for site in sites if site.owntracks_region_id is not None
    }
    transitions: list[WaypointTransition] = []
    seen: set[tuple[str, int, datetime]] = set()

    for location in locations:
        event = _transition_event(location)
        if event is None:
            continue
        site = site_for_location(location, sites, sites_by_name, sites_by_region_id)
        if site is None:
            continue
        dedupe_key = (event, site.id, location.captured_at)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        transitions.append(WaypointTransition(event=event, site=site, location=location))

    return transitions


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


def _payload_odometer_miles(location: OwnTracksLocation) -> Decimal | None:
    value = (location.raw_payload or {}).get(ODOMETER_PAYLOAD_KEY)
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def _set_payload_odometer_miles(location: OwnTracksLocation, odometer_miles: Decimal) -> None:
    payload = dict(location.raw_payload or {})
    payload[ODOMETER_PAYLOAD_KEY] = str(
        odometer_miles.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    )
    payload[ODOMETER_ATTEMPTED_PAYLOAD_KEY] = True
    location.raw_payload = payload


def _set_payload_odometer_attempted(location: OwnTracksLocation) -> None:
    payload = dict(location.raw_payload or {})
    payload[ODOMETER_ATTEMPTED_PAYLOAD_KEY] = True
    location.raw_payload = payload


def _odometer_for_transition(transition: WaypointTransition) -> Decimal | None:
    odometer = _payload_odometer_miles(transition.location)
    if odometer is not None:
        return odometer
    if (transition.location.raw_payload or {}).get(ODOMETER_ATTEMPTED_PAYLOAD_KEY):
        return None

    odometer = current_odometer_miles()
    if odometer is None:
        _set_payload_odometer_attempted(transition.location)
        log = trip_logger.warning if get_settings().fordpass_enabled else trip_logger.debug
        log(
            "Odometer unavailable site=%s event=%s captured_at=%s fordpass_enabled=%s",
            transition.site.name,
            transition.event,
            transition.location.captured_at.isoformat(),
            get_settings().fordpass_enabled,
        )
        return None

    _set_payload_odometer_miles(transition.location, odometer)
    trip_logger.debug(
        "Captured odometer site=%s event=%s odometer=%s captured_at=%s",
        transition.site.name,
        transition.event,
        odometer,
        transition.location.captured_at.isoformat(),
    )
    return odometer


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
    return miles.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


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
    odometer_anchor_miles: Decimal | None,
) -> MileageCalculation | None:
    distance = distance_miles.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if start_odometer_miles is not None:
        estimated_end = (start_odometer_miles + distance).quantize(
            Decimal("0.001"),
            rounding=ROUND_HALF_UP,
        )
        return MileageCalculation(
            miles=distance,
            mileage_source=MILEAGE_SOURCE_ESTIMATED_ODOMETER,
            start_odometer_miles=start_odometer_miles,
            end_odometer_miles=estimated_end,
            start_odometer_source=ODOMETER_SOURCE_FORDPASS,
            end_odometer_source=ODOMETER_SOURCE_ESTIMATED,
        )

    if end_odometer_miles is not None:
        estimated_start = max(end_odometer_miles - distance, Decimal("0.000")).quantize(
            Decimal("0.001"),
            rounding=ROUND_HALF_UP,
        )
        return MileageCalculation(
            miles=distance,
            mileage_source=MILEAGE_SOURCE_ESTIMATED_ODOMETER,
            start_odometer_miles=estimated_start,
            end_odometer_miles=end_odometer_miles,
            start_odometer_source=ODOMETER_SOURCE_ESTIMATED,
            end_odometer_source=ODOMETER_SOURCE_FORDPASS,
        )

    if odometer_anchor_miles is None:
        return None

    estimated_start = odometer_anchor_miles.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    estimated_end = (estimated_start + distance).quantize(
        Decimal("0.001"),
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
    start_odometer_miles: Decimal | None,
    end_odometer_miles: Decimal | None,
    odometer_anchor_miles: Decimal | None,
) -> MileageCalculation:
    odometer_miles = _odometer_miles(start_odometer_miles, end_odometer_miles)
    if odometer_miles is not None:
        return MileageCalculation(
            miles=odometer_miles,
            mileage_source=MILEAGE_SOURCE_FORDPASS_ODOMETER,
            start_odometer_miles=start_odometer_miles,
            end_odometer_miles=end_odometer_miles,
            start_odometer_source=ODOMETER_SOURCE_FORDPASS,
            end_odometer_source=ODOMETER_SOURCE_FORDPASS,
        )

    distance_miles = _distance_estimate_miles(origin, destination)
    estimated = _estimated_odometer_calculation(
        distance_miles,
        start_odometer_miles=start_odometer_miles,
        end_odometer_miles=end_odometer_miles,
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


def _add_trip(
    db: Session,
    generated: list[Trip],
    preserved_trips: list[Trip],
    existing_auto_trips: dict[tuple[int, int, datetime, datetime], Trip],
    *,
    origin: Site,
    destination: Site,
    started_at: datetime,
    ended_at: datetime,
    inferred_leave: bool,
    start_odometer_miles: Decimal | None,
    end_odometer_miles: Decimal | None,
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
    existing_auto_trip = existing_auto_trips.get(trip_key)
    if existing_auto_trip is not None:
        calculation = MileageCalculation(
            miles=Decimal(existing_auto_trip.miles).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            ),
            mileage_source=existing_auto_trip.mileage_source,
            start_odometer_miles=existing_auto_trip.start_odometer_miles,
            end_odometer_miles=existing_auto_trip.end_odometer_miles,
            start_odometer_source=existing_auto_trip.start_odometer_source,
            end_odometer_source=existing_auto_trip.end_odometer_source,
        )
        notes = existing_auto_trip.notes
    else:
        calculation = _mileage_calculation(
            origin,
            destination,
            start_odometer_miles=start_odometer_miles,
            end_odometer_miles=end_odometer_miles,
            odometer_anchor_miles=odometer_anchor_miles,
        )
        notes = _trip_notes(inferred_leave=inferred_leave)

    if calculation.mileage_source == MILEAGE_SOURCE_ESTIMATED_ODOMETER:
        notes = _append_note(notes, "Estimated odometer from waypoint distance.")
    elif calculation.mileage_source == MILEAGE_SOURCE_WAYPOINT_DISTANCE:
        notes = _append_note(notes, "Used waypoint distance because odometer data was unavailable.")

    if _should_skip_for_minimum_miles(origin, destination, calculation.miles):
        trip_logger.debug(
            "trip skipped reason=below_minimum origin=%s destination=%s miles=%s",
            origin.name,
            destination.name,
            calculation.miles,
        )
        return None

    trip = Trip(
        trip_date=trip_date,
        origin_site_id=origin.id,
        destination_site_id=destination.id,
        started_at=started_at,
        ended_at=ended_at,
        start_latitude=_site_latitude(origin),
        start_longitude=_site_longitude(origin),
        end_latitude=_site_latitude(destination),
        end_longitude=_site_longitude(destination),
        origin_name=origin.name,
        destination_name=destination.name,
        miles=calculation.miles,
        start_odometer_miles=calculation.start_odometer_miles,
        end_odometer_miles=calculation.end_odometer_miles,
        start_odometer_source=calculation.start_odometer_source,
        end_odometer_source=calculation.end_odometer_source,
        mileage_source=calculation.mileage_source,
        source=AUTO_TRIP_SOURCE,
        notes=notes,
    )
    db.add(trip)
    generated.append(trip)
    trip_logger.info(
        "trip created date=%s origin=%s destination=%s miles=%s source=%s "
        "start_odometer=%s start_odometer_source=%s end_odometer=%s "
        "end_odometer_source=%s inferred_leave=%s started_at=%s ended_at=%s",
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
    tuple[int, int, datetime, datetime],
    Trip,
]:
    trips = list(
        db.scalars(
            select(Trip)
            .where(Trip.source == AUTO_TRIP_SOURCE)
            .where(Trip.trip_date.in_(source_dates))
            .where(
                Trip.mileage_source.in_(
                    [MILEAGE_SOURCE_FORDPASS_ODOMETER, MILEAGE_SOURCE_ESTIMATED_ODOMETER]
                )
            )
        )
    )
    return {
        (trip.origin_site_id, trip.destination_site_id, trip.started_at, trip.ended_at): trip
        for trip in trips
        if trip.origin_site_id is not None and trip.destination_site_id is not None
    }


def _latest_odometer_before(db: Session, before_datetime: datetime) -> Decimal | None:
    return db.scalar(
        select(Trip.end_odometer_miles)
        .where(Trip.ended_at < before_datetime)
        .where(Trip.end_odometer_miles.is_not(None))
        .order_by(Trip.ended_at.desc(), Trip.id.desc())
        .limit(1)
    )


def generate_trips(
    db: Session,
    start_date: date,
    end_date: date,
    *,
    as_of: datetime | None = None,
) -> list[Trip]:
    sites = list(db.scalars(select(Site).order_by(Site.name.asc())))
    locations = _locations_for_range(db, start_date, end_date)
    if not locations:
        trip_logger.debug(
            "trip generation skipped reason=no_locations start_date=%s end_date=%s",
            start_date.isoformat(),
            end_date.isoformat(),
        )
        return []

    transitions = _waypoint_transitions(locations, sites)
    if not transitions:
        trip_logger.debug(
            "trip generation skipped reason=no_waypoint_transitions start_date=%s end_date=%s",
            start_date.isoformat(),
            end_date.isoformat(),
        )
        return []

    preserved_trips = _preserved_trips_for_range(db, start_date, end_date)
    source_dates = sorted(
        {datetime_to_local_date(event.location.captured_at) for event in transitions}
    )
    existing_auto_trips = _existing_auto_trips_for_dates(db, source_dates)
    delete_result = db.execute(
        delete(Trip)
        .where(Trip.source == AUTO_TRIP_SOURCE)
        .where(Trip.trip_date.in_(source_dates))
    )
    deleted = delete_result.rowcount or 0
    if deleted:
        trip_logger.info(
            "Deleted existing auto trips before regeneration count=%s dates=%s",
            deleted,
            ",".join(day.isoformat() for day in source_dates),
        )

    home_site = _home_site(sites)
    pending_leave: WaypointTransition | None = None
    last_arrival: WaypointTransition | None = None
    odometer_anchor_miles = _latest_odometer_before(db, transitions[0].location.captured_at)
    generated: list[Trip] = []

    for transition in transitions:
        if transition.event == "leave":
            _odometer_for_transition(transition)
            pending_leave = transition
            continue

        end_odometer_miles = _odometer_for_transition(transition)
        if pending_leave is not None:
            trip = _add_trip(
                db,
                generated,
                preserved_trips,
                existing_auto_trips,
                origin=pending_leave.site,
                destination=transition.site,
                started_at=pending_leave.location.captured_at,
                ended_at=transition.location.captured_at,
                inferred_leave=False,
                start_odometer_miles=_odometer_for_transition(pending_leave),
                end_odometer_miles=end_odometer_miles,
                odometer_anchor_miles=odometer_anchor_miles,
            )
        elif last_arrival is not None and last_arrival.site.id != transition.site.id:
            trip = _add_trip(
                db,
                generated,
                preserved_trips,
                existing_auto_trips,
                origin=last_arrival.site,
                destination=transition.site,
                started_at=transition.location.captured_at,
                ended_at=transition.location.captured_at,
                inferred_leave=True,
                start_odometer_miles=_odometer_for_transition(last_arrival),
                end_odometer_miles=end_odometer_miles,
                odometer_anchor_miles=odometer_anchor_miles,
            )
        elif last_arrival is None and home_site is not None and home_site.id != transition.site.id:
            trip = _add_trip(
                db,
                generated,
                preserved_trips,
                existing_auto_trips,
                origin=home_site,
                destination=transition.site,
                started_at=transition.location.captured_at,
                ended_at=transition.location.captured_at,
                inferred_leave=True,
                start_odometer_miles=None,
                end_odometer_miles=end_odometer_miles,
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


def update_trip_details(
    trip: Trip,
    origin_name: str,
    destination_name: str,
    miles: Decimal | None = None,
) -> None:
    trip.origin_name = normalize_location_name(origin_name)
    trip.destination_name = normalize_location_name(destination_name)
    if miles is not None:
        trip.miles = Decimal(miles).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        trip.mileage_source = MILEAGE_SOURCE_MANUAL
    mark_trip_manually_reviewed(trip)


def update_trip_location_names(trip: Trip, origin_name: str, destination_name: str) -> None:
    update_trip_details(trip, origin_name, destination_name)


def monthly_miles(db: Session, year: int, month: int) -> Decimal:
    start_date = date(year, month, 1)
    end_date = date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    stmt = (
        select(Trip)
        .where(Trip.trip_date >= start_date)
        .where(Trip.trip_date < end_date)
        .order_by(Trip.trip_date.asc(), Trip.started_at.asc(), Trip.id.asc())
    )
    total = sum((trip.miles for trip in db.scalars(stmt)), Decimal("0.00"))
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
