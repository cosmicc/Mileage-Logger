import json
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from mileage_logger.config import Settings
from mileage_logger.models import Base, OwnTracksLocation
from mileage_logger.services.owntracks_buffer import (
    OwnTracksPayloadBuffer,
    ingest_or_buffer_owntracks_payload,
    replay_owntracks_buffer_once,
)
from mileage_logger.services.timezone import datetime_to_utc


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _location_payload(captured_at: datetime, latitude: str) -> bytes:
    return json.dumps(
        {
            "_type": "location",
            "lat": latitude,
            "lon": "-83.0458",
            "tst": int(captured_at.timestamp()),
            "topic": "owntracks/ian/phone",
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _offline_session_factory() -> Session:
    raise OperationalError("SELECT 1", {}, Exception("database offline"))


def test_owntracks_buffer_persists_payloads_and_replays_fifo(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite://",
        owntracks_buffer_path=str(tmp_path / "owntracks-buffer.sqlite3"),
        database_run_migrations_on_reconnect=False,
    )
    session_factory = _session_factory()
    first_payload = _location_payload(datetime(2026, 7, 1, 12, 0, tzinfo=UTC), "42.3314")
    second_payload = _location_payload(datetime(2026, 7, 1, 12, 5, tzinfo=UTC), "42.3320")

    first_outcome = ingest_or_buffer_owntracks_payload(
        first_payload,
        settings=settings,
        session_factory=_offline_session_factory,
    )
    second_outcome = ingest_or_buffer_owntracks_payload(
        second_payload,
        settings=settings,
        session_factory=session_factory,
    )

    assert first_outcome.buffered is True
    assert first_outcome.reason == "database_unavailable"
    assert second_outcome.buffered is True
    assert second_outcome.reason == "buffer_not_empty"
    assert OwnTracksPayloadBuffer(settings.owntracks_buffer_path).count() == 2

    result = replay_owntracks_buffer_once(settings, session_factory=session_factory)

    assert result.processed_count == 2
    assert result.remaining_count == 0
    with session_factory() as db:
        locations = list(db.scalars(select(OwnTracksLocation).order_by(OwnTracksLocation.id.asc())))
    assert [location.latitude for location in locations] == [
        Decimal("42.3314"),
        Decimal("42.3320"),
    ]
    assert [datetime_to_utc(location.captured_at) for location in locations] == [
        datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        datetime(2026, 7, 1, 12, 5, tzinfo=UTC),
    ]


def test_owntracks_buffer_replay_stops_without_deleting_when_database_is_offline(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite://",
        owntracks_buffer_path=str(tmp_path / "owntracks-buffer.sqlite3"),
        database_run_migrations_on_reconnect=False,
    )
    buffer = OwnTracksPayloadBuffer(settings.owntracks_buffer_path)
    buffer.enqueue(
        _location_payload(datetime(2026, 7, 1, 12, 0, tzinfo=UTC), "42.3314"),
        source="test",
    )

    result = replay_owntracks_buffer_once(settings, session_factory=_offline_session_factory)

    assert result.processed_count == 0
    assert result.remaining_count == 1
    assert "database offline" in result.error
    assert buffer.count() == 1
    assert "database offline" in (buffer.stats().last_error or "")
