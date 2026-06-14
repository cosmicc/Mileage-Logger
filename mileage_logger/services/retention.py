import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete
from sqlalchemy.orm import Session

from mileage_logger.models import GasPriceSnapshot, OwnTracksLocation
from mileage_logger.services.timezone import datetime_to_local, local_day_bounds

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MonthlyResetResult:
    location_points: int
    trips: int
    gas_snapshots: int

    @property
    def total(self) -> int:
        return self.location_points + self.trips + self.gas_snapshots


def _rowcount(value: int | None) -> int:
    return value if value is not None and value > 0 else 0


def reset_previous_month_data(
    db: Session,
    *,
    now: datetime | None = None,
) -> MonthlyResetResult:
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

    result = MonthlyResetResult(
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
