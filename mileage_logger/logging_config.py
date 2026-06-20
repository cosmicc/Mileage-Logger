import logging
import re
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from mileage_logger.config import Settings, get_settings
from mileage_logger.services.timezone import local_timezone

TRIP_CALCULATION_LOGGER = "mileage_logger.trip_calculation"
LOGIN_FAILURE_LOGGER = "mileage_logger.login_failures"
SENSITIVE_QUERY_VALUE_RE = re.compile(
    r"(?i)(\b(?:api_key|apikey|access_token|refresh_token|token|client_secret|password)=)"
    r"([^&\s\"']+)"
)
SENSITIVE_BEARER_VALUE_RE = re.compile(r"(?i)(authorization:\s*bearer\s+)([^\s\"']+)")
LOG_LEVEL_VALUES = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
}


def redact_sensitive_text(value: str) -> str:
    redacted = SENSITIVE_QUERY_VALUE_RE.sub(r"\1***", value)
    return SENSITIVE_BEARER_VALUE_RE.sub(r"\1***", redacted)


class LocalTimezoneFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        if "\n" not in formatted:
            return redact_sensitive_text(formatted)
        line_prefix = f"{self.formatTime(record, self.datefmt)} {record.levelname} [{record.name}] "
        lines = formatted.splitlines()
        prefixed = "\n".join([lines[0], *(f"{line_prefix}{line}" for line in lines[1:])])
        return redact_sensitive_text(prefixed)

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
    level: int,
    propagate: bool = True,
) -> None:
    named_logger = logging.getLogger(logger_name)
    named_logger.setLevel(level)
    named_logger.propagate = propagate

    for handler in named_logger.handlers:
        if getattr(handler, "_mileage_logger_marker", "") == marker:
            handler_path = Path(getattr(handler, "baseFilename", ""))
            if handler_path == log_path:
                handler.setLevel(level)
                handler.setFormatter(formatter)
                return
            named_logger.removeHandler(handler)
            handler.close()

    file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3)
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    file_handler._mileage_logger_marker = marker  # type: ignore[attr-defined]
    named_logger.addHandler(file_handler)


def log_level_value(settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    return LOG_LEVEL_VALUES[settings.log_level]


def configure_login_failure_logging(settings: Settings | None = None) -> Path | None:
    """Configure the dedicated structured web-login failure audit log."""

    active_settings = settings or get_settings()
    log_path = Path(active_settings.login_failure_log_path)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _configure_named_file_logger(
            logger_name=LOGIN_FAILURE_LOGGER,
            log_path=log_path,
            marker="mileage_logger_login_failure_file",
            formatter=logging.Formatter("%(message)s"),
            level=logging.INFO,
            propagate=False,
        )
    except OSError:
        logging.getLogger(__name__).error(
            "Could not configure login failure audit log path=%s",
            log_path,
            exc_info=True,
        )
        return None
    return log_path


def configure_logging(process_name: str) -> Path:
    settings = get_settings()
    level = log_level_value(settings)
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{process_name}.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    logging.getLogger("httpx").setLevel(max(level, logging.WARNING))
    logging.getLogger("httpcore").setLevel(max(level, logging.WARNING))
    logging.getLogger("requests").setLevel(max(level, logging.WARNING))
    logging.getLogger("urllib3").setLevel(max(level, logging.WARNING))

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
        level=level,
    )

    for handler in root_logger.handlers:
        if getattr(handler, "_mileage_logger_marker", "") == marker:
            handler.setLevel(level)
            handler.setFormatter(formatter)
            configure_login_failure_logging(settings)
            return log_path

    file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3)
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    file_handler._mileage_logger_marker = marker  # type: ignore[attr-defined]
    root_logger.addHandler(file_handler)

    configure_login_failure_logging(settings)
    return log_path
