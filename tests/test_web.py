from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from mileage_logger.app import app
from mileage_logger.config import Settings
from mileage_logger.database import get_db
from mileage_logger.models import Base, OwnTracksLocation, Site
from mileage_logger.services.diagnostics import (
    paginated_owntracks_entries,
    recent_owntracks_entries,
)
from mileage_logger.services.gas_prices import AaaMichiganGasPriceProvider, GasPriceReading
from mileage_logger.services.smartcar import SmartcarAuthenticationError
from mileage_logger.web.routes import _human_duration_since


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


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
    return TestClient(app), session_factory


def _location(
    captured_at: datetime,
    received_at: datetime,
    raw_payload: dict,
) -> OwnTracksLocation:
    return OwnTracksLocation(
        captured_at=captured_at,
        received_at=received_at,
        latitude=Decimal("42.3314000"),
        longitude=Decimal("-83.0458000"),
        raw_payload=raw_payload,
    )


def test_recent_owntracks_entries_include_travel_entries() -> None:
    db = _session()
    now = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)
    db.add_all(
        [
            _location(now, now, {"_type": "transition", "event": "enter", "desc": "Client A"}),
            _location(
                now + timedelta(minutes=1),
                now + timedelta(minutes=1),
                {"_type": "location"},
            ),
            _location(
                now + timedelta(minutes=2),
                now + timedelta(minutes=2),
                {"_type": "waypoint", "desc": "Client B"},
            ),
        ]
    )
    db.commit()

    events = recent_owntracks_entries(db)

    assert [event.raw_payload["_type"] for event in events] == [
        "transition",
        "location",
        "waypoint",
    ]


def test_paginated_owntracks_entries_loads_requested_page() -> None:
    db = _session()
    start = datetime(2026, 6, 11, 8, 0, tzinfo=UTC)
    db.add_all(
        [
            _location(
                start + timedelta(minutes=index),
                start + timedelta(minutes=index),
                {"_type": "location", "index": index},
            )
            for index in range(45)
        ]
    )
    db.commit()

    page = paginated_owntracks_entries(db, page=2, page_size=20)

    assert page.total == 45
    assert page.page == 2
    assert page.total_pages == 3
    assert page.first_item == 21
    assert page.last_item == 40
    assert page.has_previous is True
    assert page.has_next is True
    assert len(page.entries) == 20
    assert page.entries[0].raw_payload["index"] == 5
    assert page.entries[-1].raw_payload["index"] == 24


def test_waypoints_page_paginates_twenty_per_page() -> None:
    client, session_factory = _test_client_session()
    try:
        created_start = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        with session_factory() as db:
            waypoints = []
            for index in range(45):
                waypoints.append(
                    Site(
                        name=f"Waypoint {index:02d}",
                        owntracks_region_id=f"region-{index:02d}",
                        latitude=Decimal("42.3314000"),
                        longitude=Decimal("-83.0458000"),
                        radius_m=150,
                        created_at=created_start + timedelta(minutes=index),
                        last_visited_at=(
                            datetime(2026, 6, 11, 12, 0, tzinfo=UTC)
                            if index == 20
                            else None
                        ),
                    )
                )
            db.add_all(waypoints)
            db.commit()

        first_page_response = client.get("/waypoints?page=1")
        response = client.get("/waypoints?page=2")

        assert first_page_response.status_code == 200
        assert "Waypoint 20" in first_page_response.text
        assert "2026-06-11" in first_page_response.text
        assert response.status_code == 200
        assert "Showing 21-40" in response.text
        assert "of 45" in response.text
        assert "Page 2 of 3" in response.text
        assert "OwnTracks Region ID" not in response.text
        assert "region-25" not in response.text
        assert "/waypoints?page=1" in response.text
        assert "/waypoints?page=3" in response.text
        assert "Waypoint 25" in response.text
        assert "Waypoint 06" in response.text
        assert "Waypoint 26" not in response.text
        assert "Waypoint 20" not in response.text
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_shows_single_colored_app_log_and_download(
    tmp_path,
    monkeypatch,
) -> None:
    log_path = tmp_path / "app.log"
    log_text = "\n".join(
        [
            "2026-06-13 09:00:00 EDT DEBUG [app] details",
            "2026-06-13 09:01:00 EDT INFO [app] started",
            "2026-06-13 09:02:00 EDT WARNING [app] slow",
            "2026-06-13 09:03:00 EDT ERROR [app] failed",
        ]
    )
    log_path.write_text(log_text, encoding="utf-8")
    monkeypatch.setattr(
        "mileage_logger.web.routes.get_settings",
        lambda: Settings(
            database_url="sqlite://",
            log_dir=str(tmp_path),
            log_level="debug",
        ),
    )
    client, _ = _test_client_session()
    try:
        response = client.get("/diagnostics")
        download_response = client.get("/diagnostics/logs/app")

        assert response.status_code == 200
        assert "App Log" in response.text
        assert "Refresh App Log" in response.text
        assert "Trip Calculation Log" not in response.text
        assert "Gas Price Query Log" not in response.text
        assert 'class="log-line log-line-debug"' in response.text
        assert 'class="log-line log-line-info"' in response.text
        assert 'class="log-line log-line-warning"' in response.text
        assert 'class="log-line log-line-error"' in response.text
        assert "Download App Log" in response.text
        assert download_response.status_code == 200
        assert download_response.text == log_text
        assert "attachment" in download_response.headers["content-disposition"]
        assert "app.log" in download_response.headers["content-disposition"]
        assert download_response.headers["cache-control"] == "no-store"
    finally:
        app.dependency_overrides.clear()


def test_app_log_download_redacts_sensitive_query_values(tmp_path, monkeypatch) -> None:
    log_path = tmp_path / "app.log"
    log_path.write_text(
        "2026-06-13 09:00:00 EDT INFO [httpx] GET "
        "https://api.example.test/path?api_key=secret-value&series=test",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "mileage_logger.web.routes.get_settings",
        lambda: Settings(database_url="sqlite://", log_dir=str(tmp_path)),
    )
    client, _ = _test_client_session()
    try:
        response = client.get("/diagnostics/logs/app")

        assert response.status_code == 200
        assert "api_key=***" in response.text
        assert "secret-value" not in response.text
    finally:
        app.dependency_overrides.clear()


def test_human_duration_since_formats_last_received_age() -> None:
    now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)

    assert _human_duration_since(now - timedelta(minutes=5), now=now) == "5 minutes ago"
    assert _human_duration_since(None, now=now) == "Never"


def test_diagnostics_shows_last_received_owntracks_age() -> None:
    client, session_factory = _test_client_session()
    received_at = datetime.now(UTC) - timedelta(minutes=5)
    try:
        with session_factory() as db:
            db.add(
                _location(
                    datetime(2026, 6, 11, 12, 0, tzinfo=UTC),
                    received_at,
                    {"_type": "location"},
                )
            )
            db.commit()

        response = client.get("/diagnostics")

        assert response.status_code == 200
        assert "Last OwnTracks Received" in response.text
        assert "minutes ago" in response.text
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_smartcar_api_test_button_reports_pass(monkeypatch) -> None:
    monkeypatch.setattr(
        "mileage_logger.web.routes.get_settings",
        lambda: Settings(
            database_url="sqlite://",
            smartcar_enabled=True,
            smartcar_access_token="configured",
            smartcar_vehicle_id="configured",
        ),
    )
    monkeypatch.setattr(
        "mileage_logger.web.routes.current_odometer_miles",
        lambda settings, **_: Decimal("12345.600"),
    )
    client, _ = _test_client_session()
    try:
        response = client.post("/diagnostics/test/smartcar")

        assert response.status_code == 200
        assert "Smartcar API" in response.text
        assert "Pass" in response.text
        assert "12345.6 miles" in response.text
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_smartcar_api_test_button_reports_auth_failure(monkeypatch) -> None:
    def raise_auth_failure(_settings: Settings, **_: object) -> Decimal:
        raise SmartcarAuthenticationError("Smartcar authentication failed.")

    monkeypatch.setattr(
        "mileage_logger.web.routes.get_settings",
        lambda: Settings(
            database_url="sqlite://",
            smartcar_enabled=True,
            smartcar_access_token="configured",
            smartcar_vehicle_id="configured",
        ),
    )
    monkeypatch.setattr("mileage_logger.web.routes.current_odometer_miles", raise_auth_failure)
    client, _ = _test_client_session()
    try:
        response = client.post("/diagnostics/test/smartcar")

        assert response.status_code == 200
        assert "Smartcar API" in response.text
        assert "Fail" in response.text
        assert "Smartcar authentication failed." in response.text
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_eia_api_test_button_reports_pass(monkeypatch) -> None:
    class FakeEiaProvider:
        def current_regular_price(self, state: str) -> GasPriceReading:
            return GasPriceReading(
                state=state,
                grade="regular",
                price_per_gallon=Decimal("3.250"),
                source="eia_series",
                source_detail="test",
                observed_on=datetime(2026, 6, 14, tzinfo=UTC).date(),
            )

    monkeypatch.setattr(
        "mileage_logger.web.routes.get_settings",
        lambda: Settings(
            database_url="sqlite://",
            eia_api_key="configured",
            eia_series_id="PET.EMM_EPMR_PTE_SMI_DPG.W",
        ),
    )
    monkeypatch.setattr("mileage_logger.web.routes.EiaSeriesProvider", FakeEiaProvider)
    client, _ = _test_client_session()
    try:
        response = client.post("/diagnostics/test/eia")

        assert response.status_code == 200
        assert "EIA API" in response.text
        assert "Pass" in response.text
        assert "$3.250" in response.text
    finally:
        app.dependency_overrides.clear()


def test_aaa_gas_provider_uses_local_observed_date(monkeypatch) -> None:
    class FakeResponse:
        text = "Current Avg.</td><td>$3.250</td>"

        def raise_for_status(self) -> None:
            return None

    now = datetime(2026, 6, 11, 21, 30, tzinfo=UTC)
    monkeypatch.setattr(
        "mileage_logger.services.gas_prices.httpx.get",
        lambda *_, **__: FakeResponse(),
    )
    monkeypatch.setattr("mileage_logger.services.gas_prices.local_today", lambda: now.date())

    reading = AaaMichiganGasPriceProvider().current_regular_price("MI")

    assert reading.observed_on == now.date()
