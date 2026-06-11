from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from mileage_logger.models import Site, Trip
from mileage_logger.services.pdf import (
    calculate_reimbursement,
    calculate_reimbursement_gallons,
    trip_report_rows,
)


def test_calculate_reimbursement_uses_requested_formula() -> None:
    assert calculate_reimbursement_gallons(Decimal("120.50"), Decimal("25.0")) == Decimal("4.820")
    assert calculate_reimbursement(
        Decimal("120.50"),
        Decimal("4.250"),
        Decimal("25.0"),
    ) == Decimal("20.49")


def test_trip_report_rows_include_trip_mileage() -> None:
    origin = Site(
        name="Shop",
        latitude=Decimal("42.3314"),
        longitude=Decimal("-83.0458"),
        radius_m=150,
    )
    client = Site(
        name="Client",
        latitude=Decimal("42.3440"),
        longitude=Decimal("-83.0600"),
        radius_m=150,
    )
    started_at = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    trips = [
        Trip(
            trip_date=date(2026, 6, 11),
            origin_site=origin,
            destination_site=client,
            started_at=started_at,
            ended_at=started_at + timedelta(minutes=20),
            start_latitude=origin.latitude,
            start_longitude=origin.longitude,
            end_latitude=client.latitude,
            end_longitude=client.longitude,
            miles=Decimal("12.50"),
        ),
        Trip(
            trip_date=date(2026, 6, 11),
            origin_site=client,
            destination_site=origin,
            started_at=started_at + timedelta(hours=2),
            ended_at=started_at + timedelta(hours=2, minutes=20),
            start_latitude=client.latitude,
            start_longitude=client.longitude,
            end_latitude=origin.latitude,
            end_longitude=origin.longitude,
            miles=Decimal("7.25"),
        ),
    ]

    rows = trip_report_rows(trips)

    assert rows[0].from_location == "Shop"
    assert rows[0].to_location == "Client"
    assert rows[0].trip_miles == Decimal("12.50")
    assert rows[1].from_location == "Client"
    assert rows[1].to_location == "Shop"
    assert rows[1].trip_miles == Decimal("7.25")


def test_trip_report_rows_use_unknown_for_unresolved_sites() -> None:
    started_at = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    trip = Trip(
        trip_date=date(2026, 6, 11),
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=20),
        start_latitude=Decimal("42.3314"),
        start_longitude=Decimal("-83.0458"),
        end_latitude=Decimal("42.3440"),
        end_longitude=Decimal("-83.0600"),
        miles=Decimal("12.50"),
    )

    rows = trip_report_rows([trip])

    assert rows[0].from_location == "Unknown"
    assert rows[0].to_location == "Unknown"


def test_trip_report_rows_use_trip_location_name_overrides() -> None:
    site = Site(
        name="Original Site",
        latitude=Decimal("42.3314"),
        longitude=Decimal("-83.0458"),
        radius_m=150,
    )
    started_at = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    trip = Trip(
        trip_date=date(2026, 6, 11),
        origin_site=site,
        destination_site=site,
        origin_name="Edited Start",
        destination_name="Edited End",
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=20),
        start_latitude=Decimal("42.3314"),
        start_longitude=Decimal("-83.0458"),
        end_latitude=Decimal("42.3440"),
        end_longitude=Decimal("-83.0600"),
        miles=Decimal("12.50"),
    )

    rows = trip_report_rows([trip])

    assert rows[0].from_location == "Edited Start"
    assert rows[0].to_location == "Edited End"
