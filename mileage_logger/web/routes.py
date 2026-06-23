import logging
import re
import shutil
from calendar import month_name
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from math import ceil
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, joinedload

from mileage_logger.config import get_settings
from mileage_logger.database import get_db
from mileage_logger.logging_config import redact_sensitive_text
from mileage_logger.models import (
    AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
    DeletedTrip,
    GasPriceSnapshot,
    MonthlyGasPrice,
    OwnTracksLocation,
    Site,
    Trip,
    TripProcessingCheckpoint,
)
from mileage_logger.services.backups import (
    BACKUP_MEDIA_TYPE,
    AutomaticBackupFile,
    BackupValidationError,
    create_full_backup,
    list_automatic_backup_files,
    read_automatic_backup_content,
    restore_full_backup,
)
from mileage_logger.services.diagnostics import (
    owntracks_movement_diagnostics,
    paginated_owntracks_entries,
)
from mileage_logger.services.gas_prices import (
    EiaSeriesProvider,
    GasPriceUnavailable,
    get_or_create_monthly_price,
    refresh_current_monthly_price,
)
from mileage_logger.services.login_failures import (
    record_web_login_failure,
    tail_login_failure_entries,
)
from mileage_logger.services.mileage import (
    MILEAGE_SOURCE_MANUAL,
    create_manual_trip,
    delete_trip,
    mark_trip_manually_reviewed,
    owntracks_segment_miles,
    resequence_month_trip_odometers,
    site_indexes,
)
from mileage_logger.services.pdf import generate_monthly_pdf
from mileage_logger.services.timezone import (
    datetime_to_local,
    datetime_to_utc,
    local_day_bounds,
    local_now,
    local_today,
)
from mileage_logger.services.trip_processor import update_odometer_anchor_from_reading
from mileage_logger.services.waypoints import owntracks_waypoints_json
from mileage_logger.web.auth import (
    authenticate_web_credentials,
    clear_login_failures,
    clear_request_authentication,
    login_failure_state,
    login_is_locked,
    login_lockout_remaining_seconds,
    mark_request_authenticated,
    record_login_failure,
    request_is_authenticated,
    valid_next_path,
    web_login_enabled,
)

router = APIRouter()
logger = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
ICON_DIR = STATIC_DIR / "icons"
templates = Jinja2Templates(directory=[WEB_DIR / "templates", WEB_DIR / "static"])


def _format_local_datetime(value, fmt: str = "%Y-%m-%d %I:%M:%S %p %Z") -> str:
    if value is None:
        return ""
    return datetime_to_local(value).strftime(fmt)


def _format_odometer(value) -> str:
    if value is None:
        return "-"
    return f"{Decimal(value):.1f}"


def _format_odometer_source(value) -> str:
    labels = {
        "estimated": "Estimated",
        "previous_trip": "Previous trip",
        "manual": "Manual",
        "manual_odometer": "Manual odometer",
        "owntracks_path": "OwnTracks path",
        "owntracks_rolling": "OwnTracks rolling",
        "owntracks_estimate": "OwnTracks estimate",
        "estimated_odometer": "Estimated",
        "waypoint_distance": "Waypoint distance",
    }
    if value is None:
        return "-"
    return labels.get(str(value), str(value).replace("_", " ").title())


templates.env.filters["local_datetime"] = _format_local_datetime
templates.env.filters["odometer"] = _format_odometer
templates.env.filters["odometer_source"] = _format_odometer_source
templates.env.globals["web_login_enabled"] = web_login_enabled
WAYPOINT_PAGE_SIZE = 20
DISTANCE_PRECISION = Decimal("0.1")
LOG_LINE_LEVEL_RE = re.compile(r"\s(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+\[")
LOG_LEVEL_VALUES = {
    "debug": 10,
    "info": 20,
    "warning": 30,
}
LOG_LINE_LEVEL_VALUES = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


@router.get("/manifest.webmanifest", include_in_schema=False)
def web_manifest() -> FileResponse:
    """Serve the installable web-app manifest from a root URL for phone browsers."""

    return FileResponse(
        STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/site.webmanifest", include_in_schema=False)
def site_manifest() -> FileResponse:
    """Serve the same manifest at the common fallback path used by some browsers."""

    return web_manifest()


@router.get("/service-worker.js", include_in_schema=False)
def service_worker() -> FileResponse:
    """Serve the install service worker without caching sensitive app responses."""

    return FileResponse(
        STATIC_DIR / "service-worker.js",
        media_type="text/javascript; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
        },
    )


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    """Serve the launcher icon as the browser favicon at the standard root path."""

    return FileResponse(ICON_DIR / "favicon.ico", media_type="image/x-icon")


@router.get("/apple-touch-icon.png", include_in_schema=False)
def apple_touch_icon() -> FileResponse:
    """Serve the iOS home-screen icon at Apple's default discovery path."""

    return FileResponse(ICON_DIR / "mileage-logger-apple-touch-icon.png", media_type="image/png")


@dataclass(frozen=True)
class LogLine:
    text: str
    level: str
    css_class: str


@dataclass(frozen=True)
class ApiTestResult:
    status: str
    message: str
    value: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "pass"


@dataclass(frozen=True)
class DiagnosticAutomaticBackup:
    """Automatic backup metadata rendered on the Diagnostics page."""

    filename: str
    created_at_display: str
    size_display: str
    download_url: str


@dataclass(frozen=True)
class DiagnosticDiskUsage:
    """One logical disk usage row for Diagnostics, possibly covering several paths."""

    paths: tuple[str, ...]
    inspected_path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    total_display: str
    used_display: str
    free_display: str
    used_percent_display: str

    @property
    def primary_path(self) -> str:
        return self.paths[0] if self.paths else self.inspected_path


def _current_year_month() -> tuple[int, int]:
    today = local_today()
    return _year_month_for_local_date(today)


def _year_month_for_local_date(today: date) -> tuple[int, int]:
    """Return dashboard calendar selectors for an already-resolved local date."""

    return today.year, today.month


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    month_index = (year * 12) + month - 1 + offset
    return month_index // 12, (month_index % 12) + 1


def _validate_month(month: int) -> None:
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="month must be 1 through 12")


def _quantize_distance(value: Decimal) -> Decimal:
    """Round dashboard distance totals to the displayed one-decimal precision."""

    return Decimal(value).quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)


def _distance_components(total_distance: Decimal, trip_distance: Decimal) -> dict[str, Decimal]:
    """Return dashboard distance components with a non-negative non-trip remainder."""

    trip_total = _quantize_distance(trip_distance)
    combined_total = max(_quantize_distance(total_distance), trip_total)
    non_trip_total = _quantize_distance(max(combined_total - trip_total, Decimal("0.0")))
    return {
        "total": _quantize_distance(trip_total + non_trip_total),
        "trips": trip_total,
        "non_trips": non_trip_total,
    }


def _month_date_bounds(year: int, month: int) -> tuple[date, date]:
    """Return inclusive and exclusive local dates for a dashboard month."""

    start_date = date(year, month, 1)
    end_date = date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    return start_date, end_date


def _month_datetime_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """Return UTC datetime bounds for one complete local dashboard month."""

    start_date, end_date = _month_date_bounds(year, month)
    start_dt, _ = local_day_bounds(start_date)
    end_dt, _ = local_day_bounds(end_date)
    return start_dt, end_dt


def _trip_miles_for_date_range(db: Session, start_date: date, end_date: date) -> Decimal:
    """Sum stored trip miles for a half-open local date range."""

    total = db.scalar(
        select(func.coalesce(func.sum(Trip.miles), 0))
        .where(Trip.trip_date >= start_date)
        .where(Trip.trip_date < end_date)
    )
    return _quantize_distance(Decimal(str(total or "0.0")))


def _owntracks_location_before(
    db: Session,
    before_dt: datetime,
) -> OwnTracksLocation | None:
    """Return the latest OwnTracks row before a UTC boundary."""

    return db.scalar(
        select(OwnTracksLocation)
        .where(OwnTracksLocation.captured_at < before_dt)
        .order_by(OwnTracksLocation.captured_at.desc(), OwnTracksLocation.id.desc())
        .limit(1)
    )


def _owntracks_locations_in_range(
    db: Session,
    start_dt: datetime,
    end_dt: datetime,
) -> list[OwnTracksLocation]:
    """Return OwnTracks rows inside a UTC range in chronological order."""

    return list(
        db.scalars(
            select(OwnTracksLocation)
            .where(OwnTracksLocation.captured_at >= start_dt)
            .where(OwnTracksLocation.captured_at < end_dt)
            .order_by(OwnTracksLocation.captured_at.asc(), OwnTracksLocation.id.asc())
        )
    )


def _owntracks_path_locations_for_datetime_range(
    db: Session,
    start_dt: datetime,
    end_dt: datetime,
) -> list[OwnTracksLocation]:
    """Return path rows used to count segments ending inside the UTC range."""

    previous_location = _owntracks_location_before(db, start_dt)
    locations = _owntracks_locations_in_range(db, start_dt, end_dt)
    if previous_location is None:
        return locations
    return [previous_location, *locations]


def _owntracks_total_miles_for_datetime_range(
    db: Session,
    start_dt: datetime,
    end_dt: datetime,
) -> Decimal:
    """Calculate total driven distance directly from OwnTracks coordinate points."""

    path_locations = _owntracks_path_locations_for_datetime_range(db, start_dt, end_dt)
    if len(path_locations) < 2:
        return Decimal("0.0")

    sites = list(db.scalars(select(Site).where(Site.active.is_(True)).order_by(Site.name.asc())))
    sites_by_name, sites_by_region_id = site_indexes(sites)
    total_miles = Decimal("0.0")
    previous_location = path_locations[0]
    for location in path_locations[1:]:
        total_miles += owntracks_segment_miles(
            previous_location,
            location,
            sites,
            sites_by_name,
            sites_by_region_id,
        )
        previous_location = location

    return _quantize_distance(total_miles)


def _dashboard_distance_summary(db: Session, *, today: date, year: int, month: int) -> dict:
    """Return dashboard distance totals for today and the current month."""

    tomorrow = today + timedelta(days=1)
    today_start_dt, today_end_dt = local_day_bounds(today)
    month_start_date, month_end_date = _month_date_bounds(year, month)
    month_start_dt, month_end_dt = _month_datetime_bounds(year, month)
    today_components = _distance_components(
        _owntracks_total_miles_for_datetime_range(
            db,
            today_start_dt,
            today_end_dt,
        ),
        _trip_miles_for_date_range(db, today, tomorrow),
    )
    month_components = _distance_components(
        _owntracks_total_miles_for_datetime_range(
            db,
            month_start_dt,
            month_end_dt,
        ),
        _trip_miles_for_date_range(db, month_start_date, month_end_date),
    )
    return {
        "today_total": today_components["total"],
        "today_trips": today_components["trips"],
        "today_non_trips": today_components["non_trips"],
        "month_total": month_components["total"],
        "month_trips": month_components["trips"],
        "month_non_trips": month_components["non_trips"],
    }


def _dashboard_location_state(movement_state) -> dict[str, str]:
    """Return compact current-location state text for the Dashboard card."""

    if movement_state.state == "waypoint":
        return {
            "state": "waypoint",
            "label": "Inside waypoint",
            "detail": movement_state.site_name or "Saved waypoint",
        }
    if movement_state.state == "travel":
        detail = "Moving away from saved waypoints"
        if movement_state.distance_miles is not None:
            detail = f"Last movement {movement_state.distance_miles:.1f} miles"
        return {"state": "travel", "label": "Driving", "detail": detail}
    if movement_state.state == "away":
        return {
            "state": "stationary",
            "label": "Stationary",
            "detail": "Away from saved waypoints",
        }
    return {
        "state": "unknown",
        "label": "No OwnTracks data",
        "detail": "Waiting for phone location",
    }


def _pagination_context(total: int, page: int, page_size: int) -> dict[str, int | bool]:
    total_pages = max(1, ceil(total / page_size))
    current_page = min(max(page, 1), total_pages)
    return {
        "page": current_page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "first_item": ((current_page - 1) * page_size) + 1 if total else 0,
        "last_item": min(current_page * page_size, total),
        "has_previous": current_page > 1,
        "has_next": current_page < total_pages,
    }


def _monthly_gas_context(db: Session, year: int, month: int) -> tuple[MonthlyGasPrice | None, str]:
    try:
        return get_or_create_monthly_price(db, year, month), ""
    except GasPriceUnavailable as exc:
        return None, str(exc)
    except Exception as exc:
        return None, f"Could not load gas price: {exc}"


def _human_duration_since(value: datetime | None, *, now: datetime | None = None) -> str:
    if value is None:
        return "Never"
    current_dt = now or datetime.now(UTC)
    elapsed_seconds = int((datetime_to_utc(current_dt) - datetime_to_utc(value)).total_seconds())
    if elapsed_seconds <= 5:
        return "just now"

    units = (
        ("day", 86_400),
        ("hour", 3_600),
        ("minute", 60),
    )
    for label, unit_seconds in units:
        count = elapsed_seconds // unit_seconds
        if count >= 1:
            suffix = "" if count == 1 else "s"
            return f"{count} {label}{suffix} ago"
    return f"{elapsed_seconds} seconds ago"


def _api_test_result(
    status: str | None,
    message: str | None,
    value: str | None,
) -> ApiTestResult | None:
    if status not in {"pass", "fail"}:
        return None
    cleaned_message = (message or "").strip()[:300]
    cleaned_value = (value or "").strip()[:120]
    return ApiTestResult(status=status, message=cleaned_message, value=cleaned_value)


def _format_file_size(size_bytes: int) -> str:
    """Return a compact human-readable file size for backup listings."""

    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def _format_storage_size(size_bytes: int) -> str:
    """Return a compact human-readable size for filesystem capacity values."""

    units = (
        ("TB", 1024**4),
        ("GB", 1024**3),
        ("MB", 1024**2),
        ("KB", 1024),
    )
    for suffix, unit_size in units:
        if size_bytes >= unit_size:
            return f"{size_bytes / unit_size:.1f} {suffix}"
    return f"{size_bytes} B"


def _serialize_automatic_backup(
    backup_file: AutomaticBackupFile,
) -> DiagnosticAutomaticBackup:
    """Return display-safe metadata for one automatic backup file."""

    return DiagnosticAutomaticBackup(
        filename=backup_file.filename,
        created_at_display=_format_local_datetime(backup_file.created_at_utc),
        size_display=_format_file_size(backup_file.size_bytes),
        download_url=(
            "/diagnostics/automatic-backups/download?"
            f"filename={quote(backup_file.filename, safe='')}"
        ),
    )


def _sqlite_database_path(database_url: str) -> Path | None:
    """Return a local SQLite database path when the configured URL points to one."""

    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        return None
    if parsed_url.drivername not in {"sqlite", "sqlite+pysqlite"}:
        return None
    database_path = parsed_url.database
    if not database_path or database_path == ":memory:":
        return None
    return Path(database_path)


def _diagnostic_storage_paths(settings) -> tuple[str, ...]:
    """Return configured paths worth checking for Diagnostics disk usage."""

    path_candidates = [
        str(Path.cwd()),
        settings.log_dir,
        str(Path(settings.login_failure_log_path).expanduser().parent),
        settings.automatic_backup_dir,
    ]
    sqlite_path = _sqlite_database_path(settings.database_url)
    if sqlite_path is not None:
        path_candidates.append(str(sqlite_path))

    unique_paths: list[str] = []
    seen: set[str] = set()
    for path_text in path_candidates:
        cleaned_path = str(path_text).strip()
        if not cleaned_path or cleaned_path in seen:
            continue
        seen.add(cleaned_path)
        unique_paths.append(cleaned_path)
    return tuple(unique_paths)


def _existing_disk_usage_target(path: Path) -> Path | None:
    """Return the nearest existing path that can be passed to disk usage checks."""

    expanded_path = path.expanduser()
    candidate = expanded_path if expanded_path.is_absolute() else Path.cwd() / expanded_path
    if candidate.exists():
        return candidate
    for parent in candidate.parents:
        if parent.exists():
            return parent
    return None


def _diagnostic_disk_usages(
    paths: tuple[str, ...],
    *,
    disk_usage_func=shutil.disk_usage,
) -> list[DiagnosticDiskUsage]:
    """Group configured paths by exact free and total bytes for the Diagnostics page."""

    grouped_paths: dict[tuple[int, int], list[tuple[str, str, int]]] = {}
    for path_text in paths:
        target_path = _existing_disk_usage_target(Path(path_text))
        if target_path is None:
            continue
        try:
            usage = disk_usage_func(target_path)
        except OSError:
            logger.exception("Could not read disk usage for path=%s", path_text)
            continue
        key = (int(usage.free), int(usage.total))
        grouped_paths.setdefault(key, []).append(
            (path_text, str(target_path), int(usage.used))
        )

    disk_usages: list[DiagnosticDiskUsage] = []
    for (free_bytes, total_bytes), path_rows in grouped_paths.items():
        used_bytes = path_rows[0][2]
        used_percent = (used_bytes / total_bytes * 100) if total_bytes else 0
        disk_usages.append(
            DiagnosticDiskUsage(
                paths=tuple(row[0] for row in path_rows),
                inspected_path=path_rows[0][1],
                total_bytes=total_bytes,
                used_bytes=used_bytes,
                free_bytes=free_bytes,
                total_display=_format_storage_size(total_bytes),
                used_display=_format_storage_size(used_bytes),
                free_display=_format_storage_size(free_bytes),
                used_percent_display=f"{used_percent:.1f}%",
            )
        )

    return sorted(disk_usages, key=lambda item: item.primary_path)


def _update_trip_row_values(
    trip: Trip,
    *,
    origin_site: Site,
    destination_site: Site,
    miles: Decimal,
) -> set[tuple[int, int]]:
    """Apply only the editable Trips-page fields to one trip row."""

    manual_review_needed = _apply_trip_waypoints(trip, origin_site, destination_site)
    resequence_months: set[tuple[int, int]] = set()
    rounded_miles = Decimal(str(miles)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if trip.miles != rounded_miles:
        trip.miles = rounded_miles
        trip.mileage_source = MILEAGE_SOURCE_MANUAL
        resequence_months.add((trip.trip_date.year, trip.trip_date.month))
        manual_review_needed = True

    if manual_review_needed:
        mark_trip_manually_reviewed(trip)
    return resequence_months


def _diagnostics_redirect(fragment: str, params: dict[str, str]) -> RedirectResponse:
    query = urlencode(params)
    return RedirectResponse(url=f"/diagnostics?{query}#{fragment}", status_code=303)


def _require_backup_restore_auth(request: Request) -> None:
    """Require a logged-in web session before exporting or restoring sensitive app data."""

    settings = get_settings()
    if web_login_enabled(settings) and request_is_authenticated(request):
        return
    raise HTTPException(
        status_code=403,
        detail="Full backup and restore require WEB_LOGIN_USERNAME and WEB_LOGIN_PASSWORD login.",
    )


def _log_line_is_visible(line: str, min_level: int) -> bool:
    match = LOG_LINE_LEVEL_RE.search(line)
    if match is None:
        return False
    return LOG_LINE_LEVEL_VALUES[match.group(1)] >= min_level


def _log_line_entry(line: str) -> LogLine:
    match = LOG_LINE_LEVEL_RE.search(line)
    level = match.group(1).lower() if match else "debug"
    css_level = "error" if level in {"error", "critical"} else level
    return LogLine(
        text=line,
        level=level,
        css_class=f"log-line-{css_level}",
    )


def _tail_file(path: Path, max_lines: int = 200, log_level: str = "info") -> list[LogLine]:
    if not path.exists():
        return []
    with path.open("rb") as file:
        file.seek(0, 2)
        size = file.tell()
        file.seek(max(size - 80_000, 0))
        text = file.read().decode("utf-8", errors="replace")
    min_level = LOG_LEVEL_VALUES[log_level]
    visible_lines = [
        redact_sensitive_text(line)
        for line in text.splitlines()
        if _log_line_is_visible(line, min_level)
    ]
    return [_log_line_entry(line) for line in reversed(visible_lines[-max_lines:])]


def _waypoint_ordering():
    return (
        Site.last_visited_at.desc().nulls_last(),
        Site.created_at.desc(),
        Site.name.asc(),
    )


def _waypoints_for_trip_forms(db: Session) -> list[Site]:
    """Return waypoints in the same stable order used by the Waypoints page."""

    return list(db.scalars(select(Site).order_by(*_waypoint_ordering())))


def _load_trip_form_waypoint(db: Session, waypoint_id: int) -> Site:
    """Load a submitted waypoint ID or reject the trip form as invalid."""

    waypoint = db.get(Site, waypoint_id)
    if waypoint is None:
        raise HTTPException(status_code=400, detail="Selected waypoint does not exist")
    return waypoint


def _apply_trip_waypoints(trip: Trip, origin_site: Site, destination_site: Site) -> bool:
    """Apply selected waypoint metadata to a trip and report whether it changed."""

    changed = (
        trip.origin_site_id != origin_site.id
        or trip.destination_site_id != destination_site.id
        or trip.origin_name != origin_site.name
        or trip.destination_name != destination_site.name
        or trip.start_latitude != origin_site.latitude
        or trip.start_longitude != origin_site.longitude
        or trip.end_latitude != destination_site.latitude
        or trip.end_longitude != destination_site.longitude
    )
    if not changed:
        return False

    trip.origin_site = origin_site
    trip.destination_site = destination_site
    trip.origin_site_id = origin_site.id
    trip.destination_site_id = destination_site.id
    trip.origin_name = origin_site.name
    trip.destination_name = destination_site.name
    trip.start_latitude = origin_site.latitude
    trip.start_longitude = origin_site.longitude
    trip.end_latitude = destination_site.latitude
    trip.end_longitude = destination_site.longitude
    trip.mileage_source = MILEAGE_SOURCE_MANUAL
    mark_trip_manually_reviewed(trip)
    return True


def _latest_odometer_reading(db: Session) -> dict | None:
    candidates = []
    options = (joinedload(Trip.origin_site), joinedload(Trip.destination_site))

    latest_end_trip = db.scalar(
        select(Trip)
        .options(*options)
        .where(Trip.end_odometer_miles.is_not(None))
        .order_by(Trip.ended_at.desc(), Trip.id.desc())
        .limit(1)
    )
    if latest_end_trip is not None:
        candidates.append(
            {
                "value": latest_end_trip.end_odometer_miles,
                "source": latest_end_trip.end_odometer_source or latest_end_trip.mileage_source,
                "recorded_at": latest_end_trip.ended_at,
                "trip": latest_end_trip,
                "database_id": latest_end_trip.id,
                "position": "End",
            }
        )

    latest_start_trip = db.scalar(
        select(Trip)
        .options(*options)
        .where(Trip.start_odometer_miles.is_not(None))
        .order_by(Trip.started_at.desc(), Trip.id.desc())
        .limit(1)
    )
    if latest_start_trip is not None:
        candidates.append(
            {
                "value": latest_start_trip.start_odometer_miles,
                "source": latest_start_trip.start_odometer_source
                or latest_start_trip.mileage_source,
                "recorded_at": latest_start_trip.started_at,
                "trip": latest_start_trip,
                "database_id": latest_start_trip.id,
                "position": "Start",
            }
        )

    checkpoint = db.scalar(
        select(TripProcessingCheckpoint)
        .where(TripProcessingCheckpoint.name == AUTOMATIC_TRIP_PROCESSING_CHECKPOINT)
        .where(TripProcessingCheckpoint.odometer_anchor_miles.is_not(None))
        .where(TripProcessingCheckpoint.odometer_anchor_recorded_at.is_not(None))
        .limit(1)
    )
    if checkpoint is not None:
        candidates.append(
            {
                "value": checkpoint.odometer_anchor_miles,
                "source": "owntracks_estimate",
                "recorded_at": checkpoint.odometer_anchor_recorded_at,
                "trip": None,
                "database_id": checkpoint.id,
                "position": "Rolling",
            }
        )

    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (datetime_to_utc(item["recorded_at"]), item["database_id"]),
    )


def _masked_database_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.password:
        return url
    username = parts.username or ""
    hostname = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{username}:***@{hostname}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next: str = Query(default="/"),
) -> Response:
    settings = get_settings()
    safe_next = valid_next_path(next)
    if not web_login_enabled(settings):
        return RedirectResponse(url=safe_next, status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "next_url": safe_next,
            "login_error": "",
        },
    )


@router.post("/login")
def login_form(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_url: str = Form(default="/"),
) -> Response:
    settings = get_settings()
    safe_next = valid_next_path(next_url)
    if not web_login_enabled(settings):
        return RedirectResponse(url=safe_next, status_code=303)
    if login_is_locked(request):
        attempt_state = login_failure_state(request)
        lockout_remaining_seconds = login_lockout_remaining_seconds(attempt_state)
        record_web_login_failure(
            request=request,
            username=username,
            password=password,
            reason="locked_out",
            failed_count=attempt_state.failed_count if attempt_state else 0,
            max_attempts=settings.web_login_max_attempts,
            lockout_applied=True,
            lockout_remaining_seconds=lockout_remaining_seconds,
            next_url=safe_next,
            settings=settings,
        )
        logger.warning("Web login rejected reason=locked_out")
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "next_url": safe_next,
                "login_error": "Login is temporarily unavailable.",
            },
            status_code=429,
        )
    if authenticate_web_credentials(username, password, settings):
        clear_login_failures(request)
        mark_request_authenticated(request)
        logger.info("Web login succeeded")
        return RedirectResponse(url=safe_next, status_code=303)

    attempt_state = record_login_failure(request, settings)
    lockout_remaining_seconds = login_lockout_remaining_seconds(attempt_state)
    record_web_login_failure(
        request=request,
        username=username,
        password=password,
        reason="invalid_credentials",
        failed_count=attempt_state.failed_count,
        max_attempts=settings.web_login_max_attempts,
        lockout_applied=lockout_remaining_seconds > 0,
        lockout_remaining_seconds=lockout_remaining_seconds,
        next_url=safe_next,
        settings=settings,
    )
    logger.warning("Web login failed")
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "next_url": safe_next,
            "login_error": "Invalid username or password.",
        },
        status_code=401,
    )


@router.post("/logout")
def logout_form(request: Request) -> RedirectResponse:
    clear_request_authentication(request)
    return RedirectResponse(url="/login", status_code=303)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    settings = get_settings()
    app_now = local_now()
    app_today = app_now.date()
    year, month = _year_month_for_local_date(app_today)
    monthly_gas, _ = _monthly_gas_context(db, year, month)
    distance_summary = _dashboard_distance_summary(
        db,
        today=app_today,
        year=year,
        month=month,
    )
    location_count = db.scalar(select(func.count(OwnTracksLocation.id))) or 0
    site_count = db.scalar(select(func.count(Site.id))) or 0
    trip_count = db.scalar(select(func.count(Trip.id))) or 0
    latest_odometer = _latest_odometer_reading(db)
    movement_diagnostics = owntracks_movement_diagnostics(db)
    location_state = _dashboard_location_state(movement_diagnostics.current_state)
    recent_trips = list(
        db.scalars(
            select(Trip)
            .options(joinedload(Trip.origin_site), joinedload(Trip.destination_site))
            .order_by(Trip.trip_date.desc(), Trip.started_at.desc())
            .limit(8)
        )
    )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "year": year,
            "month": month,
            "location_count": location_count,
            "site_count": site_count,
            "trip_count": trip_count,
            "distance_summary": distance_summary,
            "location_state": location_state,
            "latest_odometer": latest_odometer,
            "recent_trips": recent_trips,
            "monthly_gas": monthly_gas,
            "app_local_datetime": app_now,
            "app_timezone": settings.local_timezone,
            "app_timezone_abbr": app_now.tzname(),
        },
    )


@router.get("/trips", response_class=HTMLResponse)
def trips(
    request: Request,
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if year is None or month is None:
        year, month = _current_year_month()
    _validate_month(month)
    previous_year, previous_month = _shift_month(year, month, -1)
    next_year, next_month = _shift_month(year, month, 1)
    start = date(year, month, 1)
    end = date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    stmt = (
        select(Trip)
        .options(joinedload(Trip.origin_site), joinedload(Trip.destination_site))
        .where(Trip.trip_date >= start)
        .where(Trip.trip_date < end)
        .order_by(Trip.trip_date.asc(), Trip.started_at.asc(), Trip.id.asc())
    )
    all_trips = list(db.scalars(stmt))
    waypoints = _waypoints_for_trip_forms(db)
    suppressed_trips = list(
        db.scalars(
            select(DeletedTrip)
            .where(DeletedTrip.trip_date >= start)
            .where(DeletedTrip.trip_date < end)
            .order_by(DeletedTrip.trip_date.asc(), DeletedTrip.started_at.asc())
        )
    )
    return templates.TemplateResponse(
        request,
        "trips.html",
        {
            "trips": all_trips,
            "year": year,
            "month": month,
            "today": local_today(),
            "waypoints": waypoints,
            "waypoint_names": [waypoint.name for waypoint in waypoints],
            "month_options": [(value, month_name[value]) for value in range(1, 13)],
            "previous_year": previous_year,
            "previous_month": previous_month,
            "next_year": next_year,
            "next_month": next_month,
            "suppressed_trips": suppressed_trips,
        },
    )


@router.post("/trips/{trip_id}")
def update_trip_form(
    trip_id: int,
    origin_site_id: int = Form(...),
    destination_site_id: int = Form(...),
    miles: Decimal = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    if miles < 0:
        raise HTTPException(status_code=400, detail="Miles must be zero or greater")
    origin_site = _load_trip_form_waypoint(db, origin_site_id)
    destination_site = _load_trip_form_waypoint(db, destination_site_id)

    resequence_months = _update_trip_row_values(
        trip,
        origin_site=origin_site,
        destination_site=destination_site,
        miles=miles,
    )
    for resequence_year, resequence_month in sorted(resequence_months):
        resequence_month_trip_odometers(db, resequence_year, resequence_month)
    db.commit()
    logger.info(
        "Updated trip via web form trip_id=%s date=%s origin=%s destination=%s miles=%s "
        "resequence_months=%s",
        trip.id,
        trip.trip_date.isoformat(),
        trip.origin_display_name,
        trip.destination_display_name,
        trip.miles,
        sorted(resequence_months),
    )
    return RedirectResponse(
        url=f"/trips?year={trip.trip_date.year}&month={trip.trip_date.month}",
        status_code=303,
    )


@router.post("/trips")
def create_trip_form(
    trip_date: date = Form(...),
    origin_site_id: int = Form(...),
    destination_site_id: int = Form(...),
    miles: Decimal = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if miles < 0:
        raise HTTPException(status_code=400, detail="Miles must be zero or greater")
    origin_site = _load_trip_form_waypoint(db, origin_site_id)
    destination_site = _load_trip_form_waypoint(db, destination_site_id)
    trip = create_manual_trip(
        db,
        trip_date=trip_date,
        origin_name=origin_site.name,
        destination_name=destination_site.name,
        miles=miles,
    )
    _apply_trip_waypoints(trip, origin_site, destination_site)
    db.commit()
    logger.info(
        "Created manual trip via web form trip_id=%s date=%s origin=%s destination=%s miles=%s",
        trip.id,
        trip.trip_date.isoformat(),
        trip.origin_display_name,
        trip.destination_display_name,
        trip.miles,
    )
    return RedirectResponse(
        url=f"/trips?year={trip.trip_date.year}&month={trip.trip_date.month}",
        status_code=303,
    )


@router.post("/trips/{trip_id}/delete")
def delete_trip_form(trip_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")

    redirect_year = trip.trip_date.year
    redirect_month = trip.trip_date.month
    deleted_trip = delete_trip(db, trip)
    db.commit()
    logger.info(
        "Deleted trip via web form trip_id=%s suppressed=%s origin=%s destination=%s "
        "started_at=%s ended_at=%s",
        trip_id,
        deleted_trip is not None,
        deleted_trip.origin_name if deleted_trip is not None else "",
        deleted_trip.destination_name if deleted_trip is not None else "",
        deleted_trip.started_at.isoformat() if deleted_trip is not None else "",
        deleted_trip.ended_at.isoformat() if deleted_trip is not None else "",
    )
    return RedirectResponse(
        url=f"/trips?year={redirect_year}&month={redirect_month}",
        status_code=303,
    )


@router.post("/trips/suppression/{deleted_trip_id}/delete")
def delete_trip_suppression_form(
    deleted_trip_id: int,
    redirect_year: int = Form(...),
    redirect_month: int = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    _validate_month(redirect_month)
    deleted_trip = db.get(DeletedTrip, deleted_trip_id)
    if deleted_trip is None:
        raise HTTPException(status_code=404, detail="Deleted trip record not found")

    logger.info(
        "Removed deleted trip record deleted_trip_id=%s origin=%s destination=%s "
        "started_at=%s ended_at=%s",
        deleted_trip.id,
        deleted_trip.origin_name or "",
        deleted_trip.destination_name or "",
        deleted_trip.started_at.isoformat(),
        deleted_trip.ended_at.isoformat(),
    )
    db.delete(deleted_trip)
    db.commit()
    return RedirectResponse(
        url=f"/trips?year={redirect_year}&month={redirect_month}",
        status_code=303,
    )


@router.get("/sites")
def sites_redirect() -> RedirectResponse:
    return RedirectResponse(url="/waypoints", status_code=308)


def _detach_waypoint_references(db: Session, waypoint: Site) -> tuple[int, int]:
    """Remove foreign-key references before deleting a waypoint while keeping audit text."""

    trip_updates = 0
    trips = list(
        db.scalars(
            select(Trip).where(
                (Trip.origin_site_id == waypoint.id)
                | (Trip.destination_site_id == waypoint.id)
            )
        )
    )
    for trip in trips:
        if trip.origin_site_id == waypoint.id:
            trip.origin_name = trip.origin_name or waypoint.name
            trip.origin_site_id = None
            trip_updates += 1
        if trip.destination_site_id == waypoint.id:
            trip.destination_name = trip.destination_name or waypoint.name
            trip.destination_site_id = None
            trip_updates += 1

    deleted_trip_updates = 0
    deleted_trips = list(
        db.scalars(
            select(DeletedTrip).where(
                (DeletedTrip.origin_site_id == waypoint.id)
                | (DeletedTrip.destination_site_id == waypoint.id)
            )
        )
    )
    for deleted_trip in deleted_trips:
        if deleted_trip.origin_site_id == waypoint.id:
            deleted_trip.origin_name = deleted_trip.origin_name or waypoint.name
            deleted_trip.origin_site_id = None
            deleted_trip_updates += 1
        if deleted_trip.destination_site_id == waypoint.id:
            deleted_trip.destination_name = deleted_trip.destination_name or waypoint.name
            deleted_trip.destination_site_id = None
            deleted_trip_updates += 1

    return trip_updates, deleted_trip_updates


@router.post("/waypoints/{waypoint_id}/delete")
def delete_waypoint_form(
    waypoint_id: int,
    page: int = Form(default=1),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    waypoint = db.get(Site, waypoint_id)
    if waypoint is None:
        raise HTTPException(status_code=404, detail="Waypoint not found")

    redirect_page = max(page, 1)
    trip_updates, deleted_trip_updates = _detach_waypoint_references(db, waypoint)
    logger.info(
        "Deleted waypoint id=%s name=%s trip_references_detached=%s "
        "deleted_trip_references_detached=%s",
        waypoint.id,
        waypoint.name,
        trip_updates,
        deleted_trip_updates,
    )
    db.delete(waypoint)
    db.commit()
    return RedirectResponse(url=f"/waypoints?page={redirect_page}", status_code=303)


@router.get("/waypoints", response_class=HTMLResponse)
def waypoints(
    request: Request,
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    waypoint_count = db.scalar(select(func.count(Site.id))) or 0
    pagination = _pagination_context(waypoint_count, page, WAYPOINT_PAGE_SIZE)
    all_waypoints = list(
        db.scalars(
            select(Site)
            .order_by(*_waypoint_ordering())
            .offset((pagination["page"] - 1) * pagination["page_size"])
            .limit(pagination["page_size"])
        )
    )
    return templates.TemplateResponse(
        request,
        "waypoints.html",
        {
            "waypoints": all_waypoints,
            "waypoint_pagination": pagination,
        },
    )


@router.get("/waypoints/export")
def export_waypoints(db: Session = Depends(get_db)) -> Response:
    all_waypoints = list(db.scalars(select(Site).order_by(*_waypoint_ordering())))
    return Response(
        content=owntracks_waypoints_json(all_waypoints),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="owntracks-waypoints.json"'},
    )


@router.post("/gas-prices/refresh")
def refresh_gas_price_form(
    next_url: str = Form(default="/"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        refresh_current_monthly_price(db)
    except GasPriceUnavailable as exc:
        logger.warning("Gas price refresh unavailable from web form: %s", exc)
        pass
    else:
        logger.info("Refreshed current monthly gas price from web form")
    return RedirectResponse(url=next_url if next_url.startswith("/") else "/", status_code=303)


@router.get("/diagnostics", response_class=HTMLResponse)
def diagnostics(
    request: Request,
    owntracks_page: int = Query(default=1, ge=1),
    odometer_test: str | None = Query(default=None),
    odometer_message: str | None = Query(default=None),
    odometer_value: str | None = Query(default=None),
    eia_test: str | None = Query(default=None),
    eia_message: str | None = Query(default=None),
    eia_value: str | None = Query(default=None),
    restore_test: str | None = Query(default=None),
    restore_message: str | None = Query(default=None),
    restore_value: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    settings = get_settings()
    log_dir = Path(settings.log_dir)
    login_failure_log_path = Path(settings.login_failure_log_path)
    latest_location = db.scalar(
        select(OwnTracksLocation).order_by(OwnTracksLocation.captured_at.desc()).limit(1)
    )
    latest_received_location = db.scalar(
        select(OwnTracksLocation).order_by(OwnTracksLocation.received_at.desc()).limit(1)
    )
    latest_snapshot = db.scalar(
        select(GasPriceSnapshot).order_by(GasPriceSnapshot.observed_on.desc()).limit(1)
    )
    latest_monthly_gas = db.scalar(
        select(MonthlyGasPrice)
        .order_by(MonthlyGasPrice.year.desc(), MonthlyGasPrice.month.desc())
        .limit(1)
    )
    latest_odometer = _latest_odometer_reading(db)
    owntracks_entries_page = paginated_owntracks_entries(db, page=owntracks_page)
    movement_diagnostics = owntracks_movement_diagnostics(db)
    backup_restore_enabled = web_login_enabled(settings)
    disk_usages = _diagnostic_disk_usages(_diagnostic_storage_paths(settings))
    automatic_backups = (
        [
            _serialize_automatic_backup(backup_file)
            for backup_file in list_automatic_backup_files(settings.automatic_backup_dir)
        ]
        if backup_restore_enabled and request_is_authenticated(request)
        else []
    )
    return templates.TemplateResponse(
        request,
        "diagnostics.html",
        {
            "settings": settings,
            "database_url": _masked_database_url(settings.database_url),
            "location_count": owntracks_entries_page.total,
            "site_count": db.scalar(select(func.count(Site.id))) or 0,
            "trip_count": db.scalar(select(func.count(Trip.id))) or 0,
            "gas_snapshot_count": db.scalar(select(func.count(GasPriceSnapshot.id))) or 0,
            "latest_location": latest_location,
            "last_owntracks_received_at": (
                latest_received_location.received_at if latest_received_location else None
            ),
            "last_owntracks_received_age": _human_duration_since(
                latest_received_location.received_at if latest_received_location else None
            ),
            "latest_snapshot": latest_snapshot,
            "latest_monthly_gas": latest_monthly_gas,
            "latest_odometer": latest_odometer,
            "disk_usages": disk_usages,
            "recent_locations": owntracks_entries_page.entries,
            "owntracks_entries_page": owntracks_entries_page,
            "movement_state": movement_diagnostics.current_state,
            "movement_state_changes": movement_diagnostics.state_changes,
            "app_log_lines": _tail_file(log_dir / "app.log", log_level=settings.log_level),
            "login_failure_log_path": login_failure_log_path,
            "login_failure_entries": tail_login_failure_entries(login_failure_log_path),
            "manual_odometer_result": _api_test_result(
                odometer_test,
                odometer_message,
                odometer_value,
            ),
            "eia_test_result": _api_test_result(eia_test, eia_message, eia_value),
            "restore_result": _api_test_result(
                restore_test,
                restore_message,
                restore_value,
            ),
            "backup_restore_enabled": backup_restore_enabled,
            "automatic_backups_enabled": settings.automatic_backups_enabled,
            "automatic_backup_dir": settings.automatic_backup_dir,
            "automatic_backups": automatic_backups,
            "backup_upload_max_mb": settings.max_backup_restore_bytes // (1024 * 1024),
        },
    )


@router.get("/diagnostics/backup")
def download_full_backup(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    _require_backup_restore_auth(request)
    backup = create_full_backup(db)
    logger.warning(
        "Created full database backup filename=%s total_rows=%s",
        backup.filename,
        backup.total_rows,
    )
    return Response(
        content=backup.content,
        media_type=BACKUP_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{backup.filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/diagnostics/restore")
async def restore_full_backup_form(
    request: Request,
    backup_file: UploadFile = File(...),
    confirmation: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    _require_backup_restore_auth(request)
    settings = get_settings()
    if confirmation.strip() != "RESTORE":
        return _diagnostics_redirect(
            "data-backup",
            {
                "restore_test": "fail",
                "restore_message": "Type RESTORE to confirm full database restore.",
            },
        )

    content = await backup_file.read(settings.max_backup_restore_bytes + 1)
    await backup_file.close()
    if len(content) > settings.max_backup_restore_bytes:
        max_upload_mb = settings.max_backup_restore_bytes // (1024 * 1024)
        return _diagnostics_redirect(
            "data-backup",
            {
                "restore_test": "fail",
                "restore_message": f"Backup file is larger than {max_upload_mb} MB.",
            },
        )

    try:
        summary = restore_full_backup(db, content)
    except BackupValidationError as exc:
        logger.warning("Rejected full database restore upload: %s", exc)
        return _diagnostics_redirect(
            "data-backup",
            {
                "restore_test": "fail",
                "restore_message": str(exc),
            },
        )
    except Exception:
        logger.exception("Full database restore failed unexpectedly")
        return _diagnostics_redirect(
            "data-backup",
            {
                "restore_test": "fail",
                "restore_message": "Restore failed. Check the app log before trying again.",
            },
        )

    return _diagnostics_redirect(
        "data-backup",
        {
            "restore_test": "pass",
            "restore_message": "Full database restore completed.",
            "restore_value": (
                f"{summary.total_rows} rows restored across "
                f"{len(summary.table_counts)} tables."
            ),
        },
    )


@router.get("/diagnostics/automatic-backups/download")
def download_automatic_backup(
    request: Request,
    filename: str = Query(...),
) -> Response:
    """Download one retained automatic backup after the same checks used for restore."""

    _require_backup_restore_auth(request)
    settings = get_settings()
    backup_filename = filename.strip()
    try:
        content = read_automatic_backup_content(
            settings.automatic_backup_dir,
            backup_filename,
            max_bytes=settings.max_backup_restore_bytes,
        )
    except BackupValidationError as exc:
        logger.warning(
            "Rejected automatic backup download filename=%s error=%s",
            backup_filename,
            exc,
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    logger.warning(
        "Downloaded automatic Mileage Logger backup filename=%s size_bytes=%s",
        backup_filename,
        len(content),
    )
    return Response(
        content=content,
        media_type=BACKUP_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{backup_filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/diagnostics/automatic-backups/restore")
def restore_automatic_backup_form(
    request: Request,
    filename: str = Form(...),
    confirmation: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Restore a retained automatic backup selected from the Diagnostics page."""

    _require_backup_restore_auth(request)
    settings = get_settings()
    backup_filename = filename.strip()
    if confirmation.strip() != "RESTORE":
        return _diagnostics_redirect(
            "automatic-backups",
            {
                "restore_test": "fail",
                "restore_message": "Type RESTORE to confirm automatic backup restore.",
            },
        )

    try:
        content = read_automatic_backup_content(
            settings.automatic_backup_dir,
            backup_filename,
            max_bytes=settings.max_backup_restore_bytes,
        )
        summary = restore_full_backup(db, content)
    except BackupValidationError as exc:
        logger.warning(
            "Rejected automatic backup restore filename=%s error=%s",
            backup_filename,
            exc,
        )
        return _diagnostics_redirect(
            "automatic-backups",
            {
                "restore_test": "fail",
                "restore_message": str(exc),
            },
        )
    except Exception:
        logger.exception(
            "Automatic Mileage Logger restore failed unexpectedly filename=%s",
            backup_filename,
        )
        return _diagnostics_redirect(
            "automatic-backups",
            {
                "restore_test": "fail",
                "restore_message": "Restore failed. Check the app log before trying again.",
            },
        )

    return _diagnostics_redirect(
        "automatic-backups",
        {
            "restore_test": "pass",
            "restore_message": "Automatic backup restore completed.",
            "restore_value": (
                f"{summary.total_rows} rows restored across "
                f"{len(summary.table_counts)} tables."
            ),
        },
    )


@router.get("/diagnostics/logs/login-failures")
def download_login_failure_log() -> Response:
    log_path = Path(get_settings().login_failure_log_path)
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="Login failure log not found")
    return Response(
        content=redact_sensitive_text(log_path.read_text(encoding="utf-8", errors="replace")),
        media_type="application/jsonl; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="mileage-logger-login-failures.log"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/diagnostics/odometer")
def set_manual_odometer(
    odometer_miles: Decimal = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if odometer_miles <= 0:
        return _diagnostics_redirect(
            "api-tests",
            {
                "odometer_test": "fail",
                "odometer_message": "Odometer reading must be greater than zero.",
            },
        )

    checkpoint = update_odometer_anchor_from_reading(
        db,
        odometer_miles,
        recorded_at=datetime.now(UTC),
        source="manual",
    )
    return _diagnostics_redirect(
        "api-tests",
        {
            "odometer_test": "pass",
            "odometer_message": "Manual odometer reading saved.",
            "odometer_value": f"{checkpoint.odometer_anchor_miles:.1f} miles",
        },
    )


@router.post("/diagnostics/test/eia")
def test_eia_api() -> RedirectResponse:
    settings = get_settings()
    try:
        reading = EiaSeriesProvider().current_regular_price(settings.gas_price_state)
    except GasPriceUnavailable as exc:
        return _diagnostics_redirect(
            "api-tests",
            {
                "eia_test": "fail",
                "eia_message": str(exc),
            },
        )
    except Exception:
        logger.warning("EIA diagnostic test failed with an unexpected provider error")
        return _diagnostics_redirect(
            "api-tests",
            {
                "eia_test": "fail",
                "eia_message": "EIA test failed. Check the API key, series ID, and app log.",
            },
        )

    return _diagnostics_redirect(
        "api-tests",
        {
            "eia_test": "pass",
            "eia_message": "EIA returned a current regular gas price reading.",
            "eia_value": f"${reading.price_per_gallon:.3f} on {reading.observed_on}",
        },
    )


@router.get("/diagnostics/logs/app")
def download_app_log() -> Response:
    log_path = Path(get_settings().log_dir) / "app.log"
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="App log not found")
    return Response(
        content=redact_sensitive_text(log_path.read_text(encoding="utf-8", errors="replace")),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="app.log"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/reports/{year}/{month}")
def report_form(year: int, month: int, db: Session = Depends(get_db)) -> Response:
    _validate_month(month)
    try:
        report = generate_monthly_pdf(db, year, month)
    except GasPriceUnavailable as exc:
        logger.warning("Report generation unavailable year=%s month=%s error=%s", year, month, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info(
        "Generated report from web form year=%s month=%s filename=%s",
        year,
        month,
        report.filename,
    )
    return Response(
        content=report.content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{report.filename}"'},
    )
