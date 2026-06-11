from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from mileage_logger.api.deps import verify_owntracks_auth
from mileage_logger.database import get_db
from mileage_logger.models import OwnTracksLocation, Site, Trip
from mileage_logger.schemas import MonthlyGasPriceCreate, SiteCreate, SiteRead, TripUpdate
from mileage_logger.services.gas_prices import (
    GasPriceUnavailable,
    fetch_and_save_current_snapshot,
    upsert_manual_monthly_price,
)
from mileage_logger.services.mileage import generate_trips
from mileage_logger.services.owntracks import (
    EmptyOwnTracksPayload,
    UnsupportedOwnTracksType,
    parse_owntracks_location,
    store_owntracks_location,
)
from mileage_logger.services.pdf import generate_monthly_pdf

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/owntracks")
@router.post("/owntracks/")
@router.post("/pub")
async def owntracks_http(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    verify_owntracks_auth(request)
    body = await request.body()
    try:
        message = parse_owntracks_location(
            body,
            topic=request.query_params.get("topic"),
            user=request.headers.get("x-limit-u") or request.query_params.get("u"),
            device=request.headers.get("x-limit-d") or request.query_params.get("d"),
        )
    except EmptyOwnTracksPayload:
        return JSONResponse(content=[])
    except UnsupportedOwnTracksType:
        return JSONResponse(content=[])

    store_owntracks_location(db, message)
    return JSONResponse(content=[])


@router.get("/locations")
def locations(
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[dict]:
    stmt = select(OwnTracksLocation).order_by(OwnTracksLocation.captured_at.desc()).limit(limit)
    return [
        {
            "id": location.id,
            "user": location.user,
            "device": location.device,
            "captured_at": location.captured_at.isoformat(),
            "latitude": str(location.latitude),
            "longitude": str(location.longitude),
            "accuracy_m": location.accuracy_m,
        }
        for location in db.scalars(stmt)
    ]


@router.post("/sites", response_model=SiteRead, status_code=status.HTTP_201_CREATED)
def create_site(site_in: SiteCreate, db: Session = Depends(get_db)) -> Site:
    site = Site(**site_in.model_dump())
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


@router.get("/sites", response_model=list[SiteRead])
def list_sites(db: Session = Depends(get_db)) -> list[Site]:
    return list(db.scalars(select(Site).order_by(Site.name.asc())))


@router.post("/trips/generate")
def generate_trips_endpoint(
    start_date: date,
    end_date: date,
    db: Session = Depends(get_db),
) -> dict[str, int]:
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date")
    trips = generate_trips(db, start_date, end_date)
    return {"generated": len(trips)}


@router.patch("/trips/{trip_id}")
def update_trip(trip_id: int, update: TripUpdate, db: Session = Depends(get_db)) -> dict[str, str]:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    if update.miles is not None:
        trip.miles = update.miles.quantize(Decimal("0.01"))
    if update.include_in_report is not None:
        trip.include_in_report = update.include_in_report
    if update.notes is not None:
        trip.notes = update.notes
    db.commit()
    return {"status": "updated"}


@router.post("/gas-prices/current")
def snapshot_current_gas_price(db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        snapshot = fetch_and_save_current_snapshot(db)
    except GasPriceUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "saved",
        "observed_on": snapshot.observed_on.isoformat(),
        "price_per_gallon": str(snapshot.price_per_gallon),
    }


@router.post("/gas-prices/monthly")
def manual_monthly_gas_price(
    payload: MonthlyGasPriceCreate,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    monthly = upsert_manual_monthly_price(db, **payload.model_dump())
    return {"status": "saved", "effective_rate": str(monthly.effective_rate)}


@router.post("/reports/{year}/{month}")
def generate_report(year: int, month: int, db: Session = Depends(get_db)) -> dict[str, str]:
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="month must be 1 through 12")
    try:
        report = generate_monthly_pdf(db, year, month)
    except GasPriceUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "generated",
        "pdf_path": report.pdf_path,
        "total_miles": str(report.total_miles),
        "reimbursement_total": str(report.reimbursement_total),
    }
