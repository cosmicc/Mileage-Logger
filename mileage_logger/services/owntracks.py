import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from mileage_logger.models import OwnTracksLocation


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


def parse_owntracks_location(
    body: bytes,
    *,
    topic: str | None = None,
    user: str | None = None,
    device: str | None = None,
) -> OwnTracksLocationMessage:
    if not body:
        raise EmptyOwnTracksPayload("OwnTracks sent an empty payload")

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise OwnTracksError("OwnTracks payload is not valid JSON") from exc

    if not isinstance(payload, dict):
        raise OwnTracksError("OwnTracks payload must be a JSON object")

    payload_type = payload.get("_type")
    if payload_type != "location":
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


def store_owntracks_location(db: Session, message: OwnTracksLocationMessage) -> OwnTracksLocation:
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
