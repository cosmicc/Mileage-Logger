from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from mileage_logger.app import app
from mileage_logger.database import get_db
from mileage_logger.models import Base, OwnTracksLocation, Site
from mileage_logger.services.diagnostics import (
    paginated_owntracks_entries,
    recent_owntracks_entries,
)
from mileage_logger.services.gas_prices import AaaMichiganGasPriceProvider


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _test_client_session() -> tuple[TestClient, sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), session_factory


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


def test_waypoints_page_paginates_twenty_per_page() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            db.add_all(
                Site(
                    name=f"Waypoint {index:02d}",
                    latitude=Decimal("42.3314000"),
                    longitude=Decimal("-83.0458000"),
                    radius_m=150,
                )
                for index in range(45)
            )
            db.commit()

        response = client.get("/waypoints?page=2")

        assert response.status_code == 200
        assert "Showing 21-40" in response.text
        assert "of 45" in response.text
        assert "Page 2 of 3" in response.text
        assert "/waypoints?page=1" in response.text
        assert "/waypoints?page=3" in response.text
        assert "Waypoint 20" in response.text
        assert "Waypoint 39" in response.text
        assert "Waypoint 40" not in response.text
    finally:
        app.dependency_overrides.clear()


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
