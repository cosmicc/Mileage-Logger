import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from mileage_logger.config import get_settings
from mileage_logger.models import OwnTracksLocation, Site
from mileage_logger.services.trip_processor import run_automatic_trip_processing

logger = logging.getLogger(__name__)


class OwnTracksError(ValueError):
    pass


class EmptyOwnTracksPayload(OwnTracksError):
    pass


class UnsupportedOwnTracksType(OwnTracksError):
    pass


@dataclass(frozen=True)
class TopicIdentity:
    topic: str | None
    user: str | None
    device: str | None


@dataclass(frozen=True)
class OwnTracksLocationMessage:
    payload: dict
    identity: TopicIdentity
    captured_at: datetime
    latitude: Decimal
    longitude: Decimal
    tracker_id: str | None
    accuracy_m: int | None
    battery_percent: int | None


@dataclass(frozen=True)
class OwnTracksProcessResult:
    location: OwnTracksLocation | None
    site: Site | None


def identity_from_topic(topic: str | None) -> TopicIdentity:
    if not topic:
        return TopicIdentity(topic=None, user=None, device=None)
    parts = topic.split("/")
    if len(parts) >= 3 and parts[0] == "owntracks":
        return TopicIdentity(topic=topic, user=parts[1] or None, device=parts[2] or None)
    return TopicIdentity(topic=topic, user=None, device=None)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise OwnTracksError(f"Expected integer-compatible value, got {value!r}") from exc


def _decode_payload(body: bytes) -> dict:
    if not body:
        raise EmptyOwnTracksPayload("OwnTracks sent an empty payload")

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise OwnTracksError("OwnTracks payload is not valid JSON") from exc

    if not isinstance(payload, dict):
        raise OwnTracksError("OwnTracks payload must be a JSON object")
    return payload


def _location_message_from_payload(
    payload: dict,
    *,
    topic: str | None = None,
    user: str | None = None,
    device: str | None = None,
) -> OwnTracksLocationMessage:
    payload_type = payload.get("_type")
    if payload_type not in {"location", "transition"}:
        raise UnsupportedOwnTracksType(f"Ignoring OwnTracks payload type {payload_type!r}")

    if "lat" not in payload or "lon" not in payload or "tst" not in payload:
        raise OwnTracksError("OwnTracks location payload requires lat, lon, and tst")

    payload_topic = payload.get("topic") or topic
    identity = identity_from_topic(str(payload_topic) if payload_topic else None)
    identity = TopicIdentity(
        topic=identity.topic,
        user=user or identity.user,
        device=device or identity.device,
    )

    captured_at = datetime.fromtimestamp(int(payload["tst"]), tz=UTC)

    return OwnTracksLocationMessage(
        payload=payload,
        identity=identity,
        captured_at=captured_at,
        latitude=Decimal(str(payload["lat"])),
        longitude=Decimal(str(payload["lon"])),
        tracker_id=str(payload["tid"]) if payload.get("tid") else None,
        accuracy_m=_optional_int(payload.get("acc")),
        battery_percent=_optional_int(payload.get("batt")),
    )


def parse_owntracks_location(
    body: bytes,
    *,
    topic: str | None = None,
    user: str | None = None,
    device: str | None = None,
) -> OwnTracksLocationMessage:
    return _location_message_from_payload(
        _decode_payload(body),
        topic=topic,
        user=user,
        device=device,
    )


def _first_region_name(payload: dict) -> str | None:
    description = payload.get("desc")
    if description:
        return str(description).strip() or None

    regions = payload.get("inregions")
    if isinstance(regions, list):
        for region in regions:
            name = str(region).strip()
            if name:
                return name
    return None


def _site_radius_from_payload(payload: dict) -> int:
    settings = get_settings()
    try:
        return int(payload.get("rad") or settings.owntracks_default_site_radius_m)
    except (TypeError, ValueError):
        return settings.owntracks_default_site_radius_m


def _payload_date(payload: dict) -> date:
    try:
        timestamp = int(payload.get("tst") or datetime.now(UTC).timestamp())
        return datetime.fromtimestamp(timestamp, tz=UTC).date()
    except (TypeError, ValueError, OSError):
        return datetime.now(UTC).date()


def _run_trip_processing(db: Session, payload: dict) -> None:
    touched_date = _payload_date(payload)
    finalize_completed_days = touched_date >= datetime.now(UTC).date()
    try:
        run_automatic_trip_processing(
            db,
            touched_date=touched_date,
            finalize_completed_days=finalize_completed_days,
        )
    except Exception:
        logger.exception("Automatic trip processing failed after OwnTracks payload")


def sync_site_from_owntracks_payload(
    db: Session,
    payload: dict,
    *,
    latitude: Decimal | None = None,
    longitude: Decimal | None = None,
) -> Site | None:
    settings = get_settings()
    if not settings.owntracks_auto_create_sites:
        return None

    name = _first_region_name(payload)
    if name is None:
        return None

    payload_latitude = payload.get("lat")
    payload_longitude = payload.get("lon")
    site_latitude = Decimal(str(payload_latitude)) if payload_latitude is not None else latitude
    site_longitude = Decimal(str(payload_longitude)) if payload_longitude is not None else longitude
    if site_latitude is None or site_longitude is None:
        return None

    site = db.scalar(select(Site).where(Site.name == name))
    if site is None:
        site = Site(
            name=name,
            latitude=site_latitude,
            longitude=site_longitude,
            radius_m=_site_radius_from_payload(payload),
            active=True,
        )
        db.add(site)
        return site

    if payload.get("_type") == "waypoint":
        site.latitude = site_latitude
        site.longitude = site_longitude
        site.radius_m = _site_radius_from_payload(payload)
        site.active = True
    return site


def store_owntracks_location(db: Session, message: OwnTracksLocationMessage) -> OwnTracksLocation:
    sync_site_from_owntracks_payload(
        db,
        message.payload,
        latitude=message.latitude,
        longitude=message.longitude,
    )
    location = OwnTracksLocation(
        user=message.identity.user,
        device=message.identity.device,
        topic=message.identity.topic,
        tracker_id=message.tracker_id,
        captured_at=message.captured_at,
        received_at=datetime.now(UTC),
        latitude=message.latitude,
        longitude=message.longitude,
        accuracy_m=message.accuracy_m,
        battery_percent=message.battery_percent,
        raw_payload=message.payload,
    )
    db.add(location)
    db.commit()
    db.refresh(location)
    return location


def process_owntracks_payload(
    db: Session,
    body: bytes,
    *,
    topic: str | None = None,
    user: str | None = None,
    device: str | None = None,
) -> OwnTracksProcessResult:
    payload = _decode_payload(body)
    payload_type = payload.get("_type")

    if payload_type == "waypoint":
        site = sync_site_from_owntracks_payload(db, payload)
        db.commit()
        if site is not None:
            db.refresh(site)
        _run_trip_processing(db, payload)
        return OwnTracksProcessResult(location=None, site=site)

    message = _location_message_from_payload(payload, topic=topic, user=user, device=device)
    location = store_owntracks_location(db, message)
    _run_trip_processing(db, payload)
    return OwnTracksProcessResult(location=location, site=None)
