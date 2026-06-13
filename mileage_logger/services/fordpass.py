import logging
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from threading import Lock
from typing import Any

from mileage_logger.config import Settings, get_settings

logger = logging.getLogger(__name__)
KM_PER_MILE = Decimal("1.609344")
_vehicle_lock = Lock()
_vehicle_cache: tuple[tuple[str, str, str], Any] | None = None


class FordPassOdometerError(RuntimeError):
    pass


@dataclass(frozen=True)
class OdometerReading:
    miles: Decimal


def _decimal_value(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _odometer_unit(value: dict[str, object], default_unit: str) -> str:
    for key in ("unit", "units", "uom"):
        unit = value.get(key)
        if unit:
            return str(unit).strip().casefold()
    return default_unit.strip().casefold()


def _convert_to_miles(value: Decimal, unit: str) -> Decimal:
    if unit in {"km", "kilometer", "kilometers", "kilometre", "kilometres"}:
        value = value / KM_PER_MILE
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _find_odometer_entry(value: object) -> object | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key.strip().casefold() == "odometer":
                return nested
        for nested in value.values():
            found = _find_odometer_entry(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_odometer_entry(nested)
            if found is not None:
                return found
    return None


def odometer_miles_from_status(
    status: dict[str, object],
    *,
    default_unit: str = "km",
) -> Decimal:
    entry = _find_odometer_entry(status)
    if entry is None:
        raise FordPassOdometerError("FordPass status did not include an odometer value")

    unit = default_unit
    if isinstance(entry, dict):
        unit = _odometer_unit(entry, default_unit)
        for key in ("value", "distance", "odometer"):
            candidate = _decimal_value(entry.get(key))
            if candidate is not None:
                return _convert_to_miles(candidate, unit)
        for nested in entry.values():
            candidate = _decimal_value(nested)
            if candidate is not None:
                return _convert_to_miles(candidate, unit)
    else:
        candidate = _decimal_value(entry)
        if candidate is not None:
            return _convert_to_miles(candidate, unit)

    raise FordPassOdometerError("FordPass odometer value was not numeric")


def _configured(settings: Settings) -> bool:
    return (
        settings.fordpass_enabled
        and bool(settings.fordpass_username)
        and bool(settings.fordpass_password)
        and bool(settings.fordpass_vin)
    )


def _vehicle(settings: Settings) -> Any:
    global _vehicle_cache

    try:
        from fordpass import Vehicle
    except ImportError as exc:
        raise FordPassOdometerError("The fordpass package is not installed") from exc

    cache_key = (settings.fordpass_username, settings.fordpass_password, settings.fordpass_vin)
    with _vehicle_lock:
        if _vehicle_cache is None or _vehicle_cache[0] != cache_key:
            _vehicle_cache = (
                cache_key,
                Vehicle(
                    settings.fordpass_username,
                    settings.fordpass_password,
                    settings.fordpass_vin,
                ),
            )
        return _vehicle_cache[1]


def current_odometer_miles(settings: Settings | None = None) -> Decimal | None:
    settings = settings or get_settings()
    if not _configured(settings):
        return None

    attempts = max(settings.fordpass_retry_attempts, 1)
    for attempt in range(1, attempts + 1):
        try:
            status = _vehicle(settings).status()
            return odometer_miles_from_status(
                status,
                default_unit=settings.fordpass_odometer_unit,
            )
        except Exception as exc:
            logger.warning(
                "FordPass odometer read failed attempt=%s attempts=%s error=%s",
                attempt,
                attempts,
                exc,
            )
            if attempt < attempts and settings.fordpass_retry_delay_seconds > 0:
                time.sleep(settings.fordpass_retry_delay_seconds)
    return None
