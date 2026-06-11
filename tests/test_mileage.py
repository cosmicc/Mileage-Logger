from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from mileage_logger.models import Base, OwnTracksLocation, Site, Trip
from mileage_logger.services.mileage import (
    FALSE_STOP_MERGED_SOURCE,
    generate_trips,
    haversine_miles,
    mark_trip_manually_reviewed,
    merge_false_stop_into_next_trip,
    purge_processed_owntracks_locations,
    update_trip_location_names,
)
from mileage_logger.services.trip_processor import run_automatic_trip_processing


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
    assert trips[0].origin_display_name == "Client A"
    assert trips[0].destination_display_name == "Client B"
    assert trips[0].started_at == (day + timedelta(minutes=12)).replace(tzinfo=None)
    assert trips[0].ended_at == (day + timedelta(minutes=25)).replace(tzinfo=None)


def test_generate_trips_includes_return_to_single_point_final_waypoint() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    home = Site(
        name="Home",
        latitude=Decimal("42.3314"),
        longitude=Decimal("-83.0458"),
        radius_m=120,
    )
    client = Site(
        name="Client",
        latitude=Decimal("42.3440"),
        longitude=Decimal("-83.0600"),
        radius_m=120,
    )
    db.add_all(
        [
            home,
            client,
            _location(day, "42.3314", "-83.0458"),
            _location(day + timedelta(minutes=12), "42.3315", "-83.0459"),
            _location(day + timedelta(minutes=30), "42.3440", "-83.0600"),
            _location(day + timedelta(minutes=45), "42.3441", "-83.0601"),
            _location(day + timedelta(minutes=75), "42.3314", "-83.0458"),
        ]
    )
    db.commit()

    trips = generate_trips(
        db,
        day.date(),
        day.date(),
        as_of=day + timedelta(minutes=90),
    )

    assert len(trips) == 2
    assert trips[0].origin_site_id == home.id
    assert trips[0].destination_site_id == client.id
    assert trips[1].origin_site_id == client.id
    assert trips[1].destination_site_id == home.id
    assert trips[1].ended_at == (day + timedelta(minutes=75)).replace(tzinfo=None)


def test_generate_trips_allows_trip_across_midnight() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 23, 0, tzinfo=UTC)
    client = Site(
        name="Client",
        latitude=Decimal("42.3440"),
        longitude=Decimal("-83.0600"),
        radius_m=120,
    )
    home = Site(
        name="Home",
        latitude=Decimal("42.3314"),
        longitude=Decimal("-83.0458"),
        radius_m=120,
    )
    db.add_all(
        [
            client,
            home,
            _location(day, "42.3440", "-83.0600"),
            _location(day + timedelta(minutes=15), "42.3441", "-83.0601"),
            _location(day + timedelta(minutes=75), "42.3314", "-83.0458"),
        ]
    )
    db.commit()

    trips = generate_trips(
        db,
        day.date(),
        (day + timedelta(days=1)).date(),
        as_of=day + timedelta(minutes=90),
    )

    assert len(trips) == 1
    assert trips[0].trip_date == day.date()
    assert trips[0].origin_site_id == client.id
    assert trips[0].destination_site_id == home.id


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

    assert (
        generate_trips(
            db,
            day.date(),
            day.date(),
            as_of=day + timedelta(minutes=34),
        )
        == []
    )


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


def test_generate_trips_does_not_treat_single_unknown_final_point_as_stop() -> None:
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
        ]
    )
    db.commit()

    assert (
        generate_trips(
            db,
            day.date(),
            day.date(),
            as_of=day + timedelta(hours=2),
        )
        == []
    )


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


def test_generate_trips_does_not_delete_existing_auto_trips_without_source_locations() -> None:
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


def test_purge_processed_owntracks_locations_only_deletes_completed_days() -> None:
    db = _session()
    completed_day = datetime(2026, 6, 9, 13, 0, tzinfo=UTC)
    current_day = datetime(2026, 6, 10, 13, 0, tzinfo=UTC)
    db.add_all(
        [
            _location(completed_day, "42.3314", "-83.0458"),
            _location(completed_day + timedelta(minutes=30), "42.3440", "-83.0600"),
            _location(current_day, "42.3500", "-83.0700"),
        ]
    )
    db.commit()

    purged = purge_processed_owntracks_locations(
        db,
        completed_day.date(),
        current_day.date(),
        now=current_day,
    )

    remaining_locations = list(db.scalars(select(OwnTracksLocation)))
    assert purged == 2
    assert [location.captured_at for location in remaining_locations] == [
        current_day.replace(tzinfo=None)
    ]


def test_automatic_trip_processing_finalizes_and_purges_completed_days() -> None:
    db = _session()
    completed_day = datetime(2026, 6, 9, 13, 0, tzinfo=UTC)
    current_day = datetime(2026, 6, 10, 13, 0, tzinfo=UTC)
    db.add_all(
        [
            Site(
                name="Client A",
                latitude=Decimal("42.3314"),
                longitude=Decimal("-83.0458"),
                radius_m=120,
            ),
            Site(
                name="Client B",
                latitude=Decimal("42.3440"),
                longitude=Decimal("-83.0600"),
                radius_m=120,
            ),
            _location(completed_day, "42.3314", "-83.0458"),
            _location(completed_day + timedelta(minutes=12), "42.3315", "-83.0459"),
            _location(completed_day + timedelta(minutes=18), "42.3370", "-83.0520"),
            _location(completed_day + timedelta(minutes=25), "42.3440", "-83.0600"),
            _location(completed_day + timedelta(minutes=38), "42.3441", "-83.0601"),
            _location(current_day, "42.3500", "-83.0700"),
        ]
    )
    db.commit()

    result = run_automatic_trip_processing(db, now=current_day)

    trips = list(db.scalars(select(Trip).order_by(Trip.started_at.asc())))
    remaining_locations = list(db.scalars(select(OwnTracksLocation)))
    assert result.generated == 1
    assert result.purged_owntracks == 5
    assert len(trips) == 1
    assert trips[0].trip_date == completed_day.date()
    assert [location.captured_at for location in remaining_locations] == [
        current_day.replace(tzinfo=None)
    ]


def test_automatic_trip_processing_handles_midnight_return_before_purge() -> None:
    db = _session()
    previous_day = datetime(2026, 6, 11, 23, 0, tzinfo=UTC)
    current_day = datetime(2026, 6, 12, 0, 30, tzinfo=UTC)
    db.add_all(
        [
            Site(
                name="Client",
                latitude=Decimal("42.3440"),
                longitude=Decimal("-83.0600"),
                radius_m=120,
            ),
            Site(
                name="Home",
                latitude=Decimal("42.3314"),
                longitude=Decimal("-83.0458"),
                radius_m=120,
            ),
            _location(previous_day, "42.3440", "-83.0600"),
            _location(previous_day + timedelta(minutes=15), "42.3441", "-83.0601"),
            _location(previous_day + timedelta(minutes=75), "42.3314", "-83.0458"),
        ]
    )
    db.commit()

    result = run_automatic_trip_processing(
        db,
        touched_date=current_day.date(),
        now=current_day,
    )

    trips = list(db.scalars(select(Trip).order_by(Trip.started_at.asc())))
    remaining_locations = list(db.scalars(select(OwnTracksLocation)))
    assert result.generated == 1
    assert result.purged_owntracks == 2
    assert len(trips) == 1
    assert trips[0].trip_date == previous_day.date()
    assert trips[0].origin_site.name == "Client"
    assert trips[0].destination_site.name == "Home"
    assert [location.captured_at for location in remaining_locations] == [
        (previous_day + timedelta(minutes=75)).replace(tzinfo=None)
    ]


def test_merge_false_stop_into_next_trip_adds_miles_and_removes_stop() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    client_a = Site(
        name="Client A",
        latitude=Decimal("42.3314"),
        longitude=Decimal("-83.0458"),
        radius_m=120,
    )
    false_stop = Site(
        name="False Stop",
        latitude=Decimal("42.3370"),
        longitude=Decimal("-83.0520"),
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
            false_stop,
            client_b,
            _location(day, "42.3314", "-83.0458"),
            _location(day + timedelta(minutes=12), "42.3315", "-83.0459"),
            _location(day + timedelta(minutes=25), "42.3370", "-83.0520"),
            _location(day + timedelta(minutes=38), "42.3371", "-83.0521"),
            _location(day + timedelta(minutes=50), "42.3440", "-83.0600"),
            _location(day + timedelta(minutes=63), "42.3441", "-83.0601"),
        ]
    )
    db.commit()
    trips = generate_trips(db, day.date(), day.date())
    first_trip, second_trip = trips
    expected_miles = first_trip.miles + second_trip.miles

    merged_trip = merge_false_stop_into_next_trip(db, first_trip.id)

    remaining_trips = list(db.scalars(select(Trip).order_by(Trip.started_at.asc())))
    assert remaining_trips == [merged_trip]
    assert merged_trip.origin_site_id == client_a.id
    assert merged_trip.destination_site_id == client_b.id
    assert merged_trip.started_at == first_trip.started_at
    assert merged_trip.ended_at == second_trip.ended_at
    assert merged_trip.miles == expected_miles
    assert merged_trip.include_in_report is True
    assert merged_trip.source == FALSE_STOP_MERGED_SOURCE
    assert "Merged false stop at False Stop" in merged_trip.notes


def test_false_stop_merge_is_preserved_when_trips_regenerate() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    db.add_all(
        [
            Site(
                name="Client A",
                latitude=Decimal("42.3314"),
                longitude=Decimal("-83.0458"),
                radius_m=120,
            ),
            Site(
                name="False Stop",
                latitude=Decimal("42.3370"),
                longitude=Decimal("-83.0520"),
                radius_m=120,
            ),
            Site(
                name="Client B",
                latitude=Decimal("42.3440"),
                longitude=Decimal("-83.0600"),
                radius_m=120,
            ),
            _location(day, "42.3314", "-83.0458"),
            _location(day + timedelta(minutes=12), "42.3315", "-83.0459"),
            _location(day + timedelta(minutes=25), "42.3370", "-83.0520"),
            _location(day + timedelta(minutes=38), "42.3371", "-83.0521"),
            _location(day + timedelta(minutes=50), "42.3440", "-83.0600"),
            _location(day + timedelta(minutes=63), "42.3441", "-83.0601"),
        ]
    )
    db.commit()
    trips = generate_trips(db, day.date(), day.date())
    merged_trip = merge_false_stop_into_next_trip(db, trips[0].id)

    regenerated = generate_trips(db, day.date(), day.date())

    remaining_trips = list(db.scalars(select(Trip).order_by(Trip.started_at.asc())))
    assert regenerated == []
    assert remaining_trips == [merged_trip]
    assert remaining_trips[0].source == FALSE_STOP_MERGED_SOURCE


def test_manually_reviewed_trip_is_preserved_when_trips_regenerate() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    db.add_all(
        [
            Site(
                name="Client A",
                latitude=Decimal("42.3314"),
                longitude=Decimal("-83.0458"),
                radius_m=120,
            ),
            Site(
                name="Client B",
                latitude=Decimal("42.3440"),
                longitude=Decimal("-83.0600"),
                radius_m=120,
            ),
            _location(day, "42.3314", "-83.0458"),
            _location(day + timedelta(minutes=12), "42.3315", "-83.0459"),
            _location(day + timedelta(minutes=25), "42.3440", "-83.0600"),
            _location(day + timedelta(minutes=38), "42.3441", "-83.0601"),
        ]
    )
    db.commit()
    trip = generate_trips(db, day.date(), day.date())[0]
    trip.include_in_report = False
    trip.notes = "Personal errand."
    mark_trip_manually_reviewed(trip)
    db.commit()

    regenerated = generate_trips(db, day.date(), day.date())

    remaining_trips = list(db.scalars(select(Trip).order_by(Trip.started_at.asc())))
    assert regenerated == []
    assert remaining_trips == [trip]
    assert remaining_trips[0].include_in_report is False
    assert remaining_trips[0].notes == "Personal errand."


def test_edited_trip_location_names_are_preserved_when_trips_regenerate() -> None:
    db = _session()
    day = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    db.add_all(
        [
            Site(
                name="Client A",
                latitude=Decimal("42.3314"),
                longitude=Decimal("-83.0458"),
                radius_m=120,
            ),
            Site(
                name="Client B",
                latitude=Decimal("42.3440"),
                longitude=Decimal("-83.0600"),
                radius_m=120,
            ),
            _location(day, "42.3314", "-83.0458"),
            _location(day + timedelta(minutes=12), "42.3315", "-83.0459"),
            _location(day + timedelta(minutes=25), "42.3440", "-83.0600"),
            _location(day + timedelta(minutes=38), "42.3441", "-83.0601"),
        ]
    )
    db.commit()
    trip = generate_trips(db, day.date(), day.date())[0]
    update_trip_location_names(trip, "Edited Start", "Edited End")
    db.commit()

    regenerated = generate_trips(db, day.date(), day.date())

    remaining_trips = list(db.scalars(select(Trip).order_by(Trip.started_at.asc())))
    assert regenerated == []
    assert remaining_trips == [trip]
    assert remaining_trips[0].origin_display_name == "Edited Start"
    assert remaining_trips[0].destination_display_name == "Edited End"
