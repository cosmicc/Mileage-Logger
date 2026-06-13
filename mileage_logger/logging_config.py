import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from mileage_logger.config import get_settings
from mileage_logger.services.timezone import local_timezone

TRIP_CALCULATION_LOGGER = "mileage_logger.trip_calculation"


class LocalTimezoneFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        value = datetime.fromtimestamp(record.created, tz=local_timezone())
        if datefmt:
            return value.strftime(datefmt)
        return value.isoformat(timespec="seconds")


def _configure_named_file_logger(
    *,
    logger_name: str,
    log_path: Path,
    marker: str,
    formatter: logging.Formatter,
) -> None:
    named_logger = logging.getLogger(logger_name)
    named_logger.setLevel(logging.INFO)
    named_logger.propagate = False

    for handler in named_logger.handlers:
        if getattr(handler, "_mileage_logger_marker", "") == marker:
            return

    file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3)
    file_handler.setFormatter(formatter)
    file_handler._mileage_logger_marker = marker  # type: ignore[attr-defined]
    named_logger.addHandler(file_handler)


def configure_logging(process_name: str) -> Path:
    settings = get_settings()
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{process_name}.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    marker = f"mileage_logger_{process_name}_file"
    formatter = LocalTimezoneFormatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %Z",
    )

    trip_log_path = log_dir / "trip-calculation.log"
    _configure_named_file_logger(
        logger_name=TRIP_CALCULATION_LOGGER,
        log_path=trip_log_path,
        marker="mileage_logger_trip_calculation_file",
        formatter=formatter,
    )

    for handler in root_logger.handlers:
        if getattr(handler, "_mileage_logger_marker", "") == marker:
            return log_path

    file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3)
    file_handler.setFormatter(formatter)
    file_handler._mileage_logger_marker = marker  # type: ignore[attr-defined]
    root_logger.addHandler(file_handler)

    return log_path
