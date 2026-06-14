import hashlib
import hmac
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from mileage_logger.app import app
from mileage_logger.config import Settings
from mileage_logger.database import get_db
from mileage_logger.models import Base, SmartcarWebhookEvent, SmartcarWebhookSignal
from mileage_logger.services.smartcar import (
    hash_webhook_challenge,
    latest_webhook_odometer_miles,
    store_webhook_payload,
    verify_webhook_signature,
)


def _client_session() -> tuple[TestClient, sessionmaker[Session]]:
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


def _signed_headers(raw_body: bytes, token: str) -> dict[str, str]:
    signature = hmac.new(token.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "SC-Signature": signature}


def _vehicle_state_payload(event_id: str = "event-1") -> dict[str, Any]:
    timestamp = 1781417307128
    return {
        "eventId": event_id,
        "eventType": "VEHICLE_STATE",
        "data": {
            "user": {"id": "user-1"},
            "vehicle": {
                "id": "vehicle-1",
                "make": "Tesla",
                "model": "Model 3",
                "year": 2020,
                "mode": "test",
                "powertrainType": "BEV",
            },
            "signals": [
                {
                    "code": "closure-islocked",
                    "name": "IsLocked",
                    "group": "Closure",
                    "body": {"value": True},
                    "status": {"value": "SUCCESS"},
                    "meta": {"oemUpdatedAt": timestamp, "retrievedAt": timestamp},
                },
                {
                    "code": "connectivitystatus-isonline",
                    "name": "IsOnline",
                    "group": "ConnectivityStatus",
                    "body": {"value": True},
                    "status": {"value": "SUCCESS"},
                    "meta": {"oemUpdatedAt": timestamp, "retrievedAt": timestamp},
                },
                {
                    "code": "internalcombustionengine-fuellevel",
                    "name": "FuelLevel",
                    "group": "InternalCombustionEngine",
                    "body": {"value": 65, "unit": "percent"},
                    "status": {"value": "SUCCESS"},
                    "meta": {"oemUpdatedAt": timestamp, "retrievedAt": timestamp},
                },
                {
                    "code": "vehicleidentification-nickname",
                    "name": "Nickname",
                    "group": "VehicleIdentification",
                    "body": {"value": "QueenLiz"},
                    "status": {"value": "SUCCESS"},
                    "meta": {"oemUpdatedAt": timestamp, "retrievedAt": timestamp},
                },
                {
                    "code": "vehicleidentification-vin",
                    "name": "VIN",
                    "group": "VehicleIdentification",
                    "body": {"value": "5YJSA1CN5DFP00101"},
                    "status": {"value": "SUCCESS"},
                    "meta": {"oemUpdatedAt": 0, "retrievedAt": 0},
                },
                {
                    "code": "odometer-traveleddistance",
                    "name": "TraveledDistance",
                    "group": "Odometer",
                    "body": {"value": 78432, "unit": "km"},
                    "status": {"value": "SUCCESS"},
                    "meta": {"oemUpdatedAt": timestamp, "retrievedAt": timestamp},
                },
            ],
        },
        "triggers": [
            {
                "type": "SIGNAL_UPDATED",
                "signal": {
                    "name": "TraveledDistance",
                    "code": "odometer-traveleddistance",
                    "group": "Odometer",
                },
            }
        ],
        "meta": {
            "version": "4.0",
            "webhookId": "webhook-1",
            "webhookName": "Mileage Log",
            "deliveryId": f"delivery-{event_id}",
            "deliveredAt": timestamp,
            "mode": "TEST",
            "signalCount": 6,
        },
    }


def test_hash_webhook_challenge_uses_management_token_hmac() -> None:
    token = "management-token"
    challenge = "challenge_s4mpleR4nd0mStr1ng"
    expected = hmac.new(token.encode("utf-8"), challenge.encode("utf-8"), hashlib.sha256)

    assert hash_webhook_challenge(token, challenge) == expected.hexdigest()


def test_verify_webhook_signature_accepts_plain_or_prefixed_signature() -> None:
    token = "management-token"
    raw_body = b'{"eventType":"VEHICLE_STATE"}'
    signature = hmac.new(token.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    assert verify_webhook_signature(raw_body, signature, token) is True
    assert verify_webhook_signature(raw_body, f"sha256={signature}", token) is True
    assert verify_webhook_signature(raw_body, "bad-signature", token) is False


def test_store_webhook_payload_saves_event_and_signal_rows() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    settings = Settings(smartcar_enabled=True, smartcar_management_token="management-token")
    payload = _vehicle_state_payload()

    result = store_webhook_payload(db, payload, settings=settings)

    stored_event = db.scalar(select(SmartcarWebhookEvent))
    stored_signals = list(db.scalars(select(SmartcarWebhookSignal)))
    expected_miles = (Decimal("78432") / Decimal("1.609344")).quantize(Decimal("0.001"))
    assert result.created is True
    assert stored_event is not None
    assert stored_event.event_id == "event-1"
    assert stored_event.vehicle_id == "vehicle-1"
    assert stored_event.vin == "5YJSA1CN5DFP00101"
    assert stored_event.nickname == "QueenLiz"
    assert stored_event.fuel_percent == Decimal("65.00")
    assert stored_event.is_locked is True
    assert stored_event.is_online is True
    assert stored_event.odometer_miles == expected_miles
    assert latest_webhook_odometer_miles(db) == expected_miles
    assert len(stored_signals) == 6


def test_store_webhook_payload_is_idempotent_by_event_id() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    settings = Settings(smartcar_enabled=True, smartcar_management_token="management-token")
    payload = _vehicle_state_payload()

    first_result = store_webhook_payload(db, payload, settings=settings)
    second_result = store_webhook_payload(db, payload, settings=settings)

    assert first_result.created is True
    assert second_result.created is False
    assert len(list(db.scalars(select(SmartcarWebhookEvent)))) == 1


def test_smartcar_webhook_verify_route_returns_challenge(monkeypatch) -> None:
    token = "management-token"
    client, _session_factory = _client_session()
    monkeypatch.setattr(
        "mileage_logger.api.routes.get_settings",
        lambda: Settings(smartcar_enabled=True, smartcar_management_token=token),
    )
    try:
        payload = {
            "eventId": "verify-1",
            "eventType": "VERIFY",
            "data": {"challenge": "challenge_s4mpleR4nd0mStr1ng"},
            "meta": {"version": "4.0"},
        }
        response = client.post("/api/smartcar/webhook", json=payload)

        assert response.status_code == 200
        assert response.json() == {
            "challenge": hash_webhook_challenge(token, "challenge_s4mpleR4nd0mStr1ng")
        }
    finally:
        app.dependency_overrides.clear()


def test_smartcar_webhook_route_rejects_unsigned_vehicle_state(monkeypatch) -> None:
    client, _session_factory = _client_session()
    monkeypatch.setattr(
        "mileage_logger.api.routes.get_settings",
        lambda: Settings(smartcar_enabled=True, smartcar_management_token="management-token"),
    )
    try:
        response = client.post("/api/smartcar/webhook", json=_vehicle_state_payload())

        assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_smartcar_webhook_route_stores_signed_vehicle_state(monkeypatch) -> None:
    token = "management-token"
    client, session_factory = _client_session()
    monkeypatch.setattr(
        "mileage_logger.api.routes.get_settings",
        lambda: Settings(smartcar_enabled=True, smartcar_management_token=token),
    )
    monkeypatch.setattr(
        "mileage_logger.api.routes.run_automatic_trip_processing",
        lambda *_args, **_kwargs: None,
    )
    try:
        raw_body = json.dumps(_vehicle_state_payload()).encode("utf-8")
        response = client.post(
            "/api/smartcar/webhook",
            content=raw_body,
            headers=_signed_headers(raw_body, token),
        )

        assert response.status_code == 200
        assert response.json()["created"] is True
        with session_factory() as db:
            stored_event = db.scalar(select(SmartcarWebhookEvent))
            assert stored_event is not None
            assert stored_event.vehicle_make == "Tesla"
            assert stored_event.odometer_recorded_at is not None
            assert stored_event.odometer_recorded_at.replace(tzinfo=UTC) == datetime.fromtimestamp(
                1781417307128 / 1000,
                tz=UTC,
            )
    finally:
        app.dependency_overrides.clear()
