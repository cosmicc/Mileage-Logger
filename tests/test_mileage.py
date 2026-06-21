from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from mileage_logger.models import (
    AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
    Base,
    DeletedTrip,
    GasPriceSnapshot,
    OwnTracksLocation,
    Site,
    Trip,
    TripProcessingCheckpoint,
)
from mileage_logger.services.mileage import (
    MANUAL_TRIP_SOURCE,
    MILEAGE_SOURCE_ESTIMATED_ODOMETER,
    MILEAGE_SOURCE_MANUAL,
    MILEAGE_SOURCE_OWNTRACKS_PATH,
    MILEAGE_SOURCE_WAYPOINT_DISTANCE,
    ODOMETER_SOURCE_ESTIMATED,
    ODOMETER_SOURCE_MANUAL,
    ODOMETER_SOURCE_OWNTRACKS_ROLLING,
    ODOMETER_SOURCE_PREVIOUS_TRIP,
    create_manual_trip,
    delete_trip,
    generate_trips,
    haversine_miles,
    update_trip_details,
)
from mileage_logger.services.retention import (
    purge_processed_owntracks_locations,
    reset_previous_month_data,
)
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


def _dwell_confirmation(captured_at: datetime, site: Site) -> OwnTracksLocation:
    return _location(
        captured_at + timedelta(minutes=5),
        str(site.latitude),
        str(site.longitude),
        {"_type": "location", "inregions": [site.name]},
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


def test_create_manual_trip_saves_editable_manual_values() -> None:
    db = _session()

    trip = create_manual_trip(
        db,
        trip_date=date(2026, 6, 15),
        origin_name="Home",
        destination_name="Client",
        miles=Decimal("12.345"),
    )
    db.commit()

    assert trip.trip_date == date(2026, 6, 15)
    assert trip.origin_display_name == "Home"
    assert trip.destination_display_name == "Client"
    assert trip.miles == Decimal("12.3")
    assert trip.source == MANUAL_TRIP_SOURCE
    assert trip.mileage_source == MILEAGE_SOURCE_MANUAL
    assert trip.origin_site_id is None
    assert trip.destination_site_id is None


def test_create_manual_trip_saves_odometer_from_current_checkpoint() -> None:
    db = _session()
    trip_date = date(2026, 6, 15)
    current_odometer_at = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    db.add(
        TripProcessingCheckpoint(
            name=AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
            odometer_anchor_miles=Decimal("1234.4"),
            odometer_anchor_recorded_at=current_odometer_at,
        )
    )
    db.commit()

    trip = create_manual_trip(
        db,
        trip_date=trip_date,
        origin_name="Home",
        destination_name="Client",
        miles=Decimal("12.34"),
    )
    db.commit()

    assert trip.start_odometer_miles == Decimal("1234.4")
    assert trip.end_odometer_miles == Decimal("1246.7")
    assert trip.start_odometer_source == ODOMETER_SOURCE_MANUAL
    assert trip.end_odometer_source == ODOMETER_SOURCE_ESTIMATED


def test_create_manual_prior_trip_resequences_all_later_trip_odometers() -> None:
    db = _session()
    june_twentieth = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
    july_first = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    later_june_trip = Trip(
        trip_date=june_twentieth.date(),
        started_at=june_twentieth,
        ended_at=june_twentieth + timedelta(minutes=30),
        start_latitude=Decimal("42.3314"),
        start_longitude=Decimal("-83.0458"),
        end_latitude=Decimal("42.3440"),
        end_longitude=Decimal("-83.0600"),
        miles=Decimal("5.0"),
        start_odometer_miles=Decimal("2000.0"),
        end_odometer_miles=Decimal("2005.0"),
        start_odometer_source=ODOMETER_SOURCE_PREVIOUS_TRIP,
        end_odometer_source=ODOMETER_SOURCE_ESTIMATED,
        source="auto",
    )
    later_july_trip = Trip(
        trip_date=july_first.date(),
        started_at=july_first,
        ended_at=july_first + timedelta(minutes=30),
        start_latitude=Decimal("42.3314"),
        start_longitude=Decimal("-83.0458"),
        end_latitude=Decimal("42.3440"),
        end_longitude=Decimal("-83.0600"),
        miles=Decimal("7.0"),
        start_odometer_miles=Decimal("2005.0"),
        end_odometer_miles=Decimal("2012.0"),
        start_odometer_source=ODOMETER_SOURCE_PREVIOUS_TRIP,
        end_odometer_source=ODOMETER_SOURCE_ESTIMATED,
        source="auto",
    )
    db.add_all(
        [
            later_june_trip,
            later_july_trip,
            TripProcessingCheckpoint(
                name=AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
                odometer_anchor_miles=Decimal("5000.0"),
                odometer_anchor_recorded_at=july_first + timedelta(days=1),
            ),
        ]
    )
    db.commit()

    manual_trip = create_manual_trip(
        db,
        trip_date=date(2026, 6, 10),
        origin_name="Home",
        destination_name="Client",
        miles=Decimal("3.5"),
    )
    db.commit()

    assert manual_trip.start_odometer_miles == Decimal("5000.0")
    assert manual_trip.end_odometer_miles == Decimal("5003.5")
    assert later_june_trip.start_odometer_miles == Decimal("5003.5")
    assert later_june_trip.end_odometer_miles == Decimal("5008.5")
    assert later_july_trip.start_odometer_miles == Decimal("5008.5")
    assert later_july_trip.end_odometer_miles == Decimal("5015.5")


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
            _dwell_confirmation(day + timedelta(minutes=24), client),
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


def test_generate_trips_confirms_arrival_from_as_of_without_follow_up_location() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    arrival_at = day + timedelta(minutes=24)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    db.add_all(
        [
            home,
            client,
            _transition(day, home, "leave"),
            _transition(arrival_at, client, "enter"),
        ]
    )
    db.commit()

    early_trips = generate_trips(
        db,
        day.date(),
        day.date(),
        as_of=arrival_at + timedelta(minutes=4),
    )
    mature_trips = generate_trips(
        db,
        day.date(),
        day.date(),
        as_of=arrival_at + timedelta(minutes=6),
    )

    assert early_trips == []
    assert len(mature_trips) == 1
    assert mature_trips[0].origin_site_id == home.id
    assert mature_trips[0].destination_site_id == client.id


def test_generate_trips_ignores_drive_through_waypoint_without_dwell() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    db.add_all(
        [
            home,
            client,
            _transition(day, home, "leave"),
            _transition(day + timedelta(minutes=10), client, "enter"),
            _transition(day + timedelta(minutes=12), client, "leave"),
        ]
    )
    db.commit()

    trips = generate_trips(db, day.date(), day.date())

    assert trips == []
    assert db.scalar(select(Trip.id)) is None


def test_generate_trips_estimates_odometer_from_checkpoint_anchor() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    db.add_all(
        [
            home,
            client,
            TripProcessingCheckpoint(
                name=AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
                odometer_anchor_miles=Decimal("1000.250"),
                odometer_anchor_recorded_at=day - timedelta(minutes=1),
            ),
            _transition(day, home, "leave"),
            _transition(day + timedelta(minutes=24), client, "enter"),
            _dwell_confirmation(day + timedelta(minutes=24), client),
        ]
    )
    db.commit()

    trips = generate_trips(db, day.date(), day.date())
    distance = haversine_miles(home.latitude, home.longitude, client.latitude, client.longitude)

    assert len(trips) == 1
    assert trips[0].miles == distance
    assert trips[0].start_odometer_miles == Decimal("1000.2")
    assert trips[0].end_odometer_miles == (Decimal("1000.2") + distance).quantize(
        Decimal("0.1")
    )
    assert trips[0].mileage_source == MILEAGE_SOURCE_WAYPOINT_DISTANCE
    assert trips[0].start_odometer_source == ODOMETER_SOURCE_PREVIOUS_TRIP
    assert trips[0].end_odometer_source == ODOMETER_SOURCE_ESTIMATED


def test_generate_trips_uses_owntracks_path_with_checkpoint_odometer() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    route_point_one_latitude = Decimal("42.3314")
    route_point_one_longitude = Decimal("-83.0600")
    route_point_two_latitude = Decimal("42.3380")
    route_point_two_longitude = Decimal("-83.0700")
    db.add_all(
        [
            home,
            client,
            TripProcessingCheckpoint(
                name=AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
                odometer_anchor_miles=Decimal("1000.000"),
                odometer_anchor_recorded_at=day - timedelta(minutes=1),
            ),
            _transition(day, home, "leave"),
            _location(
                day + timedelta(minutes=5),
                str(route_point_one_latitude),
                str(route_point_one_longitude),
            ),
            _location(
                day + timedelta(minutes=10),
                str(route_point_two_latitude),
                str(route_point_two_longitude),
            ),
            _transition(day + timedelta(minutes=24), client, "enter"),
            _dwell_confirmation(day + timedelta(minutes=24), client),
        ]
    )
    db.commit()

    trips = generate_trips(db, day.date(), day.date())
    expected_path_miles = (
        haversine_miles(
            home.latitude,
            home.longitude,
            route_point_one_latitude,
            route_point_one_longitude,
        )
        + haversine_miles(
            route_point_one_latitude,
            route_point_one_longitude,
            route_point_two_latitude,
            route_point_two_longitude,
        )
        + haversine_miles(
            route_point_two_latitude,
            route_point_two_longitude,
            client.latitude,
            client.longitude,
        )
    ).quantize(Decimal("0.1"))

    assert len(trips) == 1
    assert trips[0].miles == expected_path_miles
    assert trips[0].start_odometer_miles == Decimal("1000.0")
    assert trips[0].end_odometer_miles == (Decimal("1000.0") + expected_path_miles).quantize(
        Decimal("0.1")
    )
    assert trips[0].mileage_source == MILEAGE_SOURCE_OWNTRACKS_PATH
    assert trips[0].start_odometer_source == ODOMETER_SOURCE_PREVIOUS_TRIP
    assert trips[0].end_odometer_source == ODOMETER_SOURCE_ESTIMATED


def test_generate_trips_uses_manual_checkpoint_as_anchor() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    db.add_all(
        [
            home,
            client,
            TripProcessingCheckpoint(
                name=AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
                odometer_anchor_miles=Decimal("1000.000"),
                odometer_anchor_recorded_at=day - timedelta(minutes=1),
            ),
            _transition(day, home, "leave"),
            _transition(day + timedelta(minutes=24), client, "enter"),
            _dwell_confirmation(day + timedelta(minutes=24), client),
        ]
    )
    db.commit()

    trips = generate_trips(db, day.date(), day.date())
    distance = haversine_miles(home.latitude, home.longitude, client.latitude, client.longitude)

    assert len(trips) == 1
    assert trips[0].miles == distance
    assert trips[0].mileage_source == MILEAGE_SOURCE_WAYPOINT_DISTANCE
    assert trips[0].start_odometer_miles == Decimal("1000.0")
    assert trips[0].end_odometer_miles == (Decimal("1000.0") + distance).quantize(
        Decimal("0.1")
    )
    assert trips[0].start_odometer_source == ODOMETER_SOURCE_PREVIOUS_TRIP
    assert trips[0].end_odometer_source == ODOMETER_SOURCE_ESTIMATED


def test_generate_trips_uses_owntracks_path_before_waypoint_distance() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    route_point_one_latitude = Decimal("42.3314")
    route_point_one_longitude = Decimal("-83.0600")
    route_point_two_latitude = Decimal("42.3380")
    route_point_two_longitude = Decimal("-83.0700")
    db.add_all(
        [
            home,
            client,
            TripProcessingCheckpoint(
                name=AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
                odometer_anchor_miles=Decimal("1000.250"),
                odometer_anchor_recorded_at=day - timedelta(minutes=1),
            ),
            _transition(day, home, "leave"),
            _location(
                day + timedelta(minutes=5),
                str(route_point_one_latitude),
                str(route_point_one_longitude),
            ),
            _location(
                day + timedelta(minutes=10),
                str(route_point_two_latitude),
                str(route_point_two_longitude),
            ),
            _transition(day + timedelta(minutes=24), client, "enter"),
            _dwell_confirmation(day + timedelta(minutes=24), client),
        ]
    )
    db.commit()

    trips = generate_trips(db, day.date(), day.date())
    expected_path_miles = (
        haversine_miles(
            home.latitude,
            home.longitude,
            route_point_one_latitude,
            route_point_one_longitude,
        )
        + haversine_miles(
            route_point_one_latitude,
            route_point_one_longitude,
            route_point_two_latitude,
            route_point_two_longitude,
        )
        + haversine_miles(
            route_point_two_latitude,
            route_point_two_longitude,
            client.latitude,
            client.longitude,
        )
    ).quantize(Decimal("0.1"))

    assert len(trips) == 1
    assert trips[0].miles == expected_path_miles
    assert trips[0].mileage_source == MILEAGE_SOURCE_OWNTRACKS_PATH
    assert trips[0].start_odometer_miles == Decimal("1000.2")
    assert trips[0].end_odometer_miles == (Decimal("1000.2") + expected_path_miles).quantize(
        Decimal("0.1")
    )
    assert trips[0].start_odometer_source == ODOMETER_SOURCE_PREVIOUS_TRIP
    assert trips[0].end_odometer_source == ODOMETER_SOURCE_ESTIMATED
    assert "OwnTracks location path" in trips[0].notes


def test_generate_trips_updates_existing_estimated_trip_when_location_path_arrives() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    db.add_all(
        [
            home,
            client,
            TripProcessingCheckpoint(
                name=AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
                odometer_anchor_miles=Decimal("1000.000"),
                odometer_anchor_recorded_at=day - timedelta(minutes=1),
            ),
            _transition(day, home, "leave"),
            _transition(day + timedelta(minutes=24), client, "enter"),
            _dwell_confirmation(day + timedelta(minutes=24), client),
        ]
    )
    db.commit()
    first_generation = generate_trips(db, day.date(), day.date())
    first_trip_id = first_generation[0].id
    db.add_all(
        [
            _location(day + timedelta(minutes=5), "42.3314", "-83.0600"),
            _location(day + timedelta(minutes=10), "42.3380", "-83.0700"),
            _dwell_confirmation(day + timedelta(minutes=24), client),
        ]
    )
    db.commit()

    regenerated = generate_trips(db, day.date(), day.date())
    all_trips = list(db.scalars(select(Trip).order_by(Trip.id.asc())))

    assert [trip.id for trip in all_trips] == [first_trip_id]
    assert [trip.id for trip in regenerated] == [first_trip_id]
    assert all_trips[0].mileage_source == MILEAGE_SOURCE_OWNTRACKS_PATH
    assert all_trips[0].miles != Decimal("6.50")


def test_generate_trips_reuses_existing_auto_estimated_trip() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    db.add_all(
        [
            home,
            client,
            TripProcessingCheckpoint(
                name=AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
                odometer_anchor_miles=Decimal("1000.000"),
                odometer_anchor_recorded_at=day - timedelta(minutes=1),
            ),
            _transition(day, home, "leave"),
            _transition(day + timedelta(minutes=24), client, "enter"),
            _dwell_confirmation(day + timedelta(minutes=24), client),
        ]
    )
    db.commit()
    first_generation = generate_trips(db, day.date(), day.date())
    first_trip_id = first_generation[0].id

    regenerated = generate_trips(db, day.date(), day.date())
    all_trips = list(db.scalars(select(Trip).order_by(Trip.id.asc())))

    assert regenerated == []
    assert [trip.id for trip in all_trips] == [first_trip_id]
    assert all_trips[0].miles == haversine_miles(
        home.latitude,
        home.longitude,
        client.latitude,
        client.longitude,
    )
    assert all_trips[0].start_odometer_miles == Decimal("1000.0")
    assert all_trips[0].mileage_source == MILEAGE_SOURCE_WAYPOINT_DISTANCE


def test_generate_trips_does_not_rewrite_existing_waypoint_distance_trip() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    db.add_all(
        [
            home,
            client,
            _transition(day, home, "leave"),
            _transition(day + timedelta(minutes=24), client, "enter"),
            _dwell_confirmation(day + timedelta(minutes=24), client),
        ]
    )
    db.commit()
    first_generation = generate_trips(db, day.date(), day.date())
    first_trip_id = first_generation[0].id

    regenerated = generate_trips(db, day.date(), day.date())
    all_trips = list(db.scalars(select(Trip).order_by(Trip.id.asc())))

    assert regenerated == []
    assert [trip.id for trip in all_trips] == [first_trip_id]
    assert all_trips[0].mileage_source == MILEAGE_SOURCE_WAYPOINT_DISTANCE


def test_deleted_generated_trip_is_not_recreated_from_same_transitions() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    db.add_all(
        [
            home,
            client,
            _transition(day, home, "leave"),
            _transition(day + timedelta(minutes=24), client, "enter"),
            _dwell_confirmation(day + timedelta(minutes=24), client),
        ]
    )
    db.commit()
    trip = generate_trips(db, day.date(), day.date())[0]

    deleted_trip = delete_trip(db, trip)
    db.commit()
    regenerated = generate_trips(db, day.date(), day.date())

    assert deleted_trip is not None
    assert regenerated == []
    assert db.scalar(select(Trip)) is None
    stored_deleted_trip = db.scalar(select(DeletedTrip))
    assert stored_deleted_trip is not None
    assert stored_deleted_trip.origin_site_id == home.id
    assert stored_deleted_trip.destination_site_id == client.id
    assert stored_deleted_trip.started_at == _naive(day)
    assert stored_deleted_trip.ended_at == _naive(day + timedelta(minutes=24))


def test_deleted_generated_trip_does_not_block_future_matching_route() -> None:
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
            _transition(first_day + timedelta(minutes=24), client, "enter"),
            _dwell_confirmation(first_day + timedelta(minutes=24), client),
        ]
    )
    db.commit()
    first_trip = generate_trips(db, first_day.date(), first_day.date())[0]

    deleted_trip = delete_trip(db, first_trip)
    db.add_all(
        [
            _transition(second_day, home, "leave"),
            _transition(second_day + timedelta(minutes=24), client, "enter"),
            _dwell_confirmation(second_day + timedelta(minutes=24), client),
        ]
    )
    db.commit()
    regenerated_first_day = generate_trips(db, first_day.date(), first_day.date())
    future_trips = generate_trips(db, second_day.date(), second_day.date())

    assert deleted_trip is not None
    assert regenerated_first_day == []
    assert len(future_trips) == 1
    assert future_trips[0].origin_site_id == home.id
    assert future_trips[0].destination_site_id == client.id
    assert future_trips[0].started_at == _naive(second_day)
    assert future_trips[0].ended_at == _naive(second_day + timedelta(minutes=24))


def test_delete_trip_preserves_latest_odometer_checkpoint() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    trip = Trip(
        trip_date=day.date(),
        started_at=day,
        ended_at=day + timedelta(minutes=24),
        start_latitude=Decimal("42.3314"),
        start_longitude=Decimal("-83.0458"),
        end_latitude=Decimal("42.3440"),
        end_longitude=Decimal("-83.0600"),
        miles=Decimal("5.0"),
        start_odometer_miles=Decimal("1000.0"),
        end_odometer_miles=Decimal("1005.0"),
        source="auto",
    )
    db.add(trip)
    db.add(
        TripProcessingCheckpoint(
            name=AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
            odometer_anchor_miles=Decimal("999.0"),
            odometer_anchor_recorded_at=day - timedelta(hours=1),
        )
    )
    db.commit()

    delete_trip(db, trip)
    db.commit()

    checkpoint = db.scalar(select(TripProcessingCheckpoint))
    assert db.scalar(select(Trip)) is None
    assert checkpoint is not None
    assert checkpoint.odometer_anchor_miles == Decimal("1005.0")
    assert checkpoint.odometer_anchor_recorded_at == _naive(day + timedelta(minutes=24))


def test_delete_trip_does_not_move_newer_odometer_checkpoint_backward() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    trip = Trip(
        trip_date=day.date(),
        started_at=day,
        ended_at=day + timedelta(minutes=24),
        start_latitude=Decimal("42.3314"),
        start_longitude=Decimal("-83.0458"),
        end_latitude=Decimal("42.3440"),
        end_longitude=Decimal("-83.0600"),
        miles=Decimal("5.0"),
        start_odometer_miles=Decimal("1000.0"),
        end_odometer_miles=Decimal("1005.0"),
        source="auto",
    )
    db.add(trip)
    db.add(
        TripProcessingCheckpoint(
            name=AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
            odometer_anchor_miles=Decimal("1010.0"),
            odometer_anchor_recorded_at=day + timedelta(hours=1),
        )
    )
    db.commit()

    delete_trip(db, trip)
    db.commit()

    checkpoint = db.scalar(select(TripProcessingCheckpoint))
    assert db.scalar(select(Trip)) is None
    assert checkpoint is not None
    assert checkpoint.odometer_anchor_miles == Decimal("1010.0")
    assert checkpoint.odometer_anchor_recorded_at == _naive(day + timedelta(hours=1))


def test_generate_trips_estimates_transition_only_trip_from_checkpoint() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    db.add_all(
        [
            home,
            client,
            TripProcessingCheckpoint(
                name=AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
                odometer_anchor_miles=Decimal("1012.875"),
                odometer_anchor_recorded_at=day - timedelta(minutes=1),
            ),
            _transition(day, home, "leave"),
            _transition(day + timedelta(minutes=24), client, "enter"),
            _dwell_confirmation(day + timedelta(minutes=24), client),
        ]
    )
    db.commit()

    trips = generate_trips(db, day.date(), day.date())
    distance = haversine_miles(home.latitude, home.longitude, client.latitude, client.longitude)

    assert len(trips) == 1
    assert trips[0].miles == distance
    assert trips[0].start_odometer_miles == Decimal("1012.9")
    assert trips[0].end_odometer_miles == (Decimal("1012.9") + distance).quantize(
        Decimal("0.1")
    )
    assert trips[0].mileage_source == MILEAGE_SOURCE_WAYPOINT_DISTANCE
    assert trips[0].start_odometer_source == ODOMETER_SOURCE_PREVIOUS_TRIP
    assert trips[0].end_odometer_source == ODOMETER_SOURCE_ESTIMATED
    assert "Used waypoint distance because OwnTracks path data was unavailable." in trips[0].notes


def test_generate_trips_does_not_use_odometer_delta_for_distance() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    leave = _transition(day, home, "leave")
    enter = _transition(day + timedelta(minutes=24), client, "enter")
    leave.odometer_miles = Decimal("1000.0")
    leave.odometer_source = ODOMETER_SOURCE_OWNTRACKS_ROLLING
    enter.odometer_miles = Decimal("9000.0")
    enter.odometer_source = ODOMETER_SOURCE_OWNTRACKS_ROLLING
    db.add_all(
        [
            home,
            client,
            leave,
            enter,
            _dwell_confirmation(day + timedelta(minutes=24), client),
        ]
    )
    db.commit()

    trips = generate_trips(db, day.date(), day.date())
    distance = haversine_miles(home.latitude, home.longitude, client.latitude, client.longitude)

    assert len(trips) == 1
    assert trips[0].miles == distance
    assert trips[0].miles != Decimal("8000.0")
    assert trips[0].mileage_source == MILEAGE_SOURCE_WAYPOINT_DISTANCE
    assert trips[0].start_odometer_miles == Decimal("1000.0")
    assert trips[0].end_odometer_miles == (Decimal("1000.0") + distance).quantize(
        Decimal("0.1")
    )


def test_generate_trips_estimates_from_prior_odometer_anchor() -> None:
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
            miles=Decimal("20.0"),
            end_odometer_miles=Decimal("2000.0"),
            mileage_source=MILEAGE_SOURCE_ESTIMATED_ODOMETER,
            source="auto",
        )
    )
    db.add_all(
        [
            _transition(day, home, "leave"),
            _transition(day + timedelta(minutes=24), client, "enter"),
            _dwell_confirmation(day + timedelta(minutes=24), client),
        ]
    )
    db.commit()

    trips = generate_trips(db, day.date(), day.date())
    distance = haversine_miles(home.latitude, home.longitude, client.latitude, client.longitude)

    assert len(trips) == 1
    assert trips[0].miles == distance
    assert trips[0].start_odometer_miles == Decimal("2000.0")
    assert trips[0].end_odometer_miles == (Decimal("2000.0") + distance).quantize(
        Decimal("0.1")
    )
    assert trips[0].mileage_source == MILEAGE_SOURCE_WAYPOINT_DISTANCE
    assert trips[0].start_odometer_source == ODOMETER_SOURCE_PREVIOUS_TRIP
    assert trips[0].end_odometer_source == ODOMETER_SOURCE_ESTIMATED


def test_generate_trips_ignores_same_waypoint_trip_under_one_mile() -> None:
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
            _dwell_confirmation(day + timedelta(minutes=10), home),
            _transition(day + timedelta(hours=1), client, "leave"),
            _transition(day + timedelta(hours=2), client, "enter"),
            _dwell_confirmation(day + timedelta(hours=2), client),
        ]
    )
    db.commit()

    trips = generate_trips(db, day.date(), day.date())

    assert trips == []
    assert db.scalar(select(Trip.id)) is None


def test_generate_trips_allows_same_waypoint_trip_at_least_one_mile() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    client = _site("Client", "42.3440", "-83.0600")
    route_point_one = _location(day + timedelta(minutes=15), "42.3440", "-83.0800")
    route_point_two = _location(day + timedelta(minutes=30), "42.3520", "-83.0900")
    db.add_all(
        [
            client,
            _transition(day, client, "leave"),
            route_point_one,
            route_point_two,
            _transition(day + timedelta(hours=1), client, "enter"),
            _dwell_confirmation(day + timedelta(hours=1), client),
        ]
    )
    db.commit()

    trips = generate_trips(db, day.date(), day.date())

    assert len(trips) == 1
    assert trips[0].origin_site_id == client.id
    assert trips[0].destination_site_id == client.id
    assert trips[0].miles >= Decimal("1.0")
    assert trips[0].mileage_source == MILEAGE_SOURCE_OWNTRACKS_PATH


def test_generate_trips_removes_existing_same_waypoint_trip_under_one_mile() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    client = _site("Client", "42.3440", "-83.0600")
    db.add(client)
    db.flush()
    existing_trip = Trip(
        trip_date=day.date(),
        origin_site_id=client.id,
        destination_site_id=client.id,
        started_at=_naive(day),
        ended_at=_naive(day + timedelta(hours=1)),
        start_latitude=client.latitude,
        start_longitude=client.longitude,
        end_latitude=client.latitude,
        end_longitude=client.longitude,
        origin_name=client.name,
        destination_name=client.name,
        miles=Decimal("0.5"),
        mileage_source=MILEAGE_SOURCE_WAYPOINT_DISTANCE,
        source="auto",
    )
    db.add_all(
        [
            existing_trip,
            _transition(day, client, "leave"),
            _transition(day + timedelta(hours=1), client, "enter"),
            _dwell_confirmation(day + timedelta(hours=1), client),
        ]
    )
    db.commit()

    trips = generate_trips(db, day.date(), day.date())

    assert trips == []
    assert db.scalar(select(Trip.id)) is None
    deleted_trip = db.scalar(select(DeletedTrip))
    assert deleted_trip is not None
    assert deleted_trip.reason == "invalid_same_waypoint_under_one_mile"
    assert deleted_trip.origin_site_id == client.id
    assert deleted_trip.destination_site_id == client.id


def test_generate_trips_assumes_missing_first_leave_was_from_home() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    db.add_all(
        [
            home,
            client,
            _transition(day, client, "enter"),
            _dwell_confirmation(day, client),
        ]
    )
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
    db.add_all([home, _transition(day, home, "enter"), _dwell_confirmation(day, home)])
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
            _dwell_confirmation(day, client_a),
            _transition(day + timedelta(hours=2), client_b, "enter"),
            _dwell_confirmation(day + timedelta(hours=2), client_b),
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
            _dwell_confirmation(first_day + timedelta(minutes=20), client),
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
            _dwell_confirmation(second_day + timedelta(minutes=20), client),
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
    remaining_trips = list(db.scalars(select(Trip).order_by(Trip.trip_date.asc())))
    remaining_snapshots = list(db.scalars(select(GasPriceSnapshot)))
    assert result.location_points == 1
    assert result.trips == 0
    assert result.gas_snapshots == 1
    assert db.scalar(select(Site).where(Site.id == site.id)) is not None
    assert [location.captured_at for location in remaining_locations] == [_naive(current_month)]
    assert [trip.trip_date for trip in remaining_trips] == [
        date(2026, 5, 31),
        date(2026, 6, 1),
    ]
    assert [snapshot.observed_on for snapshot in remaining_snapshots] == [date(2026, 6, 1)]


def test_purge_processed_owntracks_locations_keeps_unprocessed_and_recent_rows() -> None:
    db = _session()
    current_time = datetime(2026, 6, 20, 13, 0, tzinfo=UTC)
    old_processed_location = _location(
        current_time - timedelta(days=20),
        "42.3314",
        "-83.0458",
    )
    old_unprocessed_location = _location(
        current_time - timedelta(days=19),
        "42.3440",
        "-83.0600",
    )
    recent_location = _location(
        current_time - timedelta(days=1),
        "42.3500",
        "-83.0700",
    )
    db.add_all([old_processed_location, old_unprocessed_location, recent_location])
    db.commit()

    result = purge_processed_owntracks_locations(
        db,
        checkpoint_location_id=old_processed_location.id,
        now=current_time,
        retention_days=14,
    )

    remaining_locations = list(
        db.scalars(select(OwnTracksLocation).order_by(OwnTracksLocation.id.asc()))
    )
    assert result.location_points == 1
    assert [location.id for location in remaining_locations] == [
        old_unprocessed_location.id,
        recent_location.id,
    ]


def test_automatic_trip_processing_finalizes_completed_days_without_early_purge() -> None:
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
            _dwell_confirmation(completed_day + timedelta(minutes=30), client),
            _location(current_day, "42.3500", "-83.0700"),
        ]
    )
    db.commit()

    result = run_automatic_trip_processing(db, now=current_day)

    trips = list(db.scalars(select(Trip).order_by(Trip.started_at.asc())))
    remaining_locations = list(db.scalars(select(OwnTracksLocation)))
    assert result.generated == 1
    assert result.retention.total == 0
    assert len(trips) == 1
    assert trips[0].trip_date == completed_day.date()
    assert [location.captured_at for location in remaining_locations] == [
        _naive(completed_day),
        _naive(completed_day + timedelta(minutes=30)),
        _naive(completed_day + timedelta(minutes=35)),
        _naive(current_day),
    ]


def test_automatic_trip_processing_uses_checkpoint_without_duplicate_trips() -> None:
    db = _session()
    day = datetime(2026, 6, 9, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    db.add_all(
        [
            home,
            client,
            _transition(day, home, "leave"),
            _transition(day + timedelta(minutes=30), client, "enter"),
            _dwell_confirmation(day + timedelta(minutes=30), client),
        ]
    )
    db.commit()

    first_result = run_automatic_trip_processing(db, now=day + timedelta(hours=1))
    second_result = run_automatic_trip_processing(db, now=day + timedelta(hours=2))

    trips = list(db.scalars(select(Trip).order_by(Trip.started_at.asc())))
    checkpoint = db.scalar(select(TripProcessingCheckpoint))
    assert first_result.processed_location_count == 3
    assert second_result.processed_location_count == 0
    assert len(trips) == 1
    assert checkpoint is not None
    latest_location_id = max(location.id for location in db.scalars(select(OwnTracksLocation)))
    assert checkpoint.last_owntracks_location_id == latest_location_id


def test_automatic_trip_processing_rechecks_pending_arrival_after_dwell() -> None:
    db = _session()
    day = datetime(2026, 6, 9, 13, 0, tzinfo=UTC)
    arrival_at = day + timedelta(minutes=24)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    db.add_all(
        [
            home,
            client,
            _transition(day, home, "leave"),
            _transition(arrival_at, client, "enter"),
        ]
    )
    db.commit()

    first_result = run_automatic_trip_processing(db, now=arrival_at + timedelta(minutes=1))
    second_result = run_automatic_trip_processing(db, now=arrival_at + timedelta(minutes=20))

    trips = list(db.scalars(select(Trip).order_by(Trip.started_at.asc())))
    checkpoint = db.scalar(select(TripProcessingCheckpoint))
    assert first_result.generated == 0
    assert first_result.processed_location_count == 2
    assert second_result.generated == 1
    assert second_result.processed_location_count == 0
    assert len(trips) == 1
    assert trips[0].origin_site_id == home.id
    assert trips[0].destination_site_id == client.id
    assert checkpoint is not None
    assert checkpoint.last_owntracks_location_id == max(
        location.id for location in db.scalars(select(OwnTracksLocation))
    )


def test_automatic_trip_processing_creates_missing_checkpoint_table() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TripProcessingCheckpoint.__table__.drop(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()

    result = run_automatic_trip_processing(db, now=datetime(2026, 6, 10, 13, 0, tzinfo=UTC))

    checkpoint = db.scalar(select(TripProcessingCheckpoint))
    assert result.generated == 0
    assert checkpoint is not None
    assert checkpoint.name == "automatic_trip_processing"


def test_automatic_trip_processing_saves_initial_zero_odometer_anchor() -> None:
    db = _session()
    current_time = datetime(2026, 6, 10, 13, 0, tzinfo=UTC)

    result = run_automatic_trip_processing(db, now=current_time)

    checkpoint = db.scalar(select(TripProcessingCheckpoint))
    assert result.generated == 0
    assert checkpoint is not None
    assert checkpoint.odometer_anchor_miles == Decimal("0.0")
    assert checkpoint.odometer_anchor_recorded_at == _naive(current_time)


def test_automatic_trip_processing_advances_odometer_without_trip() -> None:
    db = _session()
    day = datetime(2026, 6, 10, 13, 0, tzinfo=UTC)
    start_location = _location(day + timedelta(minutes=1), "42.3314", "-83.0458")
    end_location = _location(day + timedelta(minutes=20), "42.3440", "-83.0600")
    db.add_all(
        [
            TripProcessingCheckpoint(
                name=AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
                odometer_anchor_miles=Decimal("1000.000"),
                odometer_anchor_recorded_at=day,
            ),
            start_location,
            end_location,
        ]
    )
    db.commit()

    result = run_automatic_trip_processing(db, now=day + timedelta(hours=1))

    expected_distance = haversine_miles(
        start_location.latitude,
        start_location.longitude,
        end_location.latitude,
        end_location.longitude,
    ).quantize(Decimal("0.1"))
    checkpoint = db.scalar(select(TripProcessingCheckpoint))
    assert result.generated == 0
    assert checkpoint is not None
    assert checkpoint.odometer_anchor_miles == Decimal("1000.0") + expected_distance
    assert checkpoint.odometer_anchor_recorded_at == _naive(end_location.captured_at)
    assert start_location.odometer_miles == Decimal("1000.0")
    assert start_location.odometer_source == ODOMETER_SOURCE_OWNTRACKS_ROLLING
    assert end_location.odometer_miles == Decimal("1000.0") + expected_distance
    assert end_location.odometer_source == ODOMETER_SOURCE_OWNTRACKS_ROLLING


def test_automatic_trip_processing_uses_rolling_odometer_for_trip_values() -> None:
    db = _session()
    day = datetime(2026, 6, 10, 13, 0, tzinfo=UTC)
    home = _site("Home", "42.3314", "-83.0458")
    client = _site("Client", "42.3440", "-83.0600")
    first_route_point = _location(day + timedelta(minutes=5), "42.3314", "-83.0600")
    second_route_point = _location(day + timedelta(minutes=10), "42.3380", "-83.0700")
    db.add_all(
        [
            home,
            client,
            TripProcessingCheckpoint(
                name=AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
                odometer_anchor_miles=Decimal("1000.000"),
                odometer_anchor_recorded_at=day - timedelta(minutes=1),
            ),
            _transition(day, home, "leave"),
            first_route_point,
            second_route_point,
            _transition(day + timedelta(minutes=24), client, "enter"),
            _dwell_confirmation(day + timedelta(minutes=24), client),
        ]
    )
    db.commit()

    result = run_automatic_trip_processing(db, now=day + timedelta(hours=1))

    trip = db.scalar(select(Trip))
    checkpoint = db.scalar(select(TripProcessingCheckpoint))
    assert result.generated == 1
    assert trip is not None
    assert checkpoint is not None
    assert trip.mileage_source == MILEAGE_SOURCE_OWNTRACKS_PATH
    assert trip.start_odometer_miles == Decimal("1000.0")
    assert trip.end_odometer_miles == (trip.start_odometer_miles + trip.miles).quantize(
        Decimal("0.1")
    )
    assert trip.start_odometer_source == ODOMETER_SOURCE_OWNTRACKS_ROLLING
    assert trip.end_odometer_source == ODOMETER_SOURCE_OWNTRACKS_ROLLING
    assert checkpoint.odometer_anchor_miles >= trip.end_odometer_miles
    assert first_route_point.odometer_source == ODOMETER_SOURCE_OWNTRACKS_ROLLING
    assert second_route_point.odometer_source == ODOMETER_SOURCE_OWNTRACKS_ROLLING


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
            _dwell_confirmation(previous_day + timedelta(minutes=75), home),
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
    assert result.retention.total == 0
    assert len(trips) == 1
    assert trips[0].trip_date == previous_day.date()
    assert trips[0].origin_site.name == "Client"
    assert trips[0].destination_site.name == "Home"
    assert [location.captured_at for location in remaining_locations] == [
        _naive(previous_day),
        _naive(previous_day + timedelta(minutes=75)),
        _naive(previous_day + timedelta(minutes=80)),
    ]


def test_automatic_trip_processing_purges_old_processed_locations_and_keeps_trip() -> None:
    db = _session()
    previous_day = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    current_time = datetime(2026, 6, 20, 13, 0, tzinfo=UTC)
    client = _site("Client", "42.3440", "-83.0600")
    home = _site("Home", "42.3314", "-83.0458")
    db.add_all(
        [
            client,
            home,
            _transition(previous_day, home, "leave"),
            _location(previous_day + timedelta(minutes=30), "42.3380", "-83.0700"),
            _transition(previous_day + timedelta(minutes=75), client, "enter"),
            _dwell_confirmation(previous_day + timedelta(minutes=75), client),
            _location(current_time - timedelta(days=1), "42.3500", "-83.0700"),
        ]
    )
    db.commit()

    result = run_automatic_trip_processing(
        db,
        touched_date=date(2026, 6, 20),
        now=current_time,
    )

    trips = list(db.scalars(select(Trip).order_by(Trip.started_at.asc())))
    remaining_locations = list(
        db.scalars(select(OwnTracksLocation).order_by(OwnTracksLocation.captured_at.asc()))
    )
    assert result.generated == 1
    assert result.retention.location_points == 4
    assert result.retention.trips == 0
    assert len(trips) == 1
    assert trips[0].trip_date == previous_day.date()
    assert trips[0].mileage_source == MILEAGE_SOURCE_OWNTRACKS_PATH
    assert [location.captured_at for location in remaining_locations] == [
        _naive(current_time - timedelta(days=1))
    ]
    assert db.scalar(select(Site).where(Site.name == "Client")) is not None
    assert db.scalar(select(Site).where(Site.name == "Home")) is not None
