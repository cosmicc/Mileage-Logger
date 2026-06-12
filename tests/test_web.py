from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from mileage_logger.models import Base, OwnTracksLocation
from mileage_logger.services.diagnostics import (
    paginated_owntracks_entries,
    recent_owntracks_entries,
)
from mileage_logger.services.gas_prices import AaaMichiganGasPriceProvider


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


def test_paginated_owntracks_entries_loads_requested_page() -> None:
    db = _session()
    start = datetime(2026, 6, 11, 8, 0, tzinfo=UTC)
    db.add_all(
        [
            _location(
                start + timedelta(minutes=index),
                start + timedelta(minutes=index),
                {"_type": "location", "index": index},
            )
            for index in range(45)
        ]
    )
    db.commit()

    page = paginated_owntracks_entries(db, page=2, page_size=20)

    assert page.total == 45
    assert page.page == 2
    assert page.total_pages == 3
    assert page.first_item == 21
    assert page.last_item == 40
    assert page.has_previous is True
    assert page.has_next is True
    assert len(page.entries) == 20
    assert page.entries[0].raw_payload["index"] == 24
    assert page.entries[-1].raw_payload["index"] == 5


def test_aaa_gas_provider_uses_local_observed_date(monkeypatch) -> None:
    class FakeResponse:
        text = "Current Avg.</td><td>$3.250</td>"

        def raise_for_status(self) -> None:
            return None

    now = datetime(2026, 6, 11, 21, 30, tzinfo=UTC)
    monkeypatch.setattr(
        "mileage_logger.services.gas_prices.httpx.get",
        lambda *_, **__: FakeResponse(),
    )
    monkeypatch.setattr("mileage_logger.services.gas_prices.local_today", lambda: now.date())

    reading = AaaMichiganGasPriceProvider().current_regular_price("MI")

    assert reading.observed_on == now.date()
