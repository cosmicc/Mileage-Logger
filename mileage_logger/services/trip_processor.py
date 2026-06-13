import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from threading import Event, Lock, Thread

from sqlalchemy import select
from sqlalchemy.orm import Session

from mileage_logger.config import get_settings
from mileage_logger.database import SessionLocal
from mileage_logger.models import OwnTracksLocation
from mileage_logger.services.mileage import (
    date_bounds,
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


def _has_owntracks_locations_for_date(db: Session, day: date) -> bool:
    start_dt, end_dt = date_bounds(day)
    location_id = db.scalar(
        select(OwnTracksLocation.id)
        .where(OwnTracksLocation.captured_at >= start_dt)
        .where(OwnTracksLocation.captured_at < end_dt)
        .limit(1)
    )
    return location_id is not None


def _oldest_owntracks_date(db: Session) -> date | None:
    captured_at = db.scalar(
        select(OwnTracksLocation.captured_at)
        .order_by(OwnTracksLocation.captured_at.asc())
        .limit(1)
    )
    return datetime_to_local_date(captured_at) if captured_at is not None else None


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


def _generate_for_range(
    db: Session,
    start_date: date,
    end_date: date,
    processed_dates: list[date],
    *,
    as_of: datetime,
) -> int:
    day = start_date
    while day <= end_date:
        if day not in processed_dates:
            processed_dates.append(day)
        day += timedelta(days=1)
    return len(generate_trips(db, start_date, end_date, as_of=as_of))


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

    with _PROCESSING_LOCK:
        trip_logger.info(
            "automatic trip processing started touched_date=%s now=%s finalize_completed_days=%s",
            touched_date.isoformat() if touched_date is not None else "",
            current_dt.isoformat(),
            finalize_completed_days,
        )
        monthly_reset = reset_previous_month_data(db, now=current_dt)
        if touched_date is not None:
            generated += _generate_for_range(
                db,
                touched_date - timedelta(days=1),
                touched_date,
                processed_dates,
                as_of=current_dt,
            )
        elif _has_owntracks_locations_for_date(db, today):
            generated += _generate_for_date(db, today, processed_dates, as_of=current_dt)

        if finalize_completed_days:
            oldest_date = _oldest_owntracks_date(db)
            if oldest_date is not None:
                day = oldest_date
                while day < today:
                    generated += _generate_for_date(db, day, processed_dates, as_of=current_dt)
                    day += timedelta(days=1)

    result = TripProcessingResult(
        generated=generated,
        monthly_reset=monthly_reset,
        processed_dates=tuple(processed_dates),
    )
    trip_logger.info(
        "automatic trip processing complete generated=%s reset_location_points=%s "
        "reset_trips=%s reset_gas_snapshots=%s dates=%s",
        result.generated,
        result.monthly_reset.location_points,
        result.monthly_reset.trips,
        result.monthly_reset.gas_snapshots,
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
