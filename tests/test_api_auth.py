import base64
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from nacl.secret import SecretBox
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from mileage_logger.api.routes import get_owntracks_session_factory
from mileage_logger.app import app
from mileage_logger.config import Settings
from mileage_logger.database import get_db
from mileage_logger.models import Base, OwnTracksLocation
from mileage_logger.services.owntracks_buffer import OwnTracksIngestOutcome


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
    app.dependency_overrides[get_owntracks_session_factory] = lambda: session_factory
    return TestClient(app), session_factory


def _patch_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    for module_name in (
        "mileage_logger.api.deps",
        "mileage_logger.api.routes",
        "mileage_logger.app",
        "mileage_logger.services.mileage",
        "mileage_logger.services.owntracks",
        "mileage_logger.services.owntracks_buffer",
        "mileage_logger.services.trip_processor",
        "mileage_logger.web.auth",
    ):
        monkeypatch.setattr(f"{module_name}.get_settings", lambda: settings, raising=False)


def _owntracks_secretbox_key(secret: str) -> bytes:
    return secret.encode("utf-8").ljust(SecretBox.KEY_SIZE, b"\0")


def _encrypted_owntracks_payload(payload: dict, secret: str) -> dict:
    encrypted = SecretBox(_owntracks_secretbox_key(secret)).encrypt(
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        nonce=b"\0" * SecretBox.NONCE_SIZE,
    )
    return {
        "_type": "encrypted",
        "data": base64.b64encode(bytes(encrypted)).decode("ascii"),
    }


def test_owntracks_endpoint_requires_basic_auth_and_encrypted_payload(monkeypatch) -> None:
    settings = Settings(
        database_url="sqlite://",
        owntracks_username="owntracks",
        owntracks_password="owntracks-password",
        owntracks_encryption_key="owntracks-secret",
        owntracks_buffer_enabled=False,
        automatic_trip_processing_enabled=False,
    )
    _patch_settings(monkeypatch, settings)
    client, session_factory = _test_client_session()
    payload = {
        "_type": "location",
        "lat": 42.3314,
        "lon": -83.0458,
        "tst": int(datetime(2026, 6, 30, 12, 0, tzinfo=UTC).timestamp()),
        "tid": "IP",
        "topic": "owntracks/ian/phone",
    }
    encrypted_payload = _encrypted_owntracks_payload(payload, settings.owntracks_encryption_key)
    try:
        unauthenticated = client.post("/api/owntracks", json=encrypted_payload)
        plaintext = client.post(
            "/api/owntracks",
            json=payload,
            auth=("owntracks", "owntracks-password"),
        )
        accepted = client.post(
            "/api/owntracks",
            json=encrypted_payload,
            auth=("owntracks", "owntracks-password"),
        )

        assert unauthenticated.status_code == 401
        assert plaintext.status_code == 400
        assert accepted.status_code == 200
        with session_factory() as db:
            location = db.scalar(select(OwnTracksLocation))
            assert location is not None
            assert location.latitude == Decimal("42.3314")
            assert location.raw_payload["_decrypted"] is True
    finally:
        app.dependency_overrides.clear()


def test_owntracks_endpoint_fails_closed_without_encryption_key(monkeypatch) -> None:
    settings = Settings(
        database_url="sqlite://",
        owntracks_username="owntracks",
        owntracks_password="owntracks-password",
        automatic_trip_processing_enabled=False,
    )
    _patch_settings(monkeypatch, settings)
    client, _ = _test_client_session()
    payload = {
        "_type": "location",
        "lat": 42.3314,
        "lon": -83.0458,
        "tst": int(datetime(2026, 6, 30, 12, 0, tzinfo=UTC).timestamp()),
    }
    try:
        response = client.post(
            "/api/owntracks",
            json=payload,
            auth=("owntracks", "owntracks-password"),
        )

        assert response.status_code == 503
        assert response.json() == {"detail": "OWNTRACKS_ENCRYPTION_KEY is not configured"}
    finally:
        app.dependency_overrides.clear()


def test_owntracks_endpoint_buffers_without_database_dependency(monkeypatch, tmp_path) -> None:
    settings = Settings(
        database_url="sqlite://",
        owntracks_username="owntracks",
        owntracks_password="owntracks-password",
        owntracks_encryption_key="owntracks-secret",
        owntracks_buffer_path=str(tmp_path / "owntracks-buffer.sqlite3"),
        automatic_trip_processing_enabled=False,
    )
    _patch_settings(monkeypatch, settings)
    captured_ingest: dict[str, object] = {}

    def fail_get_db():
        raise AssertionError("OwnTracks endpoint should not open the normal DB dependency")

    def fake_ingest(
        body,
        *,
        topic=None,
        user=None,
        device=None,
        source="http",
        session_factory=None,
    ):
        captured_ingest.update(
            {
                "body": body,
                "topic": topic,
                "user": user,
                "device": device,
                "source": source,
                "session_factory": session_factory,
            }
        )
        return OwnTracksIngestOutcome(
            buffered=True,
            queue_id=1,
            reason="database_unavailable",
        )

    monkeypatch.setattr("mileage_logger.api.routes.ingest_or_buffer_owntracks_payload", fake_ingest)
    app.dependency_overrides[get_db] = fail_get_db
    client = TestClient(app)
    payload = {
        "_type": "location",
        "lat": 42.3314,
        "lon": -83.0458,
        "tst": int(datetime(2026, 6, 30, 12, 0, tzinfo=UTC).timestamp()),
        "tid": "IP",
        "topic": "owntracks/ian/phone",
    }
    encrypted_payload = _encrypted_owntracks_payload(payload, settings.owntracks_encryption_key)
    try:
        response = client.post(
            "/api/owntracks",
            json=encrypted_payload,
            auth=("owntracks", "owntracks-password"),
        )

        assert response.status_code == 200
        assert response.headers["X-Mileage-Logger-OwnTracks-Buffered"] == "true"
        assert response.headers["X-Mileage-Logger-OwnTracks-Buffer-Reason"] == (
            "database_unavailable"
        )
        assert captured_ingest["source"] == "http"
        decoded_payload = json.loads(captured_ingest["body"])
        assert decoded_payload["_decrypted"] is True
        assert decoded_payload["lat"] == 42.3314
    finally:
        app.dependency_overrides.clear()


def test_non_owntracks_api_requires_separate_bearer_key(monkeypatch) -> None:
    settings = Settings(database_url="sqlite://", web_api_key="web-api-secret")
    _patch_settings(monkeypatch, settings)
    client, _ = _test_client_session()
    try:
        health = client.get("/api/health")
        missing = client.get("/api/locations")
        wrong = client.get("/api/locations", headers={"Authorization": "Bearer wrong"})
        accepted = client.get("/api/locations", headers={"Authorization": "Bearer web-api-secret"})

        assert health.status_code == 200
        assert missing.status_code == 401
        assert wrong.status_code == 401
        assert accepted.status_code == 200
        assert accepted.json() == []
    finally:
        app.dependency_overrides.clear()


def test_non_owntracks_api_fails_closed_when_web_api_key_is_missing(monkeypatch) -> None:
    settings = Settings(database_url="sqlite://")
    _patch_settings(monkeypatch, settings)
    client, _ = _test_client_session()
    try:
        response = client.get("/api/locations")

        assert response.status_code == 503
        assert response.json() == {"detail": "WEB_API_KEY is not configured"}
    finally:
        app.dependency_overrides.clear()


def test_api_secret_settings_are_validated() -> None:
    with pytest.raises(ValidationError, match="OWNTRACKS_ENCRYPTION_KEY"):
        Settings(owntracks_encryption_key="x" * 33)

    with pytest.raises(ValidationError, match="OWNTRACKS_USERNAME and OWNTRACKS_PASSWORD"):
        Settings(owntracks_encryption_key="owntracks-secret")

    with pytest.raises(ValidationError, match="WEB_API_KEY"):
        Settings(
            app_env="production",
            secret_key="production-test-secret",
            web_login_username="admin",
            web_login_password="secret-password",
            owntracks_username="owntracks",
            owntracks_password="owntracks-password",
            owntracks_encryption_key="owntracks-secret",
        )
