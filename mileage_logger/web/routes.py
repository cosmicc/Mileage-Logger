from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from mileage_logger.database import get_db
from mileage_logger.models import MonthlyGasPrice, MonthlyReport, OwnTracksLocation, Site, Trip
from mileage_logger.services.gas_prices import (
    GasPriceUnavailable,
    fetch_and_save_current_snapshot,
    upsert_manual_monthly_price,
)
from mileage_logger.services.mileage import generate_trips
from mileage_logger.services.pdf import generate_monthly_pdf

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def _current_year_month() -> tuple[int, int]:
    today = datetime.now(UTC).date()
    return today.year, today.month


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    year, month = _current_year_month()
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
        },
    )


@router.post("/generate-trips")
def generate_trips_form(
    start_date: date = Form(...),
    end_date: date = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    generate_trips(db, start_date, end_date)
    return RedirectResponse(url="/trips", status_code=303)


@router.get("/trips", response_class=HTMLResponse)
def trips(
    request: Request,
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if year is None or month is None:
        year, month = _current_year_month()
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
    monthly_gas = db.scalar(
        select(MonthlyGasPrice)
        .where(MonthlyGasPrice.year == year)
        .where(MonthlyGasPrice.month == month)
        .limit(1)
    )
    return templates.TemplateResponse(
        request,
        "trips.html",
        {
            "trips": all_trips,
            "year": year,
            "month": month,
            "monthly_gas": monthly_gas,
        },
    )


@router.post("/trips/{trip_id}")
def update_trip_form(
    trip_id: int,
    miles: Decimal = Form(...),
    include_in_report: str | None = Form(default=None),
    notes: str = Form(default=""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    trip.miles = miles.quantize(Decimal("0.01"))
    trip.include_in_report = include_in_report == "on"
    trip.notes = notes
    db.commit()
    return RedirectResponse(
        url=f"/trips?year={trip.trip_date.year}&month={trip.trip_date.month}",
        status_code=303,
    )


@router.get("/sites", response_class=HTMLResponse)
def sites(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    all_sites = list(db.scalars(select(Site).order_by(Site.name.asc())))
    return templates.TemplateResponse(request, "sites.html", {"sites": all_sites})


@router.post("/sites")
def create_site_form(
    name: str = Form(...),
    latitude: Decimal = Form(...),
    longitude: Decimal = Form(...),
    radius_m: int = Form(150),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    db.add(Site(name=name, latitude=latitude, longitude=longitude, radius_m=radius_m))
    db.commit()
    return RedirectResponse(url="/sites", status_code=303)


@router.post("/gas-prices/snapshot")
def snapshot_gas_price_form(db: Session = Depends(get_db)) -> RedirectResponse:
    try:
        fetch_and_save_current_snapshot(db)
    except GasPriceUnavailable:
        pass
    return RedirectResponse(url="/trips", status_code=303)


@router.post("/gas-prices/monthly")
def manual_gas_price_form(
    year: int = Form(...),
    month: int = Form(...),
    average_price_per_gallon: Decimal = Form(...),
    buffer_per_gallon: Decimal = Form(Decimal("0.50")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    upsert_manual_monthly_price(
        db,
        year=year,
        month=month,
        state="MI",
        average_price_per_gallon=average_price_per_gallon,
        buffer_per_gallon=buffer_per_gallon,
        source_detail="web form",
    )
    return RedirectResponse(url=f"/trips?year={year}&month={month}", status_code=303)


@router.post("/reports/{year}/{month}")
def report_form(year: int, month: int, db: Session = Depends(get_db)) -> RedirectResponse:
    generate_monthly_pdf(db, year, month)
    return RedirectResponse(url=f"/trips?year={year}&month={month}", status_code=303)
