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
    purge_processed_owntracks_locations,
)

logger = logging.getLogger(__name__)
_PROCESSING_LOCK = Lock()


@dataclass(frozen=True)
class TripProcessingResult:
    generated: int
    purged_owntracks: int
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
    return captured_at.date() if captured_at is not None else None


def _generate_for_date(db: Session, day: date, processed_dates: list[date]) -> int:
    if day in processed_dates:
        return 0
    processed_dates.append(day)
    return len(generate_trips(db, day, day))


def run_automatic_trip_processing(
    db: Session,
    *,
    touched_date: date | None = None,
    now: datetime | None = None,
    finalize_completed_days: bool = True,
) -> TripProcessingResult:
    current_dt = now or datetime.now(UTC)
    today = current_dt.date()
    generated = 0
    purged = 0
    processed_dates: list[date] = []

    with _PROCESSING_LOCK:
        if touched_date is not None:
            generated += _generate_for_date(db, touched_date, processed_dates)
        elif _has_owntracks_locations_for_date(db, today):
            generated += _generate_for_date(db, today, processed_dates)

        if finalize_completed_days:
            oldest_date = _oldest_owntracks_date(db)
            if oldest_date is not None:
                day = oldest_date
                while day < today:
                    generated += _generate_for_date(db, day, processed_dates)
                    purged += purge_processed_owntracks_locations(db, day, day, now=current_dt)
                    day += timedelta(days=1)

    return TripProcessingResult(
        generated=generated,
        purged_owntracks=purged,
        processed_dates=tuple(processed_dates),
    )


class AutomaticTripProcessor:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if not self.settings.automatic_trip_processing_enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="automatic-trip-processor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

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

        if result.generated or result.purged_owntracks:
            logger.info(
                "Automatic trip processing complete generated=%s purged_owntracks=%s dates=%s",
                result.generated,
                result.purged_owntracks,
                ",".join(day.isoformat() for day in result.processed_dates),
            )
