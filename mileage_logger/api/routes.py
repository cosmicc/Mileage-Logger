import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from mileage_logger.api.deps import verify_owntracks_auth
from mileage_logger.config import get_settings
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
from mileage_logger.services.smartcar import (
    SMARTCAR_VERIFY_EVENT,
    SmartcarWebhookError,
    decode_webhook_payload,
    hash_webhook_challenge,
    store_webhook_payload,
    verify_webhook_signature,
)
from mileage_logger.services.timezone import datetime_to_local, datetime_to_local_date
from mileage_logger.services.trip_processor import (
    run_automatic_trip_processing,
    update_odometer_anchor_from_reading,
)
from mileage_logger.services.waypoints import owntracks_waypoints_json

router = APIRouter()
logger = logging.getLogger(__name__)


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
        logger.debug("Ignored empty OwnTracks payload")
        return JSONResponse(content=[])
    except UnsupportedOwnTracksType as exc:
        logger.debug("Ignored unsupported OwnTracks payload: %s", exc)
        return JSONResponse(content=[])
    return JSONResponse(content=[])


def _smartcar_webhook_unavailable() -> HTTPException:
    """Return a generic unavailable response without leaking token configuration."""
    return HTTPException(status_code=503, detail="Smartcar webhook is not configured")


def _smartcar_verify_unavailable() -> HTTPException:
    """Return a generic VERIFY error when the management token needed for HMAC is missing."""
    return HTTPException(status_code=503, detail="Smartcar webhook verification is not configured")


@router.post("/smartcar/webhook")
@router.post("/smartcar/webhook/")
@router.post("/webhooks/smartcar")
@router.post("/webhooks/smartcar/")
async def smartcar_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, object]:
    settings = get_settings()
    raw_body = await request.body()
    if len(raw_body) > settings.smartcar_webhook_max_body_bytes:
        logger.warning(
            "Rejected Smartcar webhook oversized_body bytes=%s max_bytes=%s client=%s",
            len(raw_body),
            settings.smartcar_webhook_max_body_bytes,
            request.client.host if request.client else "",
        )
        raise HTTPException(status_code=413, detail="Smartcar webhook body is too large")

    try:
        payload = decode_webhook_payload(raw_body)
    except SmartcarWebhookError as exc:
        logger.warning("Rejected malformed Smartcar webhook: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid Smartcar webhook payload") from exc

    event_type = str(payload.get("eventType") or "").strip().upper()
    if event_type == SMARTCAR_VERIFY_EVENT:
        if not settings.smartcar_management_token:
            raise _smartcar_verify_unavailable()

        try:
            data = payload.get("data")
            challenge = str(data.get("challenge") if isinstance(data, dict) else "")
            challenge_response = hash_webhook_challenge(
                settings.smartcar_management_token,
                challenge,
            )
        except SmartcarWebhookError as exc:
            logger.warning("Rejected Smartcar VERIFY webhook: %s", exc)
            raise HTTPException(status_code=400, detail="Invalid Smartcar VERIFY payload") from exc

        logger.info("Answered Smartcar VERIFY webhook event_id=%s", payload.get("eventId") or "")
        return {"challenge": challenge_response}

    if not settings.smartcar_enabled or not settings.smartcar_management_token:
        raise _smartcar_webhook_unavailable()

    signature = request.headers.get("SC-Signature")
    if not verify_webhook_signature(
        raw_body,
        signature,
        settings.smartcar_management_token,
    ):
        logger.warning(
            "Rejected Smartcar webhook invalid_signature event_type=%s bytes=%s client=%s",
            event_type or "",
            len(raw_body),
            request.client.host if request.client else "",
        )
        raise HTTPException(status_code=401, detail="Invalid Smartcar webhook signature")

    try:
        result = store_webhook_payload(db, payload, settings=settings)
    except SmartcarWebhookError as exc:
        logger.warning("Rejected Smartcar webhook after signature verification: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid Smartcar webhook payload") from exc

    if result.created and result.event.odometer_miles is not None:
        touched_datetime = (
            result.event.odometer_recorded_at
            or result.event.delivered_at
            or result.event.received_at
        )
        update_odometer_anchor_from_reading(
            db,
            result.event.odometer_miles,
            recorded_at=touched_datetime,
            source="smartcar",
        )
        touched_date = datetime_to_local_date(touched_datetime)
        finalize_completed_days = touched_date >= datetime_to_local_date(datetime.now(UTC))
        try:
            run_automatic_trip_processing(
                db,
                touched_date=touched_date,
                finalize_completed_days=finalize_completed_days,
            )
        except Exception:
            logger.exception(
                "Automatic trip processing failed after Smartcar webhook event_id=%s",
                result.event.event_id,
            )

    return {
        "status": "received",
        "created": result.created,
        "event_id": result.event.event_id,
        "database_id": result.event.id,
    }


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
    update_trip_details(
        trip,
        update.origin_name,
        update.destination_name,
        update.miles,
        update.trip_date,
    )
    db.commit()
    logger.info(
        "Updated trip via API trip_id=%s origin=%s destination=%s miles=%s",
        trip.id,
        trip.origin_display_name,
        trip.destination_display_name,
        trip.miles,
    )
    return {"status": "updated"}


@router.post("/gas-prices/current")
def snapshot_current_gas_price(db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        snapshot = fetch_and_save_current_snapshot(db)
    except GasPriceUnavailable as exc:
        logger.warning("Current gas price snapshot unavailable: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info(
        "Saved current gas price snapshot via API date=%s price=%s",
        snapshot.observed_on,
        snapshot.price_per_gallon,
    )
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
    logger.info(
        "Saved manual monthly gas price via API year=%s month=%s state=%s effective_rate=%s",
        monthly.year,
        monthly.month,
        monthly.state,
        monthly.effective_rate,
    )
    return {"status": "saved", "effective_rate": str(monthly.effective_rate)}


@router.post("/reports/{year}/{month}")
def generate_report(year: int, month: int, db: Session = Depends(get_db)) -> Response:
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="month must be 1 through 12")
    try:
        report = generate_monthly_pdf(db, year, month)
    except GasPriceUnavailable as exc:
        logger.warning("Report generation unavailable year=%s month=%s error=%s", year, month, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info(
        "Generated report via API year=%s month=%s filename=%s",
        year,
        month,
        report.filename,
    )
    return Response(
        content=report.content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{report.filename}"'},
    )
