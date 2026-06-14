import logging
from decimal import Decimal
from typing import Any

import httpx
import pytest

from mileage_logger.config import Settings
from mileage_logger.services.smartcar import (
    SmartcarAuthenticationError,
    SmartcarOdometerError,
    current_odometer_miles,
    odometer_miles_from_response,
)


class FakeSmartcarResponse:
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.request = httpx.Request("GET", "https://api.smartcar.test")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = httpx.Response(self.status_code, request=self.request)
            raise httpx.HTTPStatusError(
                "Smartcar request failed",
                request=self.request,
                response=response,
            )

    def json(self) -> dict[str, object]:
        return self.payload


def test_odometer_miles_from_response_reads_distance_and_converts_km() -> None:
    response_data = {"distance": 160.9344}

    assert odometer_miles_from_response(response_data) == Decimal("100.000")


def test_odometer_miles_from_response_accepts_signal_shape_miles() -> None:
    response_data = {"odometer": {"value": "12345.678", "unit": "mi"}}

    assert odometer_miles_from_response(response_data) == Decimal("12345.678")


def test_odometer_miles_from_response_rejects_missing_value() -> None:
    with pytest.raises(SmartcarOdometerError):
        odometer_miles_from_response({"fuel": {"value": 50}})


def test_current_odometer_uses_configured_vehicle_id(monkeypatch) -> None:
    calls: list[str] = []

    def fake_get(url: str, **_: Any) -> FakeSmartcarResponse:
        calls.append(url)
        return FakeSmartcarResponse({"distance": 160.9344})

    settings = Settings(
        smartcar_enabled=True,
        smartcar_api_polling_enabled=True,
        smartcar_access_token="configured-token",
        smartcar_vehicle_id="vehicle-123",
        smartcar_api_base_url="https://api.smartcar.test/v2.0",
        smartcar_retry_delay_seconds=0,
    )
    monkeypatch.setattr("mileage_logger.services.smartcar.httpx.get", fake_get)

    assert current_odometer_miles(settings) == Decimal("100.000")
    assert calls == ["https://api.smartcar.test/v2.0/vehicles/vehicle-123/odometer"]


def test_current_odometer_fetches_client_credentials_token(monkeypatch) -> None:
    post_calls: list[dict[str, object]] = []
    get_headers: list[dict[str, str]] = []

    def fake_post(url: str, **kwargs: Any) -> FakeSmartcarResponse:
        post_calls.append({"url": url, **kwargs})
        return FakeSmartcarResponse({"access_token": "fetched-token", "expires_in": 3600})

    def fake_get(_url: str, **kwargs: Any) -> FakeSmartcarResponse:
        get_headers.append(kwargs["headers"])
        return FakeSmartcarResponse({"distance": 160.9344})

    settings = Settings(
        smartcar_enabled=True,
        smartcar_api_polling_enabled=True,
        smartcar_client_id="client-id",
        smartcar_client_secret="client-secret",
        smartcar_vehicle_id="vehicle-123",
        smartcar_api_base_url="https://api.smartcar.test/v2.0",
        smartcar_retry_delay_seconds=0,
    )
    monkeypatch.setattr("mileage_logger.services.smartcar.httpx.post", fake_post)
    monkeypatch.setattr("mileage_logger.services.smartcar.httpx.get", fake_get)

    assert current_odometer_miles(settings) == Decimal("100.000")
    assert post_calls[0]["url"] == "https://iam.smartcar.com/oauth2/token"
    assert post_calls[0]["data"]["grant_type"] == "client_credentials"
    assert post_calls[0]["data"]["scope"] == "read_odometer"
    assert get_headers == [{"Accept": "application/json", "Authorization": "Bearer fetched-token"}]


def test_current_odometer_auto_detects_vehicle_id(monkeypatch) -> None:
    calls: list[str] = []

    def fake_get(url: str, **_: Any) -> FakeSmartcarResponse:
        calls.append(url)
        if url.endswith("/vehicles"):
            return FakeSmartcarResponse({"vehicles": ["vehicle-abc"]})
        return FakeSmartcarResponse({"distance": 321.8688})

    settings = Settings(
        smartcar_enabled=True,
        smartcar_api_polling_enabled=True,
        smartcar_access_token="auto-detect-token",
        smartcar_api_base_url="https://api.smartcar.test/v2.0",
        smartcar_retry_delay_seconds=0,
    )
    monkeypatch.setattr("mileage_logger.services.smartcar.httpx.get", fake_get)

    assert current_odometer_miles(settings) == Decimal("200.000")
    assert calls == [
        "https://api.smartcar.test/v2.0/vehicles",
        "https://api.smartcar.test/v2.0/vehicles/vehicle-abc/odometer",
    ]


def test_current_odometer_auth_failure_does_not_retry_or_repeat(
    monkeypatch,
    caplog,
) -> None:
    calls: list[str] = []

    def fake_get(url: str, **_: Any) -> FakeSmartcarResponse:
        calls.append(url)
        return FakeSmartcarResponse({"error": "unauthorized"}, status_code=401)

    settings = Settings(
        smartcar_enabled=True,
        smartcar_api_polling_enabled=True,
        smartcar_access_token="auth-failure-token",
        smartcar_vehicle_id="vehicle-401",
        smartcar_retry_attempts=3,
        smartcar_retry_delay_seconds=0,
        smartcar_auth_failure_cooldown_seconds=3600,
    )
    monkeypatch.setattr("mileage_logger.services.smartcar.httpx.get", fake_get)

    with caplog.at_level(logging.WARNING):
        assert current_odometer_miles(settings) is None

    assert calls == ["https://api.smartcar.com/v2.0/vehicles/vehicle-401/odometer"]
    assert "authentication failed" in caplog.text

    caplog.clear()
    assert current_odometer_miles(settings) is None

    assert calls == ["https://api.smartcar.com/v2.0/vehicles/vehicle-401/odometer"]
    assert "authentication failed" not in caplog.text


def test_current_odometer_force_can_raise_auth_failure(monkeypatch) -> None:
    def fake_get(_url: str, **_: Any) -> FakeSmartcarResponse:
        return FakeSmartcarResponse({"error": "unauthorized"}, status_code=401)

    settings = Settings(
        smartcar_enabled=True,
        smartcar_api_polling_enabled=True,
        smartcar_access_token="force-auth-failure-token",
        smartcar_vehicle_id="vehicle-401",
        smartcar_retry_attempts=3,
        smartcar_retry_delay_seconds=0,
    )
    monkeypatch.setattr("mileage_logger.services.smartcar.httpx.get", fake_get)

    with pytest.raises(SmartcarAuthenticationError):
        current_odometer_miles(settings, force=True, raise_on_auth_error=True)


def test_current_odometer_skips_api_polling_until_enabled(monkeypatch) -> None:
    def fail_get(_url: str, **_: Any) -> FakeSmartcarResponse:
        raise AssertionError("Smartcar API polling should not run by default")

    settings = Settings(
        smartcar_enabled=True,
        smartcar_access_token="api-token",
        smartcar_vehicle_id="vehicle-123",
    )
    monkeypatch.setattr("mileage_logger.services.smartcar.httpx.get", fail_get)
    monkeypatch.setattr(
        "mileage_logger.services.smartcar.latest_webhook_odometer_miles",
        lambda: None,
    )

    assert current_odometer_miles(settings) is None
