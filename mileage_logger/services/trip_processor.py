import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from threading import Event, Lock, Thread

from sqlalchemy import select
from sqlalchemy.orm import Session

from mileage_logger.config import get_settings
from mileage_logger.database import SessionLocal
from mileage_logger.models import (
    AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
    OwnTracksLocation,
    Trip,
    TripProcessingCheckpoint,
)
from mileage_logger.services.fordpass import current_odometer_miles
from mileage_logger.services.mileage import (
    generate_trips,
)
from mileage_logger.services.retention import MonthlyResetResult, reset_previous_month_data
from mileage_logger.services.timezone import datetime_to_local_date

logger = logging.getLogger(__name__)
trip_logger = logging.getLogger("mileage_logger.trip_calculation")
_PROCESSING_LOCK = Lock()


@dataclass(frozen=True)
class TripProcessingResult:
    generated: int
    monthly_reset: MonthlyResetResult
    processed_dates: tuple[date, ...]
    processed_location_count: int = 0
    checkpoint_location_id: int | None = None


def _get_or_create_checkpoint(db: Session) -> TripProcessingCheckpoint:
    checkpoint = db.scalar(
        select(TripProcessingCheckpoint).where(
            TripProcessingCheckpoint.name == AUTOMATIC_TRIP_PROCESSING_CHECKPOINT
        )
    )
    if checkpoint is not None:
        return checkpoint

    checkpoint = TripProcessingCheckpoint(name=AUTOMATIC_TRIP_PROCESSING_CHECKPOINT)
    db.add(checkpoint)
    db.flush()
    return checkpoint


def _new_locations_after_checkpoint(
    db: Session,
    checkpoint: TripProcessingCheckpoint,
) -> list[OwnTracksLocation]:
    stmt = select(OwnTracksLocation).order_by(OwnTracksLocation.id.asc())
    if checkpoint.last_owntracks_location_id is not None:
        stmt = stmt.where(OwnTracksLocation.id > checkpoint.last_owntracks_location_id)
    return list(db.scalars(stmt))


def _has_any_trip_odometer(db: Session) -> bool:
    trip_id = db.scalar(
        select(Trip.id)
        .where(
            (Trip.start_odometer_miles.is_not(None))
            | (Trip.end_odometer_miles.is_not(None))
        )
        .limit(1)
    )
    return trip_id is not None


def _ensure_initial_odometer_anchor(
    db: Session,
    checkpoint: TripProcessingCheckpoint,
    *,
    current_dt: datetime,
) -> None:
    if checkpoint.odometer_anchor_miles is not None or _has_any_trip_odometer(db):
        return

    odometer = current_odometer_miles()
    if odometer is None:
        trip_logger.debug("Initial FordPass odometer anchor unavailable")
        return

    checkpoint.odometer_anchor_miles = odometer
    checkpoint.odometer_anchor_recorded_at = current_dt
    trip_logger.info(
        "Saved initial FordPass odometer anchor miles=%s recorded_at=%s",
        odometer,
        current_dt.isoformat(),
    )


def _generate_for_date(
    db: Session,
    day: date,
    processed_dates: list[date],
    *,
    as_of: datetime,
) -> int:
    if day in processed_dates:
        return 0
    processed_dates.append(day)
    return len(generate_trips(db, day, day, as_of=as_of))


def _dates_touched_by_new_locations(locations: list[OwnTracksLocation]) -> set[date]:
    touched_dates: set[date] = set()
    for location in locations:
        location_date = datetime_to_local_date(location.captured_at)
        touched_dates.add(location_date)
        touched_dates.add(location_date - timedelta(days=1))
    return touched_dates


def run_automatic_trip_processing(
    db: Session,
    *,
    touched_date: date | None = None,
    now: datetime | None = None,
    finalize_completed_days: bool = True,
) -> TripProcessingResult:
    current_dt = now or datetime.now(UTC)
    today = datetime_to_local_date(current_dt)
    generated = 0
    monthly_reset = MonthlyResetResult(location_points=0, trips=0, gas_snapshots=0)
    processed_dates: list[date] = []
    processed_location_count = 0
    checkpoint_location_id: int | None = None

    with _PROCESSING_LOCK:
        trip_logger.info(
            "automatic trip processing started touched_date=%s now=%s finalize_completed_days=%s",
            touched_date.isoformat() if touched_date is not None else "",
            current_dt.isoformat(),
            finalize_completed_days,
        )
        checkpoint = _get_or_create_checkpoint(db)
        _ensure_initial_odometer_anchor(db, checkpoint, current_dt=current_dt)
        checkpoint_location_id = checkpoint.last_owntracks_location_id

        new_locations = _new_locations_after_checkpoint(db, checkpoint)
        dates_to_process = _dates_touched_by_new_locations(new_locations)
        if touched_date is not None:
            dates_to_process.add(touched_date)
            dates_to_process.add(touched_date - timedelta(days=1))

        if not finalize_completed_days:
            dates_to_process = {day for day in dates_to_process if day <= today}

        for day in sorted(dates_to_process):
            generated += _generate_for_date(db, day, processed_dates, as_of=current_dt)

        if new_locations:
            checkpoint.last_owntracks_location_id = max(location.id for location in new_locations)
            checkpoint_location_id = checkpoint.last_owntracks_location_id
            processed_location_count = len(new_locations)
            db.commit()

        monthly_reset = reset_previous_month_data(db, now=current_dt)

    result = TripProcessingResult(
        generated=generated,
        monthly_reset=monthly_reset,
        processed_dates=tuple(processed_dates),
        processed_location_count=processed_location_count,
        checkpoint_location_id=checkpoint_location_id,
    )
    trip_logger.info(
        "automatic trip processing complete generated=%s reset_location_points=%s "
        "reset_trips=%s reset_gas_snapshots=%s processed_locations=%s checkpoint_location_id=%s "
        "dates=%s",
        result.generated,
        result.monthly_reset.location_points,
        result.monthly_reset.trips,
        result.monthly_reset.gas_snapshots,
        result.processed_location_count,
        result.checkpoint_location_id or "",
        ",".join(day.isoformat() for day in result.processed_dates),
    )
    return result


class AutomaticTripProcessor:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if not self.settings.automatic_trip_processing_enabled:
            logger.info("Automatic trip processing is disabled")
            return
        if self._thread is not None:
            logger.debug("Automatic trip processor already running")
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="automatic-trip-processor", daemon=True)
        self._thread.start()
        logger.info(
            "Automatic trip processor started interval_seconds=%s",
            self.settings.automatic_trip_processing_interval_seconds,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
            logger.info("Automatic trip processor stopped")

    def _run(self) -> None:
        self._process_once()
        while not self._stop.wait(self.settings.automatic_trip_processing_interval_seconds):
            self._process_once()

    def _process_once(self) -> None:
        with SessionLocal() as db:
            try:
                result = run_automatic_trip_processing(db)
            except Exception:
                logger.exception("Automatic trip processing failed")
                return

        if result.generated or result.monthly_reset.total:
            logger.info(
                "Automatic trip processing complete generated=%s "
                "reset_location_points=%s reset_trips=%s reset_gas_snapshots=%s dates=%s",
                result.generated,
                result.monthly_reset.location_points,
                result.monthly_reset.trips,
                result.monthly_reset.gas_snapshots,
                ",".join(day.isoformat() for day in result.processed_dates),
            )
