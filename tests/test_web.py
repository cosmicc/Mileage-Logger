from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from mileage_logger.models import Base, OwnTracksLocation
from mileage_logger.services.diagnostics import recent_owntracks_entries


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _location(
    captured_at: datetime,
    received_at: datetime,
    raw_payload: dict,
) -> OwnTracksLocation:
    return OwnTracksLocation(
        captured_at=captured_at,
        received_at=received_at,
        latitude=Decimal("42.3314000"),
        longitude=Decimal("-83.0458000"),
        raw_payload=raw_payload,
    )


def test_recent_owntracks_entries_include_travel_entries() -> None:
    db = _session()
    now = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)
    db.add_all(
        [
            _location(now, now, {"_type": "transition", "event": "enter", "desc": "Client A"}),
            _location(
                now + timedelta(minutes=1),
                now + timedelta(minutes=1),
                {"_type": "location"},
            ),
            _location(
                now + timedelta(minutes=2),
                now + timedelta(minutes=2),
                {"_type": "waypoint", "desc": "Client B"},
            ),
        ]
    )
    db.commit()

    events = recent_owntracks_entries(db)

    assert [event.raw_payload["_type"] for event in events] == [
        "waypoint",
        "location",
        "transition",
    ]
