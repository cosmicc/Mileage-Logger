import hashlib
import logging
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from threading import Lock
from typing import Any
from urllib.parse import quote

import httpx

from mileage_logger.config import Settings, get_settings

logger = logging.getLogger(__name__)
KM_PER_MILE = Decimal("1.609344")
AUTHENTICATION_STATUS_CODES = {401, 403}
ODOMETER_PRECISION = Decimal("0.001")
TOKEN_EXPIRY_SAFETY_SECONDS = 60
DEFAULT_TOKEN_EXPIRES_IN_SECONDS = 3600
_token_lock = Lock()
_token_cache: dict[tuple[str, str, str], tuple[str, float]] = {}
_auth_failure_lock = Lock()
_auth_failure_retry_after: dict[tuple[str, str, str], float] = {}


class SmartcarOdometerError(RuntimeError):
    """Raised when Smartcar does not return a usable odometer reading."""


class SmartcarAuthenticationError(SmartcarOdometerError):
    """Raised when Smartcar rejects the configured token or permission set."""


def _decimal_value(value: object) -> Decimal | None:
    """Return a Decimal for numeric API values while rejecting booleans and blanks."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _odometer_unit(value: dict[str, object], default_unit: str) -> str:
    """Read the unit attached to a Smartcar odometer value."""
    for key in ("unit", "units", "uom"):
        unit = value.get(key)
        if unit:
            return str(unit).strip().casefold()
    return default_unit.strip().casefold()


def _has_odometer_unit(value: dict[str, object]) -> bool:
    """Return true when a mapping carries an odometer unit field."""
    return any(value.get(key) for key in ("unit", "units", "uom"))


def _convert_to_miles(value: Decimal, unit: str) -> Decimal:
    """Convert Smartcar's odometer value to miles for report storage."""
    if unit in {"km", "kilometer", "kilometers", "kilometre", "kilometres"}:
        value = value / KM_PER_MILE
    return value.quantize(ODOMETER_PRECISION, rounding=ROUND_HALF_UP)


def _odometer_from_mapping(
    value: dict[str, object],
    *,
    default_unit: str,
) -> Decimal | None:
    """Parse the common Smartcar v2 and signal-style odometer response shapes."""
    unit = _odometer_unit(value, default_unit)
    for key in ("distance", "odometer"):
        candidate = _decimal_value(value.get(key))
        if candidate is not None:
            return _convert_to_miles(candidate, unit)

    if _has_odometer_unit(value):
        candidate = _decimal_value(value.get("value"))
        if candidate is not None:
            return _convert_to_miles(candidate, unit)

    for key in ("odometer", "data", "signal", "signals"):
        nested = value.get(key)
        if isinstance(nested, dict | list):
            candidate = _odometer_miles_from_value(nested, default_unit=default_unit)
            if candidate is not None:
                return candidate
    return None


def _odometer_miles_from_value(value: object, *, default_unit: str) -> Decimal | None:
    """Search a JSON value for a Smartcar odometer value."""
    if isinstance(value, dict):
        candidate = _odometer_from_mapping(value, default_unit=default_unit)
        if candidate is not None:
            return candidate
    elif isinstance(value, list):
        for nested in value:
            candidate = _odometer_miles_from_value(nested, default_unit=default_unit)
            if candidate is not None:
                return candidate
    return None


def odometer_miles_from_response(
    response_data: dict[str, object],
    *,
    default_unit: str = "km",
) -> Decimal:
    """Return Smartcar odometer miles from the provider response body."""
    odometer = _odometer_miles_from_value(response_data, default_unit=default_unit)
    if odometer is None:
        raise SmartcarOdometerError("Smartcar response did not include a numeric odometer value")
    return odometer


def _configured(settings: Settings) -> bool:
    """Require an explicit enable flag and a usable Smartcar credential before requests."""
    return settings.smartcar_enabled and (
        bool(settings.smartcar_access_token) or _client_credentials_configured(settings)
    )


def _client_credentials_configured(settings: Settings) -> bool:
    """Return true when Smartcar client credentials can request a short-lived token."""
    return bool(settings.smartcar_client_id) and bool(settings.smartcar_client_secret)


def _secret_digest(value: str) -> str:
    """Hash secret values before using them in cache keys."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _token_cache_key(settings: Settings) -> tuple[str, str, str]:
    """Build a non-secret token cache key for Smartcar client credentials."""
    return (
        settings.smartcar_client_id,
        _secret_digest(settings.smartcar_client_secret),
        settings.smartcar_scope.strip(),
    )


def _cache_key(settings: Settings) -> tuple[str, str, str]:
    """Build a non-secret cooldown key so tokens are not copied into provider state."""
    credential_key = (
        _secret_digest(settings.smartcar_access_token)
        if settings.smartcar_access_token
        else _secret_digest(f"{settings.smartcar_client_id}:{settings.smartcar_client_secret}")
    )
    return (
        credential_key,
        settings.smartcar_vehicle_id.strip(),
        settings.smartcar_api_base_url.rstrip("/"),
    )


def _auth_failure_remaining_seconds(settings: Settings) -> float:
    """Return the remaining authentication cooldown for this Smartcar token."""
    cache_key = _cache_key(settings)
    with _auth_failure_lock:
        retry_after = _auth_failure_retry_after.get(cache_key)
        if retry_after is None:
            return 0

        remaining = retry_after - time.monotonic()
        if remaining <= 0:
            _auth_failure_retry_after.pop(cache_key, None)
            return 0
        return remaining


def _remember_auth_failure(settings: Settings) -> None:
    """Pause automatic Smartcar reads after authentication or permission failures."""
    cooldown_seconds = settings.smartcar_auth_failure_cooldown_seconds
    if cooldown_seconds <= 0:
        return

    cache_key = _cache_key(settings)
    with _auth_failure_lock:
        _auth_failure_retry_after[cache_key] = time.monotonic() + cooldown_seconds


def _clear_token_cache(settings: Settings) -> None:
    """Remove a cached Smartcar token after the provider rejects it."""
    if settings.smartcar_access_token or not _client_credentials_configured(settings):
        return

    cache_key = _token_cache_key(settings)
    with _token_lock:
        _token_cache.pop(cache_key, None)


def _is_authentication_error(exc: Exception) -> bool:
    """Classify provider errors that should stop retries until the cooldown expires."""
    if isinstance(exc, SmartcarAuthenticationError):
        return True

    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code in AUTHENTICATION_STATUS_CODES:
        return True

    message = str(exc).casefold()
    return ("401" in message and "unauthorized" in message) or (
        "403" in message and "forbidden" in message
    )


def _api_url(settings: Settings, path: str) -> str:
    """Join the configured Smartcar API base URL to a relative endpoint path."""
    return f"{settings.smartcar_api_base_url.rstrip('/')}/{path.lstrip('/')}"


def _token_expires_at(value: object) -> float:
    """Convert Smartcar token lifetime seconds into a monotonic expiry timestamp."""
    expires_in = _decimal_value(value) or Decimal(DEFAULT_TOKEN_EXPIRES_IN_SECONDS)
    safe_expires_in = max(int(expires_in) - TOKEN_EXPIRY_SAFETY_SECONDS, 0)
    return time.monotonic() + safe_expires_in


def _fetch_access_token(settings: Settings) -> str:
    """Request a Smartcar access token with configured client credentials."""
    response = httpx.post(
        settings.smartcar_token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": settings.smartcar_client_id,
            "client_secret": settings.smartcar_client_secret,
            "scope": settings.smartcar_scope,
        },
        headers={"Accept": "application/json"},
        timeout=settings.smartcar_timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise SmartcarAuthenticationError("Smartcar token endpoint returned a non-object response")

    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        raise SmartcarAuthenticationError("Smartcar token endpoint did not return an access token")

    cache_key = _token_cache_key(settings)
    expires_at = _token_expires_at(data.get("expires_in"))
    with _token_lock:
        _token_cache[cache_key] = (access_token, expires_at)
    logger.info(
        "Fetched Smartcar access token expires_in=%s scope_configured=%s",
        data.get("expires_in", DEFAULT_TOKEN_EXPIRES_IN_SECONDS),
        bool(settings.smartcar_scope),
    )
    return access_token


def _bearer_token(settings: Settings) -> str:
    """Return a static Smartcar token or a cached client-credential token."""
    static_token = settings.smartcar_access_token.strip()
    if static_token:
        return static_token

    if not _client_credentials_configured(settings):
        raise SmartcarAuthenticationError(
            "Smartcar access token or client credentials are required"
        )

    cache_key = _token_cache_key(settings)
    with _token_lock:
        cached = _token_cache.get(cache_key)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]

    return _fetch_access_token(settings)


def _auth_headers(settings: Settings) -> dict[str, str]:
    """Build Smartcar API headers without logging or exposing the bearer token."""
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {_bearer_token(settings)}",
    }


def _get_json(
    settings: Settings,
    path: str,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, object]:
    """Make a Smartcar GET request and return a JSON object response."""
    response = httpx.get(
        _api_url(settings, path),
        headers=_auth_headers(settings),
        params=params,
        timeout=settings.smartcar_timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise SmartcarOdometerError("Smartcar returned a non-object JSON response")
    return data


def _vehicle_id(settings: Settings) -> str:
    """Return the configured Smartcar vehicle ID or auto-detect the first connected vehicle."""
    configured_vehicle_id = settings.smartcar_vehicle_id.strip()
    if configured_vehicle_id:
        return configured_vehicle_id

    data = _get_json(settings, "vehicles", params={"limit": 1})
    vehicles = data.get("vehicles")
    if not isinstance(vehicles, list) or not vehicles:
        raise SmartcarOdometerError("Smartcar returned no connected vehicles")

    vehicle_id = str(vehicles[0]).strip()
    if not vehicle_id:
        raise SmartcarOdometerError("Smartcar returned a blank vehicle ID")
    return vehicle_id


def _read_odometer_once(settings: Settings) -> Decimal:
    """Read one Smartcar odometer value and convert it to miles."""
    vehicle_id = _vehicle_id(settings)
    encoded_vehicle_id = quote(vehicle_id, safe="")
    data = _get_json(settings, f"vehicles/{encoded_vehicle_id}/odometer")
    return odometer_miles_from_response(data, default_unit=settings.smartcar_odometer_unit)


def current_odometer_miles(
    settings: Settings | None = None,
    *,
    force: bool = False,
    raise_on_auth_error: bool = False,
) -> Decimal | None:
    """Read the current Smartcar odometer in miles when the integration is configured."""
    settings = settings or get_settings()
    if not _configured(settings):
        logger.debug(
            "Smartcar odometer skipped enabled=%s token_configured=%s "
            "client_credentials_configured=%s vehicle_id_configured=%s",
            settings.smartcar_enabled,
            bool(settings.smartcar_access_token),
            _client_credentials_configured(settings),
            bool(settings.smartcar_vehicle_id),
        )
        return None

    if not force:
        remaining_seconds = _auth_failure_remaining_seconds(settings)
        if remaining_seconds > 0:
            logger.debug(
                "Smartcar odometer skipped after recent authentication failure "
                "retry_after_seconds=%.0f",
                remaining_seconds,
            )
            return None

    attempts = max(settings.smartcar_retry_attempts, 1)
    for attempt in range(1, attempts + 1):
        try:
            logger.debug("Reading Smartcar odometer attempt=%s attempts=%s", attempt, attempts)
            odometer = _read_odometer_once(settings)
            logger.info(
                "Read Smartcar odometer miles=%s attempt=%s vehicle_id_configured=%s",
                odometer,
                attempt,
                bool(settings.smartcar_vehicle_id),
            )
            return odometer
        except Exception as exc:
            if _is_authentication_error(exc):
                _clear_token_cache(settings)
                _remember_auth_failure(settings)
                logger.warning(
                    "Smartcar odometer authentication failed attempt=%s; automatic reads are "
                    "paused for %s seconds. Check the Smartcar access token, read_odometer "
                    "permission, and vehicle connection.",
                    attempt,
                    settings.smartcar_auth_failure_cooldown_seconds,
                )
                if raise_on_auth_error:
                    raise SmartcarAuthenticationError(
                        "Smartcar authentication failed. Check the Smartcar access token, "
                        "read_odometer permission, and vehicle connection."
                    ) from exc
                return None

            logger.warning(
                "Smartcar odometer read failed attempt=%s attempts=%s error=%s",
                attempt,
                attempts,
                exc,
            )
            if attempt < attempts and settings.smartcar_retry_delay_seconds > 0:
                time.sleep(settings.smartcar_retry_delay_seconds)
    return None
