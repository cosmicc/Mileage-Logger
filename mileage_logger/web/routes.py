from calendar import month_name
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
    MonthlyReport,
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
from mileage_logger.services.timezone import datetime_to_local, local_now, local_today
from mileage_logger.services.waypoints import owntracks_waypoints_json

router = APIRouter()
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


templates.env.filters["local_datetime"] = _format_local_datetime
templates.env.filters["odometer"] = _format_odometer
WAYPOINT_PAGE_SIZE = 20


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


def _tail_file(path: Path, max_lines: int = 200) -> list[str]:
    if not path.exists():
        return []
    with path.open("rb") as file:
        file.seek(0, 2)
        size = file.tell()
        file.seek(max(size - 80_000, 0))
        text = file.read().decode("utf-8", errors="replace")
    return text.splitlines()[-max_lines:]


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
    recent_trips = list(
        db.scalars(
            select(Trip)
            .options(joinedload(Trip.origin_site), joinedload(Trip.destination_site))
            .order_by(Trip.trip_date.desc(), Trip.started_at.desc())
            .limit(8)
        )
    )
    latest_report = db.scalar(
        select(MonthlyReport).order_by(MonthlyReport.created_at.desc()).limit(1)
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
            "recent_trips": recent_trips,
            "latest_report": latest_report,
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
        .order_by(Trip.trip_date.asc(), Trip.started_at.asc())
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
            .order_by(Site.name.asc())
            .offset((pagination["page"] - 1) * pagination["page_size"])
            .limit(pagination["page_size"])
        )
    )
    return templates.TemplateResponse(
        request,
        "waypoints.html",
        {"waypoints": all_waypoints, "waypoint_pagination": pagination},
    )


@router.get("/waypoints/export")
def export_waypoints(db: Session = Depends(get_db)) -> Response:
    all_waypoints = list(db.scalars(select(Site).order_by(Site.name.asc())))
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
    except GasPriceUnavailable:
        pass
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
            "report_count": db.scalar(select(func.count(MonthlyReport.id))) or 0,
            "latest_location": latest_location,
            "latest_snapshot": latest_snapshot,
            "latest_monthly_gas": latest_monthly_gas,
            "recent_locations": owntracks_entries_page.entries,
            "owntracks_entries_page": owntracks_entries_page,
            "app_log_lines": _tail_file(log_dir / "app.log"),
            "gas_log_lines": _tail_file(log_dir / "gas-snapshot.log"),
        },
    )


@router.post("/reports/{year}/{month}")
def report_form(year: int, month: int, db: Session = Depends(get_db)) -> FileResponse:
    _validate_month(month)
    try:
        report = generate_monthly_pdf(db, year, month)
    except GasPriceUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(
        report.pdf_path,
        media_type="application/pdf",
        filename=f"mileage-{year}-{month:02d}.pdf",
    )
