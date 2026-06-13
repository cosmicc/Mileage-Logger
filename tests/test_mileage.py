from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from mileage_logger.models import Base, GasPriceSnapshot, OwnTracksLocation, Site, Trip
from mileage_logger.services.mileage import (
    MANUAL_TRIP_SOURCE,
    MILEAGE_SOURCE_ESTIMATED_ODOMETER,
    MILEAGE_SOURCE_FORDPASS_ODOMETER,
    MILEAGE_SOURCE_MANUAL,
    MILEAGE_SOURCE_WAYPOINT_DISTANCE,
    generate_trips,
    haversine_miles,
    update_trip_details,
)
from mileage_logger.services.retention import reset_previous_month_data
from mileage_logger.services.trip_processor import run_automatic_trip_processing


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _site(name: str, latitude: str, longitude: str, region_id: str | None = None) -> Site:
    return Site(
        name=name,
        owntracks_region_id=region_id,
        latitude=Decimal(latitude),
        longitude=Decimal(longitude),
        radius_m=120,
    )


def _transition(
    captured_at: datetime,
    site: Site,
    event: str,
    *,
    duplicate_region_name: bool = False,
) -> OwnTracksLocation:
    payload = {
        "_type": "transition",
        "event": event,
        "desc": site.name,
    }
    if site.owntracks_region_id:
        payload["rid"] = site.owntracks_region_id
    if duplicate_region_name:
        payload["inregions"] = [site.name]
    return OwnTracksLocation(
        captured_at=captured_at,
        received_at=captured_at,
        latitude=site.latitude,
        longitude=site.longitude,
        raw_payload=payload,
    )


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


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None)


def test_haversine_miles_returns_expected_short_distance() -> None:
    miles = haversine_miles(
        Decimal("42.3314"),
        Decimal("-83.0458"),
        Decimal("42.7325"),
        Decimal("-84.5555"),
    )

    assert Decimal("81.00") < miles < Decimal("83.00")


def test_generate_trips_from_leave_and_enter_transitions() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458", "home-rid")
    client = _site("Client", "42.3440", "-83.0600", "client-rid")
    db.add_all(
        [
            home,
            client,
            _transition(day, home, "leave"),
            _transition(day + timedelta(minutes=24), client, "enter"),
        ]
    )
    db.commit()

    trips = generate_trips(db, day.date(), day.date())

    assert len(trips) == 1
    assert trips[0].origin_site_id == home.id
    assert trips[0].destination_site_id == client.id
    assert trips[0].origin_display_name == "Home"
    assert trips[0].destination_display_name == "Client"
    assert trips[0].started_at == _naive(day)
    assert trips[0].ended_at == _naive(day + timedelta(minutes=24))
    assert trips[0].miles == haversine_miles(
        home.latitude,
        home.longitude,
        client.latitude,
        client.longitude,
    )
    assert trips[0].mileage_source == MILEAGE_SOURCE_WAYPOINT_DISTANCE


def test_generate_trips_uses_fordpass_odometer_delta(monkeypatch) -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    readings = iter([Decimal("1000.250"), Decimal("1012.875")])
    monkeypatch.setattr(
        "mileage_logger.services.mileage.current_odometer_miles",
        lambda: next(readings),
    )
    db.add_all(
        [
            home,
            client,
            _transition(day, home, "leave"),
            _transition(day + timedelta(minutes=24), client, "enter"),
        ]
    )
    db.commit()

    trips = generate_trips(db, day.date(), day.date())

    assert len(trips) == 1
    assert trips[0].miles == Decimal("12.63")
    assert trips[0].start_odometer_miles == Decimal("1000.250")
    assert trips[0].end_odometer_miles == Decimal("1012.875")
    assert trips[0].mileage_source == MILEAGE_SOURCE_FORDPASS_ODOMETER


def test_generate_trips_reuses_existing_auto_odometer_trip(monkeypatch) -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    readings = iter([Decimal("1000.000"), Decimal("1006.500")])
    monkeypatch.setattr(
        "mileage_logger.services.mileage.current_odometer_miles",
        lambda: next(readings),
    )
    db.add_all(
        [
            home,
            client,
            _transition(day, home, "leave"),
            _transition(day + timedelta(minutes=24), client, "enter"),
        ]
    )
    db.commit()
    generate_trips(db, day.date(), day.date())
    monkeypatch.setattr(
        "mileage_logger.services.mileage.current_odometer_miles",
        lambda: Decimal("2000.000"),
    )

    regenerated = generate_trips(db, day.date(), day.date())

    assert len(regenerated) == 1
    assert regenerated[0].miles == Decimal("6.50")
    assert regenerated[0].start_odometer_miles == Decimal("1000.000")
    assert regenerated[0].end_odometer_miles == Decimal("1006.500")
    assert regenerated[0].mileage_source == MILEAGE_SOURCE_FORDPASS_ODOMETER


def test_generate_trips_estimates_missing_start_odometer_from_distance(monkeypatch) -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    readings = iter([None, Decimal("1012.875")])
    monkeypatch.setattr(
        "mileage_logger.services.mileage.current_odometer_miles",
        lambda: next(readings),
    )
    db.add_all(
        [
            home,
            client,
            _transition(day, home, "leave"),
            _transition(day + timedelta(minutes=24), client, "enter"),
        ]
    )
    db.commit()

    trips = generate_trips(db, day.date(), day.date())
    distance = haversine_miles(home.latitude, home.longitude, client.latitude, client.longitude)

    assert len(trips) == 1
    assert trips[0].miles == distance
    assert trips[0].end_odometer_miles == Decimal("1012.875")
    assert trips[0].start_odometer_miles == (Decimal("1012.875") - distance).quantize(
        Decimal("0.001")
    )
    assert trips[0].mileage_source == MILEAGE_SOURCE_ESTIMATED_ODOMETER
    assert "Estimated odometer" in trips[0].notes


def test_generate_trips_estimates_from_prior_odometer_anchor_when_fordpass_unavailable(
    monkeypatch,
) -> None:
    db = _session()
    previous_day = datetime(2026, 6, 10, 13, 0, tzinfo=UTC)
    day = previous_day + timedelta(days=1)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    db.add_all([home, client])
    db.flush()
    db.add(
        Trip(
            trip_date=previous_day.date(),
            origin_site_id=client.id,
            destination_site_id=home.id,
            started_at=previous_day,
            ended_at=previous_day + timedelta(minutes=20),
            start_latitude=client.latitude,
            start_longitude=client.longitude,
            end_latitude=home.latitude,
            end_longitude=home.longitude,
            miles=Decimal("20.00"),
            end_odometer_miles=Decimal("2000.000"),
            mileage_source=MILEAGE_SOURCE_ESTIMATED_ODOMETER,
            source="auto",
        )
    )
    db.add_all(
        [
            _transition(day, home, "leave"),
            _transition(day + timedelta(minutes=24), client, "enter"),
        ]
    )
    db.commit()
    monkeypatch.setattr("mileage_logger.services.mileage.current_odometer_miles", lambda: None)

    trips = generate_trips(db, day.date(), day.date())
    distance = haversine_miles(home.latitude, home.longitude, client.latitude, client.longitude)

    assert len(trips) == 1
    assert trips[0].miles == distance
    assert trips[0].start_odometer_miles == Decimal("2000.000")
    assert trips[0].end_odometer_miles == (Decimal("2000.000") + distance).quantize(
        Decimal("0.001")
    )
    assert trips[0].mileage_source == MILEAGE_SOURCE_ESTIMATED_ODOMETER


def test_generate_trips_ignores_home_to_home_but_allows_same_work_waypoint() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    db.add_all(
        [
            home,
            client,
            _transition(day, home, "leave"),
            _transition(day + timedelta(minutes=10), home, "enter"),
            _transition(day + timedelta(hours=1), client, "leave"),
            _transition(day + timedelta(hours=2), client, "enter"),
        ]
    )
    db.commit()

    trips = generate_trips(db, day.date(), day.date())

    assert len(trips) == 1
    assert trips[0].origin_site_id == client.id
    assert trips[0].destination_site_id == client.id
    assert trips[0].miles == Decimal("0.00")


def test_generate_trips_assumes_missing_first_leave_was_from_home() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    db.add_all([home, client, _transition(day, client, "enter")])
    db.commit()

    trips = generate_trips(db, day.date(), day.date())

    assert len(trips) == 1
    assert trips[0].origin_site_id == home.id
    assert trips[0].destination_site_id == client.id
    assert trips[0].started_at == _naive(day)
    assert trips[0].ended_at == _naive(day)
    assert "Missing leave event" in trips[0].notes


def test_generate_trips_does_not_create_missing_leave_home_to_home() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    db.add_all([home, _transition(day, home, "enter")])
    db.commit()

    assert generate_trips(db, day.date(), day.date()) == []
    assert db.scalar(select(Trip.id)) is None


def test_generate_trips_uses_previous_arrival_when_next_leave_is_missing() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client_a = _site("Client A", "42.3440", "-83.0600")
    client_b = _site("Client B", "42.3600", "-83.0700")
    db.add_all(
        [
            home,
            client_a,
            client_b,
            _transition(day, client_a, "enter"),
            _transition(day + timedelta(hours=2), client_b, "enter"),
        ]
    )
    db.commit()

    trips = generate_trips(db, day.date(), day.date())

    assert len(trips) == 2
    assert trips[0].origin_site_id == home.id
    assert trips[0].destination_site_id == client_a.id
    assert trips[1].origin_site_id == client_a.id
    assert trips[1].destination_site_id == client_b.id
    assert trips[1].started_at == _naive(day + timedelta(hours=2))
    assert trips[1].ended_at == _naive(day + timedelta(hours=2))


def test_generate_trips_ignores_non_transition_location_points() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    db.add_all(
        [
            _site("Home", "42.3314", "-83.0458"),
            _site("Client", "42.3440", "-83.0600"),
            _location(day, "42.3314", "-83.0458"),
            _location(day + timedelta(minutes=20), "42.3440", "-83.0600"),
        ]
    )
    db.commit()

    assert generate_trips(db, day.date(), day.date()) == []


def test_manual_mileage_edit_is_preserved_without_reusing_future_matching_route() -> None:
    db = _session()
    first_day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    second_day = first_day + timedelta(days=1)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    db.add_all(
        [
            home,
            client,
            _transition(first_day, home, "leave"),
            _transition(first_day + timedelta(minutes=20), client, "enter"),
        ]
    )
    db.commit()
    first_trip = generate_trips(db, first_day.date(), first_day.date())[0]

    update_trip_details(first_trip, "Home", "Client", Decimal("12.34"))
    db.commit()
    db.add_all(
        [
            _transition(second_day, home, "leave"),
            _transition(second_day + timedelta(minutes=20), client, "enter"),
        ]
    )
    db.commit()

    regenerated = generate_trips(db, first_day.date(), first_day.date())
    future_trips = generate_trips(db, second_day.date(), second_day.date())

    assert regenerated == []
    assert first_trip.source == MANUAL_TRIP_SOURCE
    assert first_trip.mileage_source == MILEAGE_SOURCE_MANUAL
    assert future_trips[0].miles == haversine_miles(
        home.latitude,
        home.longitude,
        client.latitude,
        client.longitude,
    )
    assert future_trips[0].mileage_source == MILEAGE_SOURCE_WAYPOINT_DISTANCE


def test_generate_trips_does_not_delete_existing_auto_trips_without_source_events() -> None:
    db = _session()
    day = datetime(2026, 6, 9, 13, 0, tzinfo=UTC)
    trip = Trip(
        trip_date=day.date(),
        started_at=day,
        ended_at=day + timedelta(minutes=30),
        start_latitude=Decimal("42.3314"),
        start_longitude=Decimal("-83.0458"),
        end_latitude=Decimal("42.3440"),
        end_longitude=Decimal("-83.0600"),
        miles=Decimal("5.00"),
        source="auto",
    )
    db.add(trip)
    db.commit()

    generated = generate_trips(db, day.date(), day.date())

    assert generated == []
    assert db.scalar(select(Trip).where(Trip.id == trip.id)) is not None


def test_reset_previous_month_data_keeps_current_month_and_waypoints() -> None:
    db = _session()
    previous_month = datetime(2026, 5, 31, 13, 0, tzinfo=UTC)
    current_month = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    site = _site("Home", "42.3314", "-83.0458")
    previous_trip = Trip(
        trip_date=date(2026, 5, 31),
        started_at=previous_month,
        ended_at=previous_month + timedelta(minutes=30),
        start_latitude=Decimal("42.3314"),
        start_longitude=Decimal("-83.0458"),
        end_latitude=Decimal("42.3440"),
        end_longitude=Decimal("-83.0600"),
        miles=Decimal("5.00"),
        source="auto",
    )
    current_trip = Trip(
        trip_date=date(2026, 6, 1),
        started_at=current_month,
        ended_at=current_month + timedelta(minutes=30),
        start_latitude=Decimal("42.3314"),
        start_longitude=Decimal("-83.0458"),
        end_latitude=Decimal("42.3440"),
        end_longitude=Decimal("-83.0600"),
        miles=Decimal("6.00"),
        source="auto",
    )
    db.add_all(
        [
            site,
            _location(previous_month, "42.3314", "-83.0458"),
            _location(current_month, "42.3500", "-83.0700"),
            previous_trip,
            current_trip,
            GasPriceSnapshot(
                observed_on=date(2026, 5, 31),
                state="MI",
                grade="regular",
                price_per_gallon=Decimal("3.000"),
                source="test",
            ),
            GasPriceSnapshot(
                observed_on=date(2026, 6, 1),
                state="MI",
                grade="regular",
                price_per_gallon=Decimal("3.100"),
                source="test",
            ),
        ]
    )
    db.commit()

    result = reset_previous_month_data(db, now=current_month)

    remaining_locations = list(db.scalars(select(OwnTracksLocation)))
    remaining_trips = list(db.scalars(select(Trip)))
    remaining_snapshots = list(db.scalars(select(GasPriceSnapshot)))
    assert result.location_points == 1
    assert result.trips == 1
    assert result.gas_snapshots == 1
    assert db.scalar(select(Site).where(Site.id == site.id)) is not None
    assert [location.captured_at for location in remaining_locations] == [_naive(current_month)]
    assert [trip.trip_date for trip in remaining_trips] == [date(2026, 6, 1)]
    assert [snapshot.observed_on for snapshot in remaining_snapshots] == [date(2026, 6, 1)]


def test_automatic_trip_processing_finalizes_completed_days_without_daily_purge() -> None:
    db = _session()
    completed_day = datetime(2026, 6, 9, 13, 0, tzinfo=UTC)
    current_day = datetime(2026, 6, 10, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    db.add_all(
        [
            home,
            client,
            _transition(completed_day, home, "leave"),
            _transition(completed_day + timedelta(minutes=30), client, "enter"),
            _location(current_day, "42.3500", "-83.0700"),
        ]
    )
    db.commit()

    result = run_automatic_trip_processing(db, now=current_day)

    trips = list(db.scalars(select(Trip).order_by(Trip.started_at.asc())))
    remaining_locations = list(db.scalars(select(OwnTracksLocation)))
    assert result.generated == 1
    assert result.monthly_reset.total == 0
    assert len(trips) == 1
    assert trips[0].trip_date == completed_day.date()
    assert [location.captured_at for location in remaining_locations] == [
        _naive(completed_day),
        _naive(completed_day + timedelta(minutes=30)),
        _naive(current_day),
    ]


def test_automatic_trip_processing_keeps_current_local_day_after_utc_midnight() -> None:
    db = _session()
    previous_day = datetime(2026, 6, 11, 23, 0, tzinfo=UTC)
    current_time = datetime(2026, 6, 12, 1, 30, tzinfo=UTC)
    client = _site("Client", "42.3440", "-83.0600")
    home = _site("Home", "42.3314", "-83.0458")
    db.add_all(
        [
            client,
            home,
            _transition(previous_day, client, "leave"),
            _transition(previous_day + timedelta(minutes=75), home, "enter"),
        ]
    )
    db.commit()

    result = run_automatic_trip_processing(
        db,
        touched_date=date(2026, 6, 11),
        now=current_time,
    )

    trips = list(db.scalars(select(Trip).order_by(Trip.started_at.asc())))
    remaining_locations = list(db.scalars(select(OwnTracksLocation)))
    assert result.generated == 1
    assert result.monthly_reset.total == 0
    assert len(trips) == 1
    assert trips[0].trip_date == previous_day.date()
    assert trips[0].origin_site.name == "Client"
    assert trips[0].destination_site.name == "Home"
    assert [location.captured_at for location in remaining_locations] == [
        _naive(previous_day),
        _naive(previous_day + timedelta(minutes=75)),
    ]


def test_automatic_trip_processing_resets_previous_month_after_local_month_start() -> None:
    db = _session()
    previous_day = datetime(2026, 5, 31, 23, 0, tzinfo=UTC)
    current_time = datetime(2026, 6, 1, 5, 30, tzinfo=UTC)
    client = _site("Client", "42.3440", "-83.0600")
    home = _site("Home", "42.3314", "-83.0458")
    db.add_all(
        [
            client,
            home,
            _transition(previous_day, client, "leave"),
            _transition(previous_day + timedelta(minutes=75), home, "enter"),
        ]
    )
    db.commit()

    result = run_automatic_trip_processing(
        db,
        touched_date=date(2026, 6, 1),
        now=current_time,
    )

    trips = list(db.scalars(select(Trip).order_by(Trip.started_at.asc())))
    remaining_locations = list(db.scalars(select(OwnTracksLocation)))
    assert result.generated == 0
    assert result.monthly_reset.location_points == 2
    assert len(trips) == 0
    assert remaining_locations == []
    assert db.scalar(select(Site).where(Site.name == "Client")) is not None
    assert db.scalar(select(Site).where(Site.name == "Home")) is not None
