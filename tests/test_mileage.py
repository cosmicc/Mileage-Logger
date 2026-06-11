from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from mileage_logger.models import Base, OwnTracksLocation, Site, Trip
from mileage_logger.services.mileage import generate_trips, haversine_miles


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _location(
    captured_at: datetime,
    latitude: str,
    longitude: str,
    raw_payload: dict | None = None,
) -> OwnTracksLocation:
    return OwnTracksLocation(
        captured_at=captured_at,
        received_at=captured_at,
        latitude=Decimal(latitude),
        longitude=Decimal(longitude),
        raw_payload=raw_payload or {"_type": "location"},
    )


def test_haversine_miles_returns_expected_short_distance() -> None:
    miles = haversine_miles(
        Decimal("42.3314"),
        Decimal("-83.0458"),
        Decimal("42.7325"),
        Decimal("-84.5555"),
    )

    assert Decimal("81.00") < miles < Decimal("83.00")


def test_generate_trips_between_stops_that_last_at_least_ten_minutes() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    client_a = Site(
        name="Client A",
        latitude=Decimal("42.3314"),
        longitude=Decimal("-83.0458"),
        radius_m=120,
    )
    client_b = Site(
        name="Client B",
        latitude=Decimal("42.3440"),
        longitude=Decimal("-83.0600"),
        radius_m=120,
    )
    db.add_all(
        [
            client_a,
            client_b,
            _location(day, "42.3314", "-83.0458"),
            _location(day + timedelta(minutes=12), "42.3315", "-83.0459"),
            _location(day + timedelta(minutes=18), "42.3370", "-83.0520"),
            _location(day + timedelta(minutes=25), "42.3440", "-83.0600"),
            _location(day + timedelta(minutes=38), "42.3441", "-83.0601"),
        ]
    )
    db.commit()

    trips = generate_trips(db, day.date(), day.date())

    assert len(trips) == 1
    assert trips[0].origin_site_id == client_a.id
    assert trips[0].destination_site_id == client_b.id
    assert trips[0].started_at == (day + timedelta(minutes=12)).replace(tzinfo=None)
    assert trips[0].ended_at == (day + timedelta(minutes=25)).replace(tzinfo=None)


def test_generate_trips_ignores_short_client_stops() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    client_a = Site(
        name="Client A",
        latitude=Decimal("42.3314"),
        longitude=Decimal("-83.0458"),
        radius_m=120,
    )
    client_b = Site(
        name="Client B",
        latitude=Decimal("42.3440"),
        longitude=Decimal("-83.0600"),
        radius_m=120,
    )
    db.add_all(
        [
            client_a,
            client_b,
            _location(day, "42.3314", "-83.0458"),
            _location(day + timedelta(minutes=12), "42.3315", "-83.0459"),
            _location(day + timedelta(minutes=25), "42.3440", "-83.0600"),
            _location(day + timedelta(minutes=31), "42.3441", "-83.0601"),
        ]
    )
    db.commit()

    assert generate_trips(db, day.date(), day.date()) == []


def test_generate_trips_to_unknown_stationary_stop() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    client_a = Site(
        name="Client A",
        latitude=Decimal("42.3314"),
        longitude=Decimal("-83.0458"),
        radius_m=120,
    )
    db.add_all(
        [
            client_a,
            _location(day, "42.3314", "-83.0458"),
            _location(day + timedelta(minutes=12), "42.3315", "-83.0459"),
            _location(day + timedelta(minutes=25), "42.3500", "-83.0700"),
            _location(day + timedelta(minutes=38), "42.3501", "-83.0701"),
        ]
    )
    db.commit()

    trips = generate_trips(db, day.date(), day.date())

    assert len(trips) == 1
    assert trips[0].origin_site_id == client_a.id
    assert trips[0].destination_site_id is None
    assert "unknown stationary stop" in trips[0].notes
    assert db.scalar(select(Trip).where(Trip.id == trips[0].id)) is not None


def test_generate_trips_uses_google_enriched_unknown_stop(monkeypatch) -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    client_a = Site(
        name="Client A",
        latitude=Decimal("42.3314"),
        longitude=Decimal("-83.0458"),
        radius_m=120,
    )
    db.add_all(
        [
            client_a,
            _location(day, "42.3314", "-83.0458"),
            _location(day + timedelta(minutes=12), "42.3315", "-83.0459"),
            _location(day + timedelta(minutes=25), "42.3500", "-83.0700"),
            _location(day + timedelta(minutes=38), "42.3501", "-83.0701"),
        ]
    )
    db.commit()

    def fake_create_site_from_google_place(
        session: Session,
        latitude: Decimal,
        longitude: Decimal,
    ) -> Site:
        site = Site(
            name="Google Client",
            latitude=latitude,
            longitude=longitude,
            radius_m=150,
        )
        session.add(site)
        session.flush()
        return site

    monkeypatch.setattr(
        "mileage_logger.services.mileage.create_site_from_google_place",
        fake_create_site_from_google_place,
    )

    trips = generate_trips(db, day.date(), day.date())

    assert len(trips) == 1
    assert trips[0].origin_site_id == client_a.id
    assert trips[0].destination_site is not None
    assert trips[0].destination_site.name == "Google Client"
    assert "unknown stationary stop" not in trips[0].notes
