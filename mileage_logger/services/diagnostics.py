from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from math import ceil

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mileage_logger.config import get_settings
from mileage_logger.models import OwnTracksLocation, Site
from mileage_logger.services.mileage import (
    haversine_miles,
    site_for_location,
)
from mileage_logger.services.timezone import datetime_to_utc

# OwnTracks reports velocity as kilometers per hour, while the app displays miles.
MILES_PER_KILOMETER = Decimal("0.621371")

# Point-to-point speed divides distance by elapsed hours, so keep the conversion explicit.
MINUTES_PER_HOUR = Decimal("60")


@dataclass(frozen=True)
class OwnTracksEntriesPage:
    entries: list[OwnTracksLocation]
    page: int
    page_size: int
    total: int
    total_pages: int

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages

    @property
    def first_item(self) -> int:
        if self.total == 0:
            return 0
        return ((self.page - 1) * self.page_size) + 1

    @property
    def last_item(self) -> int:
        return min(self.page * self.page_size, self.total)


@dataclass(frozen=True)
class OwnTracksStateChange:
    """A single high-signal movement state transition for the Diagnostics page."""

    captured_at: datetime
    state: str
    label: str
    site_name: str | None = None
    speed_mph: Decimal | None = None


@dataclass(frozen=True)
class CurrentOwnTracksState:
    """The latest inferred OwnTracks state shown at the top of the Diagnostics page."""

    state: str
    label: str
    site_name: str | None = None
    arrived_at: datetime | None = None
    detected_at: datetime | None = None
    speed_mph: Decimal | None = None


@dataclass(frozen=True)
class OwnTracksMovementDiagnostics:
    """Current OwnTracks state and the recent transition-only state log."""

    current_state: CurrentOwnTracksState
    state_changes: list[OwnTracksStateChange]


def paginated_owntracks_entries(
    db: Session,
    *,
    page: int = 1,
    page_size: int = 20,
) -> OwnTracksEntriesPage:
    page_size = max(page_size, 1)
    total = db.scalar(select(func.count(OwnTracksLocation.id))) or 0
    total_pages = max(1, ceil(total / page_size))
    current_page = min(max(page, 1), total_pages)
    offset = (current_page - 1) * page_size
    newest_entries = list(
        db.scalars(
            select(OwnTracksLocation)
            .order_by(OwnTracksLocation.id.desc())
            .offset(offset)
            .limit(page_size)
        )
    )
    entries = list(reversed(newest_entries))
    return OwnTracksEntriesPage(
        entries=entries,
        page=current_page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
    )


def recent_owntracks_entries(db: Session, limit: int = 20) -> list[OwnTracksLocation]:
    return paginated_owntracks_entries(db, page=1, page_size=limit).entries


def _payload_type(location: OwnTracksLocation) -> str:
    """Return the normalized OwnTracks payload type for a stored location row."""

    return str((location.raw_payload or {}).get("_type") or "").strip().casefold()


def _transition_event(location: OwnTracksLocation) -> str | None:
    """Return a normalized waypoint transition event name when the payload has one."""

    if _payload_type(location) != "transition":
        return None
    event = str((location.raw_payload or {}).get("event") or "").strip().casefold()
    if event in {"enter", "arrive", "arrival"}:
        return "enter"
    if event in {"leave", "exit", "departure"}:
        return "leave"
    return None


def _site_indexes(sites: list[Site]) -> tuple[dict[str, Site], dict[str, Site]]:
    """Build lookup tables used to match OwnTracks payload names and region ids to saved sites."""

    sites_by_name = {site.name.casefold(): site for site in sites}
    sites_by_region_id = {
        site.owntracks_region_id: site
        for site in sites
        if site.owntracks_region_id is not None
    }
    return sites_by_name, sites_by_region_id


def _site_for_location(
    location: OwnTracksLocation,
    sites: list[Site],
    sites_by_name: dict[str, Site],
    sites_by_region_id: dict[str, Site],
) -> Site | None:
    """Match a non-leave OwnTracks row to a saved site using normal waypoint matching rules."""

    if _transition_event(location) == "leave":
        return None
    return site_for_location(location, sites, sites_by_name, sites_by_region_id)


def _payload_speed_mph(location: OwnTracksLocation) -> Decimal | None:
    """Read OwnTracks payload velocity and convert it from kilometers per hour to miles per hour."""

    value = (location.raw_payload or {}).get("vel")
    if value is None:
        return None
    try:
        kilometers_per_hour = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if kilometers_per_hour < 0:
        return None
    return (kilometers_per_hour * MILES_PER_KILOMETER).quantize(
        Decimal("0.1"),
        rounding=ROUND_HALF_UP,
    )


def _computed_speed_mph(
    previous_location: OwnTracksLocation | None,
    location: OwnTracksLocation,
    *,
    window_minutes: int,
) -> Decimal | None:
    """Compute point-to-point speed when OwnTracks did not include a velocity field."""

    if previous_location is None:
        return None
    previous_dt = datetime_to_utc(previous_location.captured_at)
    current_dt = datetime_to_utc(location.captured_at)
    elapsed_seconds = Decimal(str((current_dt - previous_dt).total_seconds()))
    if elapsed_seconds <= 0:
        return None
    elapsed_minutes = elapsed_seconds / Decimal("60")
    if elapsed_minutes > Decimal(window_minutes):
        return None
    miles = haversine_miles(
        previous_location.latitude,
        previous_location.longitude,
        location.latitude,
        location.longitude,
    )
    hours = elapsed_minutes / MINUTES_PER_HOUR
    if hours <= 0:
        return None
    return (miles / hours).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _location_speed_mph(
    previous_location: OwnTracksLocation | None,
    location: OwnTracksLocation,
    *,
    window_minutes: int,
) -> Decimal | None:
    """Prefer OwnTracks-reported velocity and fall back to bounded point-to-point speed."""

    payload_speed = _payload_speed_mph(location)
    if payload_speed is not None:
        return payload_speed
    return _computed_speed_mph(
        previous_location,
        location,
        window_minutes=window_minutes,
    )


def _latest_arrival_at_for_site(
    db: Session,
    site: Site,
    sites: list[Site],
    sites_by_name: dict[str, Site],
    sites_by_region_id: dict[str, Site],
    *,
    before_or_at: datetime | None,
    transition_limit: int = 200,
) -> datetime | None:
    """Find the latest saved OwnTracks enter transition for the displayed current site."""

    # Transition rows are sparse compared with location rows, so this query stays bounded while
    # still reaching farther back than the high-volume movement history used for state replay.
    query = (
        select(OwnTracksLocation)
        .where(OwnTracksLocation.raw_payload["_type"].as_string() == "transition")
        .order_by(OwnTracksLocation.captured_at.desc(), OwnTracksLocation.id.desc())
        .limit(transition_limit)
    )
    if before_or_at is not None:
        query = query.where(OwnTracksLocation.captured_at <= before_or_at)

    for location in db.scalars(query):
        if _transition_event(location) != "enter":
            continue
        event_site = site_for_location(location, sites, sites_by_name, sites_by_region_id)
        if event_site is not None and event_site.id == site.id:
            return location.captured_at
    return None


def owntracks_movement_diagnostics(
    db: Session,
    *,
    history_limit: int = 500,
    state_change_limit: int = 30,
) -> OwnTracksMovementDiagnostics:
    """Infer current waypoint/driving state and recent state changes from OwnTracks rows."""

    settings = get_settings()
    # The state machine needs chronological rows, but loading newest first keeps the query bounded.
    newest_locations = list(
        db.scalars(
            select(OwnTracksLocation)
            .order_by(OwnTracksLocation.id.desc())
            .limit(history_limit)
        )
    )
    locations = list(reversed(newest_locations))
    sites = list(db.scalars(select(Site).where(Site.active.is_(True)).order_by(Site.name.asc())))
    sites_by_name, sites_by_region_id = _site_indexes(sites)

    # State variables represent the latest known position while replaying stored OwnTracks rows.
    state_changes: list[OwnTracksStateChange] = []
    current_site: Site | None = None
    arrived_at: datetime | None = None
    driving_active = False
    latest_speed_mph: Decimal | None = None
    latest_detected_at: datetime | None = None
    previous_location: OwnTracksLocation | None = None

    for location in locations:
        event = _transition_event(location)
        event_site = site_for_location(location, sites, sites_by_name, sites_by_region_id)
        location_site = _site_for_location(location, sites, sites_by_name, sites_by_region_id)
        speed_mph = _location_speed_mph(
            previous_location,
            location,
            window_minutes=settings.owntracks_driving_window_minutes,
        )
        latest_speed_mph = speed_mph
        latest_detected_at = location.captured_at

        if event == "enter" and event_site is not None:
            current_site = event_site
            arrived_at = location.captured_at
            driving_active = False
            state_changes.append(
                OwnTracksStateChange(
                    captured_at=location.captured_at,
                    state="arrived",
                    label="Arrived at waypoint",
                    site_name=event_site.name,
                )
            )
        elif event == "leave" and event_site is not None:
            if current_site is not None and current_site.id == event_site.id:
                current_site = None
                arrived_at = None
            driving_active = False
            state_changes.append(
                OwnTracksStateChange(
                    captured_at=location.captured_at,
                    state="left",
                    label="Left waypoint",
                    site_name=event_site.name,
                )
            )
        elif location_site is not None:
            if current_site is None or current_site.id != location_site.id:
                current_site = location_site
                arrived_at = location.captured_at
                if previous_location is not None:
                    state_changes.append(
                        OwnTracksStateChange(
                            captured_at=location.captured_at,
                            state="arrived",
                            label="Arrived at waypoint",
                            site_name=location_site.name,
                        )
                    )
            driving_active = False
        else:
            if current_site is not None:
                state_changes.append(
                    OwnTracksStateChange(
                        captured_at=location.captured_at,
                        state="left",
                        label="Left waypoint",
                        site_name=current_site.name,
                    )
                )
                current_site = None
                arrived_at = None

            if (
                speed_mph is not None
                and speed_mph >= settings.owntracks_driving_speed_mph
                and not driving_active
            ):
                driving_active = True
                state_changes.append(
                    OwnTracksStateChange(
                        captured_at=location.captured_at,
                        state="driving",
                        label="Driving detected",
                        speed_mph=speed_mph,
                    )
                )
            elif speed_mph is not None and speed_mph < settings.owntracks_driving_speed_mph:
                driving_active = False

        previous_location = location

    if current_site is not None:
        resolved_arrived_at = _latest_arrival_at_for_site(
            db,
            current_site,
            sites,
            sites_by_name,
            sites_by_region_id,
            before_or_at=latest_detected_at,
        )
        current_state = CurrentOwnTracksState(
            state="waypoint",
            label="Inside waypoint",
            site_name=current_site.name,
            arrived_at=resolved_arrived_at or arrived_at or current_site.last_visited_at,
            detected_at=latest_detected_at,
            speed_mph=latest_speed_mph,
        )
    elif driving_active:
        current_state = CurrentOwnTracksState(
            state="driving",
            label="Driving detected",
            detected_at=latest_detected_at,
            speed_mph=latest_speed_mph,
        )
    elif locations:
        current_state = CurrentOwnTracksState(
            state="away",
            label="Away from saved waypoints",
            detected_at=latest_detected_at,
            speed_mph=latest_speed_mph,
        )
    else:
        current_state = CurrentOwnTracksState(state="unknown", label="No OwnTracks data")

    return OwnTracksMovementDiagnostics(
        current_state=current_state,
        state_changes=list(reversed(state_changes[-state_change_limit:])),
    )
