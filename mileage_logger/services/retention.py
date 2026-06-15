import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from mileage_logger.config import get_settings
from mileage_logger.models import GasPriceSnapshot, OwnTracksLocation
from mileage_logger.services.timezone import datetime_to_local, local_day_bounds

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetentionResult:
    location_points: int
    trips: int
    gas_snapshots: int

    @property
    def total(self) -> int:
        return self.location_points + self.trips + self.gas_snapshots


def _rowcount(value: int | None) -> int:
    return value if value is not None and value > 0 else 0


def _empty_retention_result() -> RetentionResult:
    return RetentionResult(location_points=0, trips=0, gas_snapshots=0)


def _owntracks_retention_cutoff(
    *,
    now: datetime,
    retention_days: int,
) -> datetime:
    local_dt = datetime_to_local(now)
    cutoff_date = local_dt.date() - timedelta(days=retention_days)
    cutoff_dt, _ = local_day_bounds(cutoff_date)
    return cutoff_dt


def purge_processed_owntracks_locations(
    db: Session,
    *,
    checkpoint_location_id: int | None,
    now: datetime | None = None,
    enabled: bool | None = None,
    retention_days: int | None = None,
) -> RetentionResult:
    settings = get_settings()
    purge_enabled = settings.owntracks_purge_enabled if enabled is None else enabled
    if not purge_enabled:
        logger.debug("OwnTracks retention purge skipped because purge is disabled")
        return _empty_retention_result()

    if checkpoint_location_id is None:
        logger.debug("OwnTracks retention purge skipped because no checkpoint exists")
        return _empty_retention_result()

    current_dt = now or datetime.now(UTC)
    configured_retention_days = (
        settings.owntracks_location_retention_days
        if retention_days is None
        else retention_days
    )
    cutoff_dt = _owntracks_retention_cutoff(
        now=current_dt,
        retention_days=configured_retention_days,
    )
    location_result = db.execute(
        delete(OwnTracksLocation)
        .where(OwnTracksLocation.id <= checkpoint_location_id)
        .where(OwnTracksLocation.captured_at < cutoff_dt)
        .execution_options(synchronize_session=False)
    )
    db.commit()

    result = RetentionResult(
        location_points=_rowcount(location_result.rowcount),
        trips=0,
        gas_snapshots=0,
    )
    if result.location_points:
        logger.info(
            "OwnTracks retention purge removed processed rows cutoff=%s "
            "checkpoint_location_id=%s location_points=%s retention_days=%s",
            cutoff_dt.isoformat(),
            checkpoint_location_id,
            result.location_points,
            configured_retention_days,
        )
    else:
        logger.debug(
            "OwnTracks retention purge found no rows cutoff=%s checkpoint_location_id=%s "
            "retention_days=%s",
            cutoff_dt.isoformat(),
            checkpoint_location_id,
            configured_retention_days,
        )
    return result


def reset_previous_month_data(
    db: Session,
    *,
    now: datetime | None = None,
) -> RetentionResult:
    current_dt = now or datetime.now(UTC)
    local_dt = datetime_to_local(current_dt)
    month_start_date = local_dt.date().replace(day=1)
    month_start_dt, _ = local_day_bounds(month_start_date)

    location_result = db.execute(
        delete(OwnTracksLocation)
        .where(OwnTracksLocation.captured_at < month_start_dt)
        .execution_options(synchronize_session=False)
    )
    gas_snapshot_result = db.execute(
        delete(GasPriceSnapshot)
        .where(GasPriceSnapshot.observed_on < month_start_date)
        .execution_options(synchronize_session=False)
    )
    db.commit()

    result = RetentionResult(
        location_points=_rowcount(location_result.rowcount),
        trips=0,
        gas_snapshots=_rowcount(gas_snapshot_result.rowcount),
    )
    if result.total:
        logger.info(
            "Monthly reset removed old records month_start=%s location_points=%s trips=%s "
            "gas_snapshots=%s",
            month_start_date.isoformat(),
            result.location_points,
            result.trips,
            result.gas_snapshots,
        )
    else:
        logger.debug(
            "Monthly reset found no old records month_start=%s",
            month_start_date.isoformat(),
        )
    return result


MonthlyResetResult = RetentionResult
