from datetime import UTC, date, datetime

from mileage_logger.services.timezone import datetime_to_local, datetime_to_local_date


def test_datetime_to_local_uses_configured_detroit_timezone() -> None:
    utc_time = datetime(2026, 6, 12, 1, 30, tzinfo=UTC)

    local_time = datetime_to_local(utc_time)

    assert local_time.date() == date(2026, 6, 11)
    assert local_time.strftime("%H:%M %Z") == "21:30 EDT"
    assert datetime_to_local_date(utc_time) == date(2026, 6, 11)
