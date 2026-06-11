import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from mileage_logger.config import get_settings


def configure_logging(process_name: str) -> Path:
    settings = get_settings()
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{process_name}.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    marker = f"mileage_logger_{process_name}_file"
    for handler in root_logger.handlers:
        if getattr(handler, "_mileage_logger_marker", "") == marker:
            return log_path

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3)
    file_handler.setFormatter(formatter)
    file_handler._mileage_logger_marker = marker  # type: ignore[attr-defined]
    root_logger.addHandler(file_handler)

    return log_path
