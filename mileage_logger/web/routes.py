import logging
import re
from calendar import month_name
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from math import ceil
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from mileage_logger.config import get_settings
from mileage_logger.database import get_db
from mileage_logger.models import (
    GasPriceSnapshot,
    MonthlyGasPrice,
    OwnTracksLocation,
    Site,
    Trip,
)
from mileage_logger.services.diagnostics import paginated_owntracks_entries
from mileage_logger.services.gas_prices import (
    GasPriceUnavailable,
    get_or_create_monthly_price,
    refresh_current_monthly_price,
)
from mileage_logger.services.mileage import update_trip_details
from mileage_logger.services.pdf import generate_monthly_pdf
from mileage_logger.services.timezone import (
    datetime_to_local,
    datetime_to_utc,
    local_now,
    local_today,
)
from mileage_logger.services.waypoints import owntracks_waypoints_json

router = APIRouter()
logger = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent
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
        "fordpass": "FordPass",
        "estimated": "Estimated",
        "previous_trip": "Previous trip",
        "manual": "Manual",
        "fordpass_odometer": "FordPass",
        "estimated_odometer": "Estimated",
        "waypoint_distance": "Waypoint distance",
    }
    if value is None:
        return "-"
    return labels.get(str(value), str(value).replace("_", " ").title())


templates.env.filters["local_datetime"] = _format_local_datetime
templates.env.filters["odometer"] = _format_odometer
templates.env.filters["odometer_source"] = _format_odometer_source
WAYPOINT_PAGE_SIZE = 20
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


@dataclass(frozen=True)
class LogLine:
    text: str
    level: str
    css_class: str


def _current_year_month() -> tuple[int, int]:
    today = local_today()
    return today.year, today.month


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    month_index = (year * 12) + month - 1 + offset
    return month_index // 12, (month_index % 12) + 1


def _validate_month(month: int) -> None:
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="month must be 1 through 12")


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
        line for line in text.splitlines() if _log_line_is_visible(line, min_level)
    ]
    return [_log_line_entry(line) for line in reversed(visible_lines[-max_lines:])]


def _waypoint_ordering():
    return (
        Site.last_visited_at.desc().nulls_last(),
        Site.created_at.desc(),
        Site.name.asc(),
    )


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
                "position": "Start",
            }
        )

    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (datetime_to_utc(item["recorded_at"]), item["trip"].id),
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


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    settings = get_settings()
    app_now = local_now()
    year, month = _current_year_month()
    monthly_gas, _ = _monthly_gas_context(db, year, month)
    location_count = db.scalar(select(func.count(OwnTracksLocation.id))) or 0
    site_count = db.scalar(select(func.count(Site.id))) or 0
    trip_count = db.scalar(select(func.count(Trip.id))) or 0
    latest_odometer = _latest_odometer_reading(db)
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
            "latest_odometer": latest_odometer,
            "recent_trips": recent_trips,
            "monthly_gas": monthly_gas,
            "vehicle_mpg": settings.vehicle_mpg,
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
        .order_by(Trip.trip_date.desc(), Trip.started_at.desc())
    )
    all_trips = list(db.scalars(stmt))
    return templates.TemplateResponse(
        request,
        "trips.html",
        {
            "trips": all_trips,
            "year": year,
            "month": month,
            "month_options": [(value, month_name[value]) for value in range(1, 13)],
            "previous_year": previous_year,
            "previous_month": previous_month,
            "next_year": next_year,
            "next_month": next_month,
        },
    )


@router.post("/trips/{trip_id}")
def update_trip_form(
    trip_id: int,
    origin_name: str = Form(...),
    destination_name: str = Form(...),
    miles: Decimal = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    update_trip_details(trip, origin_name, destination_name, miles)
    db.commit()
    logger.info(
        "Updated trip via web form trip_id=%s origin=%s destination=%s miles=%s",
        trip.id,
        trip.origin_display_name,
        trip.destination_display_name,
        trip.miles,
    )
    return RedirectResponse(
        url=f"/trips?year={trip.trip_date.year}&month={trip.trip_date.month}",
        status_code=303,
    )


@router.get("/sites")
def sites_redirect() -> RedirectResponse:
    return RedirectResponse(url="/waypoints", status_code=308)


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
    db: Session = Depends(get_db),
) -> HTMLResponse:
    settings = get_settings()
    log_dir = Path(settings.log_dir)
    latest_location = db.scalar(
        select(OwnTracksLocation).order_by(OwnTracksLocation.captured_at.desc()).limit(1)
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
            "latest_snapshot": latest_snapshot,
            "latest_monthly_gas": latest_monthly_gas,
            "latest_odometer": latest_odometer,
            "recent_locations": owntracks_entries_page.entries,
            "owntracks_entries_page": owntracks_entries_page,
            "app_log_lines": _tail_file(log_dir / "app.log", log_level=settings.log_level),
        },
    )


@router.get("/diagnostics/logs/app")
def download_app_log() -> FileResponse:
    log_path = Path(get_settings().log_dir) / "app.log"
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="App log not found")
    return FileResponse(
        log_path,
        media_type="text/plain",
        filename="app.log",
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
