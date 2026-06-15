import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from threading import Lock
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mileage_logger.config import Settings, get_settings
from mileage_logger.database import SessionLocal
from mileage_logger.models import SmartcarWebhookEvent, SmartcarWebhookSignal

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
SMARTCAR_SIGNATURE_PREFIX = "sha256="
SMARTCAR_VERIFY_EVENT = "VERIFY"
SMARTCAR_ODOMETER_SIGNAL_CODE = "odometer-traveleddistance"
SMARTCAR_FUEL_SIGNAL_CODE = "internalcombustionengine-fuellevel"
SMARTCAR_LOCK_SIGNAL_CODE = "closure-islocked"
SMARTCAR_ONLINE_SIGNAL_CODE = "connectivitystatus-isonline"
SMARTCAR_NICKNAME_SIGNAL_CODE = "vehicleidentification-nickname"
SMARTCAR_VIN_SIGNAL_CODE = "vehicleidentification-vin"
SMARTCAR_FIRMWARE_SIGNAL_CODE = "connectivitysoftware-currentfirmwareversion"
MANUAL_ODOMETER_EVENT_TYPE = "MANUAL_ODOMETER"
MANUAL_ODOMETER_SIGNAL_CODE = "manual-odometer"


class SmartcarOdometerError(RuntimeError):
    """Raised when Smartcar does not return a usable odometer reading."""


class SmartcarAuthenticationError(SmartcarOdometerError):
    """Raised when Smartcar rejects the configured token or permission set."""


class SmartcarWebhookError(ValueError):
    """Raised when a Smartcar webhook cannot be safely accepted."""


@dataclass(frozen=True)
class SmartcarWebhookProcessResult:
    """Result from storing or deduplicating a Smartcar webhook delivery."""

    event: SmartcarWebhookEvent
    created: bool


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


def hash_webhook_challenge(management_token: str, challenge: str) -> str:
    """Return Smartcar's required hex HMAC for a webhook VERIFY challenge."""
    cleaned_token = management_token.strip()
    if not cleaned_token:
        raise SmartcarWebhookError("Smartcar management token is not configured")
    if not challenge:
        raise SmartcarWebhookError("Smartcar VERIFY payload did not include a challenge")
    return hmac.new(
        cleaned_token.encode("utf-8"),
        challenge.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _normalized_signature(signature: str | None) -> str:
    """Normalize Smartcar signature header values before constant-time comparison."""
    cleaned_signature = str(signature or "").strip()
    if cleaned_signature.casefold().startswith(SMARTCAR_SIGNATURE_PREFIX):
        return cleaned_signature[len(SMARTCAR_SIGNATURE_PREFIX) :].strip().casefold()
    return cleaned_signature.casefold()


def verify_webhook_signature(
    raw_body: bytes,
    signature: str | None,
    management_token: str,
) -> bool:
    """Verify Smartcar's SC-Signature HMAC against the exact raw request body."""
    cleaned_token = management_token.strip()
    normalized_signature = _normalized_signature(signature)
    if not cleaned_token or not normalized_signature:
        return False

    expected_signature = hmac.new(
        cleaned_token.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected_signature, normalized_signature)


def decode_webhook_payload(raw_body: bytes) -> dict[str, object]:
    """Decode a Smartcar webhook body as a JSON object with bounded assumptions."""
    if not raw_body:
        raise SmartcarWebhookError("Smartcar webhook body is empty")
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise SmartcarWebhookError("Smartcar webhook body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise SmartcarWebhookError("Smartcar webhook body must be a JSON object")
    return payload


def _text_value(value: object) -> str | None:
    """Return a trimmed string for JSON scalar values."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _mapping_value(value: object) -> dict[str, object]:
    """Return a JSON object value or an empty mapping for malformed nested fields."""
    if isinstance(value, dict):
        return value
    return {}


def _list_value(value: object) -> list[object]:
    """Return a JSON array value or an empty list for malformed nested fields."""
    if isinstance(value, list):
        return value
    return []


def _smartcar_timestamp(value: object) -> datetime | None:
    """Convert Smartcar millisecond or ISO timestamps into timezone-aware UTC datetimes."""
    if value in (None, ""):
        return None

    if isinstance(value, str):
        cleaned_value = value.strip()
        if not cleaned_value:
            return None
        if not cleaned_value.replace(".", "", 1).isdigit():
            try:
                return datetime.fromisoformat(cleaned_value.replace("Z", "+00:00")).astimezone(
                    UTC
                )
            except ValueError:
                return None
        value = cleaned_value

    timestamp_value = _decimal_value(value)
    if timestamp_value is None or timestamp_value <= 0:
        return None

    timestamp_seconds = (
        timestamp_value / Decimal("1000")
        if timestamp_value > Decimal("10000000000")
        else timestamp_value
    )
    try:
        return datetime.fromtimestamp(float(timestamp_seconds), tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _signal_status(signal: dict[str, object]) -> str | None:
    """Return the status.value field from a Smartcar signal."""
    status = _mapping_value(signal.get("status"))
    return _text_value(status.get("value"))


def _signal_succeeded(signal: dict[str, object]) -> bool:
    """Return true when a Smartcar signal reports SUCCESS."""
    return (_signal_status(signal) or "").casefold() == "success"


def _signal_body(signal: dict[str, object]) -> dict[str, object]:
    """Return the body object from a Smartcar signal."""
    return _mapping_value(signal.get("body"))


def _signal_meta(signal: dict[str, object]) -> dict[str, object]:
    """Return the meta object from a Smartcar signal."""
    return _mapping_value(signal.get("meta"))


def _signal_value(signal: dict[str, object]) -> object | None:
    """Return a successful signal's body.value field."""
    if not _signal_succeeded(signal):
        return None
    return _signal_body(signal).get("value")


def _signal_unit(signal: dict[str, object]) -> str | None:
    """Return a signal unit from body.unit when Smartcar includes one."""
    return _text_value(_signal_body(signal).get("unit"))


def _signal_recorded_at(signal: dict[str, object]) -> datetime | None:
    """Return the best available timestamp for a Smartcar signal value."""
    meta = _signal_meta(signal)
    return _smartcar_timestamp(meta.get("oemUpdatedAt")) or _smartcar_timestamp(
        meta.get("retrievedAt")
    )


def _signal_key(signal: dict[str, object]) -> tuple[str, str]:
    """Return lowercase lookup keys for a Smartcar signal code and display name."""
    return (
        (_text_value(signal.get("code")) or "").casefold(),
        (_text_value(signal.get("name")) or "").casefold(),
    )


def _signals_from_payload(payload: dict[str, object]) -> list[dict[str, object]]:
    """Return Smartcar signal objects from a webhook payload."""
    data = _mapping_value(payload.get("data"))
    signals: list[dict[str, object]] = []
    for signal in _list_value(data.get("signals")):
        if isinstance(signal, dict):
            signals.append(signal)
    return signals


def _signal_lookup(signals: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    """Index Smartcar signals by code and by display name for summary extraction."""
    lookup: dict[str, dict[str, object]] = {}
    for signal in signals:
        code, name = _signal_key(signal)
        if code:
            lookup[code] = signal
        if name:
            lookup[name] = signal
    return lookup


def _signal_by_key(
    lookup: dict[str, dict[str, object]],
    code: str,
    name: str,
) -> dict[str, object] | None:
    """Return a signal by stable Smartcar code first, then by display name."""
    return lookup.get(code.casefold()) or lookup.get(name.casefold())


def _decimal_signal_value(signal: dict[str, object] | None) -> Decimal | None:
    """Return a successful Smartcar signal value as Decimal."""
    if signal is None:
        return None
    return _decimal_value(_signal_value(signal))


def _boolean_signal_value(signal: dict[str, object] | None) -> bool | None:
    """Return a successful Smartcar signal value as bool."""
    if signal is None:
        return None
    value = _signal_value(signal)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned_value = value.strip().casefold()
        if cleaned_value in {"true", "1", "yes"}:
            return True
        if cleaned_value in {"false", "0", "no"}:
            return False
    return None


def _odometer_from_signal(
    signal: dict[str, object] | None,
    *,
    default_unit: str,
) -> tuple[Decimal | None, Decimal | None, str | None, datetime | None]:
    """Return converted miles, raw value, unit, and timestamp from an odometer signal."""
    raw_value = _decimal_signal_value(signal)
    if raw_value is None:
        return None, None, None, None

    signal_unit = _signal_unit(signal) or default_unit
    odometer_miles = _convert_to_miles(raw_value, signal_unit)
    return odometer_miles, raw_value, signal_unit, _signal_recorded_at(signal)


def _event_identifiers(
    payload: dict[str, object],
) -> tuple[str, str | None, dict[str, object]]:
    """Return required event id, optional delivery id, and metadata from a webhook payload."""
    event_id = _text_value(payload.get("eventId"))
    if event_id is None:
        raise SmartcarWebhookError("Smartcar webhook payload missing eventId")

    metadata = _mapping_value(payload.get("meta"))
    delivery_id = _text_value(metadata.get("deliveryId"))
    return event_id, delivery_id, metadata


def _existing_webhook_event(
    db: Session,
    *,
    event_id: str,
    delivery_id: str | None,
) -> SmartcarWebhookEvent | None:
    """Return an already-stored Smartcar webhook event for idempotent retries."""
    existing_event = db.scalar(
        select(SmartcarWebhookEvent).where(SmartcarWebhookEvent.event_id == event_id)
    )
    if existing_event is not None:
        return existing_event
    if delivery_id is None:
        return None
    return db.scalar(
        select(SmartcarWebhookEvent).where(SmartcarWebhookEvent.delivery_id == delivery_id)
    )


def _build_signal_row(signal: dict[str, object]) -> SmartcarWebhookSignal:
    """Build a database row for one Smartcar signal object."""
    body = _signal_body(signal)
    meta = _signal_meta(signal)
    return SmartcarWebhookSignal(
        code=_text_value(signal.get("code")),
        name=_text_value(signal.get("name")),
        group=_text_value(signal.get("group")),
        status=_signal_status(signal),
        value=body.get("value"),
        unit=_text_value(body.get("unit")),
        oem_updated_at=_smartcar_timestamp(meta.get("oemUpdatedAt")),
        retrieved_at=_smartcar_timestamp(meta.get("retrievedAt")),
        body=body,
        meta=meta,
        raw_signal=signal,
    )


def store_webhook_payload(
    db: Session,
    payload: dict[str, object],
    *,
    settings: Settings | None = None,
) -> SmartcarWebhookProcessResult:
    """Store a verified Smartcar webhook payload and all included signal values."""
    settings = settings or get_settings()
    event_id, delivery_id, metadata = _event_identifiers(payload)
    existing_event = _existing_webhook_event(db, event_id=event_id, delivery_id=delivery_id)
    if existing_event is not None:
        logger.info(
            "Ignored duplicate Smartcar webhook event_id=%s delivery_id=%s database_id=%s",
            event_id,
            delivery_id or "",
            existing_event.id,
        )
        return SmartcarWebhookProcessResult(event=existing_event, created=False)

    data = _mapping_value(payload.get("data"))
    user = _mapping_value(data.get("user"))
    vehicle = _mapping_value(data.get("vehicle"))
    signals = _signals_from_payload(payload)
    lookup = _signal_lookup(signals)

    odometer_signal = _signal_by_key(lookup, SMARTCAR_ODOMETER_SIGNAL_CODE, "TraveledDistance")
    fuel_signal = _signal_by_key(lookup, SMARTCAR_FUEL_SIGNAL_CODE, "FuelLevel")
    lock_signal = _signal_by_key(lookup, SMARTCAR_LOCK_SIGNAL_CODE, "IsLocked")
    online_signal = _signal_by_key(lookup, SMARTCAR_ONLINE_SIGNAL_CODE, "IsOnline")
    nickname_signal = _signal_by_key(lookup, SMARTCAR_NICKNAME_SIGNAL_CODE, "Nickname")
    vin_signal = _signal_by_key(lookup, SMARTCAR_VIN_SIGNAL_CODE, "VIN")
    firmware_signal = _signal_by_key(
        lookup,
        SMARTCAR_FIRMWARE_SIGNAL_CODE,
        "CurrentFirmwareVersion",
    )
    odometer_miles, odometer_raw_value, odometer_unit, odometer_recorded_at = (
        _odometer_from_signal(odometer_signal, default_unit=settings.smartcar_odometer_unit)
    )
    vehicle_year_value = _decimal_value(vehicle.get("year"))

    event = SmartcarWebhookEvent(
        event_id=event_id,
        event_type=_text_value(payload.get("eventType")) or "UNKNOWN",
        user_id=_text_value(user.get("id")),
        vehicle_id=_text_value(vehicle.get("id")),
        vehicle_make=_text_value(vehicle.get("make")),
        vehicle_model=_text_value(vehicle.get("model")),
        vehicle_year=int(vehicle_year_value) if vehicle_year_value is not None else None,
        vehicle_mode=_text_value(vehicle.get("mode")),
        vehicle_powertrain_type=_text_value(vehicle.get("powertrainType")),
        webhook_id=_text_value(metadata.get("webhookId")),
        webhook_name=_text_value(metadata.get("webhookName")),
        delivery_id=delivery_id,
        delivered_at=_smartcar_timestamp(metadata.get("deliveredAt")),
        received_at=datetime.now(UTC),
        odometer_miles=odometer_miles,
        odometer_raw_value=odometer_raw_value,
        odometer_unit=odometer_unit,
        odometer_recorded_at=odometer_recorded_at,
        fuel_percent=_decimal_signal_value(fuel_signal),
        fuel_unit=_signal_unit(fuel_signal),
        is_locked=_boolean_signal_value(lock_signal),
        is_online=_boolean_signal_value(online_signal),
        nickname=_text_value(_signal_value(nickname_signal)) if nickname_signal else None,
        vin=_text_value(_signal_value(vin_signal)) if vin_signal else None,
        firmware_version=(
            _text_value(_signal_value(firmware_signal)) if firmware_signal else None
        ),
        triggers=_list_value(payload.get("triggers")),
        raw_payload=payload,
    )
    event.signal_rows = [_build_signal_row(signal) for signal in signals]
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        duplicate_event = _existing_webhook_event(
            db,
            event_id=event_id,
            delivery_id=delivery_id,
        )
        if duplicate_event is None:
            raise
        return SmartcarWebhookProcessResult(event=duplicate_event, created=False)

    db.refresh(event)
    logger.info(
        "Stored Smartcar webhook event id=%s event_id=%s type=%s vehicle_id=%s "
        "delivery_id=%s odometer_miles=%s signal_count=%s",
        event.id,
        event.event_id,
        event.event_type,
        event.vehicle_id or "",
        event.delivery_id or "",
        event.odometer_miles if event.odometer_miles is not None else "",
        len(event.signal_rows),
    )
    return SmartcarWebhookProcessResult(event=event, created=True)


def create_manual_odometer_event(
    db: Session,
    odometer_miles: Decimal,
    *,
    recorded_at: datetime | None = None,
) -> SmartcarWebhookEvent:
    """Store a user-entered odometer reading in the normal odometer event stream."""
    recorded_dt = recorded_at or datetime.now(UTC)
    received_dt = datetime.now(UTC)
    odometer_value = Decimal(str(odometer_miles)).quantize(
        ODOMETER_PRECISION,
        rounding=ROUND_HALF_UP,
    )
    event_id = f"manual-odometer-{received_dt:%Y%m%dT%H%M%S%fZ}-{uuid4().hex[:12]}"
    payload = {
        "eventId": event_id,
        "eventType": MANUAL_ODOMETER_EVENT_TYPE,
        "source": "manual",
        "odometer": {
            "value": str(odometer_value),
            "unit": "mi",
            "recordedAt": recorded_dt.isoformat(),
        },
    }
    event = SmartcarWebhookEvent(
        event_id=event_id,
        event_type=MANUAL_ODOMETER_EVENT_TYPE,
        delivered_at=recorded_dt,
        received_at=received_dt,
        odometer_miles=odometer_value,
        odometer_raw_value=odometer_value,
        odometer_unit="mi",
        odometer_recorded_at=recorded_dt,
        raw_payload=payload,
    )
    event.signal_rows = [
        SmartcarWebhookSignal(
            code=MANUAL_ODOMETER_SIGNAL_CODE,
            name="ManualOdometer",
            group="Odometer",
            status="SUCCESS",
            value=str(odometer_value),
            unit="mi",
            retrieved_at=recorded_dt,
            body={"value": str(odometer_value), "unit": "mi"},
            meta={"retrievedAt": recorded_dt.isoformat(), "source": "manual"},
            raw_signal=payload,
        )
    ]
    db.add(event)
    db.commit()
    db.refresh(event)
    logger.info(
        "Stored manual odometer event id=%s event_id=%s odometer_miles=%s recorded_at=%s",
        event.id,
        event.event_id,
        event.odometer_miles,
        recorded_dt.isoformat(),
    )
    return event


def odometer_event_source(event: SmartcarWebhookEvent) -> str:
    """Return the display/source label for a stored odometer event."""
    if event.event_type == MANUAL_ODOMETER_EVENT_TYPE:
        return "manual"
    return "smartcar"


def _latest_webhook_odometer_query(at: datetime | None = None) -> Any:
    """Build the query that returns the freshest stored Smartcar odometer event."""
    recorded_timestamp = func.coalesce(
        SmartcarWebhookEvent.odometer_recorded_at,
        SmartcarWebhookEvent.delivered_at,
        SmartcarWebhookEvent.received_at,
    )
    query = (
        select(SmartcarWebhookEvent)
        .where(SmartcarWebhookEvent.odometer_miles.is_not(None))
        .order_by(
            SmartcarWebhookEvent.odometer_recorded_at.desc().nulls_last(),
            SmartcarWebhookEvent.delivered_at.desc().nulls_last(),
            SmartcarWebhookEvent.received_at.desc(),
            SmartcarWebhookEvent.id.desc(),
        )
        .limit(1)
    )
    if at is not None:
        query = query.where(recorded_timestamp <= at)
    return query


def latest_webhook_odometer_event(
    db: Session,
    at: datetime | None = None,
) -> SmartcarWebhookEvent | None:
    """Return the newest stored Smartcar odometer webhook event."""
    return db.scalar(_latest_webhook_odometer_query(at=at))


def latest_webhook_odometer_miles(
    db: Session | None = None,
    at: datetime | None = None,
) -> Decimal | None:
    """Return the newest stored Smartcar webhook odometer in miles."""
    if db is not None:
        event = latest_webhook_odometer_event(db, at=at)
        return event.odometer_miles if event is not None else None

    with SessionLocal() as session:
        event = latest_webhook_odometer_event(session, at=at)
        return event.odometer_miles if event is not None else None


def _api_configured(settings: Settings) -> bool:
    """Require explicit Smartcar enablement and API credentials before API requests."""
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
    if settings.smartcar_enabled:
        try:
            webhook_odometer = latest_webhook_odometer_miles()
        except Exception:
            logger.exception("Stored Smartcar webhook odometer lookup failed")
            webhook_odometer = None
        if webhook_odometer is not None:
            logger.debug("Using stored Smartcar webhook odometer miles=%s", webhook_odometer)
            return webhook_odometer

    if not force and not settings.smartcar_api_polling_enabled:
        logger.debug(
            "Smartcar API odometer polling skipped enabled=%s api_polling_enabled=%s",
            settings.smartcar_enabled,
            settings.smartcar_api_polling_enabled,
        )
        return None

    if not _api_configured(settings):
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
