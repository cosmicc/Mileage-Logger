from decimal import Decimal

import pytest

from mileage_logger.services.fordpass import FordPassOdometerError, odometer_miles_from_status


def test_odometer_miles_from_status_reads_value_object_and_converts_km() -> None:
    status = {"odometer": {"value": 160.9344, "unit": "km"}}

    assert odometer_miles_from_status(status) == Decimal("100.000")


def test_odometer_miles_from_status_uses_default_unit() -> None:
    status = {"vehiclestatus": {"odometer": {"value": 160.9344}}}

    assert odometer_miles_from_status(status, default_unit="km") == Decimal("100.000")


def test_odometer_miles_from_status_accepts_miles_unit() -> None:
    status = {"odometer": {"value": "12345.678", "unit": "mi"}}

    assert odometer_miles_from_status(status) == Decimal("12345.678")


def test_odometer_miles_from_status_rejects_missing_value() -> None:
    with pytest.raises(FordPassOdometerError):
        odometer_miles_from_status({"fuel": {"value": 50}})
