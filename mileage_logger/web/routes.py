import logging
import re
from calendar import month_name
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from math import ceil
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from mileage_logger.config import get_settings
from mileage_logger.database import get_db
from mileage_logger.logging_config import redact_sensitive_text
from mileage_logger.models import (
    DeletedTrip,
    GasPriceSnapshot,
    MonthlyGasPrice,
    OwnTracksLocation,
    Site,
    SmartcarWebhookEvent,
    Trip,
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
from mileage_logger.services.mileage import create_manual_trip, delete_trip, update_trip_details
from mileage_logger.services.pdf import generate_monthly_pdf
from mileage_logger.services.smartcar import (
    MANUAL_ODOMETER_EVENT_TYPE,
    create_manual_odometer_event,
    latest_webhook_odometer_event,
    odometer_event_source,
)
from mileage_logger.services.timezone import (
    datetime_to_local,
    datetime_to_utc,
    local_now,
    local_today,
)
from mileage_logger.services.waypoints import owntracks_waypoints_json
from mileage_logger.web.auth import (
    authenticate_web_credentials,
    clear_login_failures,
    clear_request_authentication,
    login_is_locked,
    mark_request_authenticated,
    record_login_failure,
    valid_next_path,
    web_login_enabled,
)

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
        "smartcar": "Smartcar",
        "fordpass": "FordPass",
        "estimated": "Estimated",
        "previous_trip": "Previous trip",
        "manual": "Manual",
        "manual_odometer": "Manual odometer",
        "owntracks_path": "OwnTracks path",
        "smartcar_odometer": "Smartcar",
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
templates.env.globals["web_login_enabled"] = web_login_enabled
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


@dataclass(frozen=True)
class ApiTestResult:
    status: str
    message: str
    value: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "pass"


@dataclass(frozen=True)
class SmartcarWebhookDiagnostics:
    """Latest non-manual Smartcar webhook delivery details for Diagnostics."""

    event: SmartcarWebhookEvent | None
    age: str
    data_rows: list[tuple[str, str]]
    signal_rows: list[tuple[str, str, str, str]]


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


def _display_text(value: object | None) -> str:
    """Return a compact display value for optional diagnostic fields."""

    if value is None:
        return "-"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, Decimal):
        return f"{value.normalize():f}"
    return str(value)


def _masked_vin(vin: str | None) -> str:
    """Display only the VIN suffix because full VINs are sensitive diagnostics data."""

    cleaned_vin = (vin or "").strip()
    if not cleaned_vin:
        return "-"
    return f"Ending {cleaned_vin[-4:]}"


def _vehicle_description(event: SmartcarWebhookEvent) -> str:
    """Build a human-readable vehicle label from the latest Smartcar webhook."""

    parts = [
        str(event.vehicle_year) if event.vehicle_year is not None else "",
        event.vehicle_make or "",
        event.vehicle_model or "",
    ]
    description = " ".join(part for part in parts if part).strip()
    return description or "-"


def _miles_text(value: Decimal | None) -> str:
    """Format a mileage value for webhook diagnostics."""

    return f"{value:.1f} miles" if value is not None else "-"


def _percent_text(value: Decimal | None) -> str:
    """Format a percent value for webhook diagnostics."""

    return f"{value:.1f}%" if value is not None else "-"


def _smartcar_signal_display_rows(event: SmartcarWebhookEvent) -> list[tuple[str, str, str, str]]:
    """Return sorted signal rows with safe display values for the latest webhook card."""

    signals = sorted(
        event.signal_rows,
        key=lambda signal: (
            signal.group or "",
            signal.name or "",
            signal.code or "",
        ),
    )
    return [
        (
            signal.group or "-",
            signal.name or signal.code or "-",
            _display_text(signal.value),
            signal.unit or signal.status or "-",
        )
        for signal in signals
    ]


def _smartcar_webhook_diagnostics(db: Session) -> SmartcarWebhookDiagnostics:
    """Load the latest real Smartcar webhook and summarize the received vehicle data."""

    latest_event = db.scalar(
        select(SmartcarWebhookEvent)
        .options(selectinload(SmartcarWebhookEvent.signal_rows))
        .where(SmartcarWebhookEvent.event_type != MANUAL_ODOMETER_EVENT_TYPE)
        .order_by(SmartcarWebhookEvent.received_at.desc(), SmartcarWebhookEvent.id.desc())
        .limit(1)
    )
    if latest_event is None:
        return SmartcarWebhookDiagnostics(
            event=None,
            age="Never",
            data_rows=[],
            signal_rows=[],
        )

    data_rows = [
        ("Event Type", latest_event.event_type),
        ("Event ID", latest_event.event_id),
        ("Delivery ID", latest_event.delivery_id or "-"),
        ("Webhook", latest_event.webhook_name or latest_event.webhook_id or "-"),
        ("Vehicle", _vehicle_description(latest_event)),
        ("Vehicle ID", latest_event.vehicle_id or "-"),
        ("Mode", latest_event.vehicle_mode or "-"),
        ("Powertrain", latest_event.vehicle_powertrain_type or "-"),
        ("Odometer", _miles_text(latest_event.odometer_miles)),
        ("Odometer Recorded", _format_local_datetime(latest_event.odometer_recorded_at) or "-"),
        ("Fuel", _percent_text(latest_event.fuel_percent)),
        ("Locked", _display_text(latest_event.is_locked)),
        ("Online", _display_text(latest_event.is_online)),
        ("Nickname", latest_event.nickname or "-"),
        ("VIN", _masked_vin(latest_event.vin)),
        ("Firmware", latest_event.firmware_version or "-"),
        ("Signals", str(len(latest_event.signal_rows))),
        ("Triggers", str(len(latest_event.triggers or []))),
    ]
    return SmartcarWebhookDiagnostics(
        event=latest_event,
        age=_human_duration_since(latest_event.received_at),
        data_rows=data_rows,
        signal_rows=_smartcar_signal_display_rows(latest_event),
    )


def _diagnostics_redirect(fragment: str, params: dict[str, str]) -> RedirectResponse:
    query = urlencode(params)
    return RedirectResponse(url=f"/diagnostics?{query}#{fragment}", status_code=303)


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

    latest_webhook_event = latest_webhook_odometer_event(db)
    if latest_webhook_event is not None:
        event_source = odometer_event_source(latest_webhook_event)
        candidates.append(
            {
                "value": latest_webhook_event.odometer_miles,
                "source": event_source,
                "recorded_at": latest_webhook_event.odometer_recorded_at
                or latest_webhook_event.delivered_at
                or latest_webhook_event.received_at,
                "trip": None,
                "database_id": latest_webhook_event.id,
                "position": "Manual" if event_source == "manual" else "Webhook",
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

    record_login_failure(request, settings)
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
    suppressed_trips = list(
        db.scalars(
            select(DeletedTrip)
            .where(DeletedTrip.trip_date >= start)
            .where(DeletedTrip.trip_date < end)
            .order_by(DeletedTrip.trip_date.desc(), DeletedTrip.started_at.desc())
        )
    )
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
            "suppressed_trips": suppressed_trips,
        },
    )


@router.post("/trips/{trip_id}")
def update_trip_form(
    trip_id: int,
    trip_date: date = Form(...),
    origin_name: str = Form(...),
    destination_name: str = Form(...),
    miles: Decimal = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    update_trip_details(trip, origin_name, destination_name, miles, trip_date)
    db.commit()
    logger.info(
        "Updated trip via web form trip_id=%s date=%s origin=%s destination=%s miles=%s",
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


@router.post("/trips")
def create_trip_form(
    trip_date: date = Form(...),
    origin_name: str = Form(...),
    destination_name: str = Form(...),
    miles: Decimal = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if miles < 0:
        raise HTTPException(status_code=400, detail="Miles must be zero or greater")
    trip = create_manual_trip(
        db,
        trip_date=trip_date,
        origin_name=origin_name,
        destination_name=destination_name,
        miles=miles,
    )
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
        raise HTTPException(status_code=404, detail="Trip suppression rule not found")

    logger.info(
        "Removed trip suppression rule deleted_trip_id=%s origin=%s destination=%s "
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
    db: Session = Depends(get_db),
) -> HTMLResponse:
    settings = get_settings()
    log_dir = Path(settings.log_dir)
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
    smartcar_webhook_diagnostics = _smartcar_webhook_diagnostics(db)
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
            "recent_locations": owntracks_entries_page.entries,
            "owntracks_entries_page": owntracks_entries_page,
            "movement_state": movement_diagnostics.current_state,
            "movement_state_changes": movement_diagnostics.state_changes,
            "smartcar_webhook": smartcar_webhook_diagnostics,
            "app_log_lines": _tail_file(log_dir / "app.log", log_level=settings.log_level),
            "manual_odometer_result": _api_test_result(
                odometer_test,
                odometer_message,
                odometer_value,
            ),
            "eia_test_result": _api_test_result(eia_test, eia_message, eia_value),
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

    event = create_manual_odometer_event(db, odometer_miles)
    return _diagnostics_redirect(
        "api-tests",
        {
            "odometer_test": "pass",
            "odometer_message": "Manual odometer reading saved.",
            "odometer_value": f"{event.odometer_miles:.1f} miles",
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
