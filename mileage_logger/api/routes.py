from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from mileage_logger.api.deps import verify_owntracks_auth
from mileage_logger.database import get_db
from mileage_logger.models import OwnTracksLocation, Site, Trip
from mileage_logger.schemas import MonthlyGasPriceCreate, TripUpdate, WaypointRead
from mileage_logger.services.gas_prices import (
    GasPriceUnavailable,
    fetch_and_save_current_snapshot,
    upsert_manual_monthly_price,
)
from mileage_logger.services.mileage import update_trip_details
from mileage_logger.services.owntracks import (
    EmptyOwnTracksPayload,
    UnsupportedOwnTracksType,
    process_owntracks_payload,
)
from mileage_logger.services.pdf import generate_monthly_pdf
from mileage_logger.services.timezone import datetime_to_local
from mileage_logger.services.waypoints import owntracks_waypoints_json

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
        process_owntracks_payload(
            db,
            body,
            topic=request.query_params.get("topic"),
            user=request.headers.get("x-limit-u") or request.query_params.get("u"),
            device=request.headers.get("x-limit-d") or request.query_params.get("d"),
        )
    except EmptyOwnTracksPayload:
        return JSONResponse(content=[])
    except UnsupportedOwnTracksType:
        return JSONResponse(content=[])
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
            "captured_at": datetime_to_local(location.captured_at).isoformat(),
            "latitude": str(location.latitude),
            "longitude": str(location.longitude),
            "accuracy_m": location.accuracy_m,
        }
        for location in db.scalars(stmt)
    ]


@router.get("/sites", response_model=list[WaypointRead])
@router.get("/waypoints", response_model=list[WaypointRead])
def list_waypoints(db: Session = Depends(get_db)) -> list[Site]:
    return list(
        db.scalars(
            select(Site).order_by(
                Site.last_visited_at.desc().nulls_last(),
                Site.created_at.desc(),
                Site.name.asc(),
            )
        )
    )


@router.get("/waypoints/export")
def export_waypoints(db: Session = Depends(get_db)) -> Response:
    all_waypoints = list(
        db.scalars(
            select(Site).order_by(
                Site.last_visited_at.desc().nulls_last(),
                Site.created_at.desc(),
                Site.name.asc(),
            )
        )
    )
    return Response(
        content=owntracks_waypoints_json(all_waypoints),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="owntracks-waypoints.json"'},
    )


@router.patch("/trips/{trip_id}")
def update_trip(trip_id: int, update: TripUpdate, db: Session = Depends(get_db)) -> dict[str, str]:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    update_trip_details(trip, update.origin_name, update.destination_name, update.miles)
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
def generate_report(year: int, month: int, db: Session = Depends(get_db)) -> Response:
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="month must be 1 through 12")
    try:
        report = generate_monthly_pdf(db, year, month)
    except GasPriceUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=report.content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{report.filename}"'},
    )
