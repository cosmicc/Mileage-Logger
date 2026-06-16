from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from mileage_logger.app import app
from mileage_logger.config import Settings
from mileage_logger.database import get_db
from mileage_logger.models import (
    AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
    Base,
    DeletedTrip,
    OwnTracksLocation,
    Site,
    Trip,
    TripProcessingCheckpoint,
)
from mileage_logger.services.diagnostics import (
    paginated_owntracks_entries,
    recent_owntracks_entries,
)
from mileage_logger.services.gas_prices import AaaMichiganGasPriceProvider, GasPriceReading
from mileage_logger.web.auth import FAILED_LOGIN_ATTEMPTS
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
    latitude: str = "42.3314000",
    longitude: str = "-83.0458000",
    odometer_miles: Decimal | None = None,
) -> OwnTracksLocation:
    return OwnTracksLocation(
        captured_at=captured_at,
        received_at=received_at,
        latitude=Decimal(latitude),
        longitude=Decimal(longitude),
        odometer_miles=odometer_miles,
        odometer_source="owntracks_rolling" if odometer_miles is not None else None,
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


def test_web_login_redirects_browser_pages_when_configured(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("mileage_logger.web.auth.get_settings", lambda: settings)
    client, _ = _test_client_session()
    try:
        response = client.get("/trips?year=2026&month=6", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?next=")
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_web_login_leaves_api_routes_open(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("mileage_logger.web.auth.get_settings", lambda: settings)
    client, _ = _test_client_session()
    try:
        response = client.get("/api/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_web_login_accepts_configured_credentials(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("mileage_logger.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("mileage_logger.web.routes.get_settings", lambda: settings)
    client, _ = _test_client_session()
    try:
        login_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/trips?year=2026&month=6",
            },
            follow_redirects=False,
        )
        page_response = client.get("/trips?year=2026&month=6")

        assert login_response.status_code == 303
        assert login_response.headers["location"] == "/trips?year=2026&month=6"
        assert page_response.status_code == 200
        assert "Monthly Trips" in page_response.text
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_web_login_rejects_invalid_credentials(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("mileage_logger.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("mileage_logger.web.routes.get_settings", lambda: settings)
    client, _ = _test_client_session()
    try:
        login_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "wrong-password",
                "next_url": "/trips?year=2026&month=6",
            },
        )
        page_response = client.get("/trips?year=2026&month=6", follow_redirects=False)

        assert login_response.status_code == 401
        assert "Invalid username or password." in login_response.text
        assert page_response.status_code == 303
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_web_login_page_does_not_disclose_app_name(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("mileage_logger.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("mileage_logger.web.routes.get_settings", lambda: settings)
    client, _ = _test_client_session()
    try:
        response = client.get("/login")

        assert response.status_code == 200
        assert "Mileage Logger" not in response.text
        assert ">ML<" not in response.text
        assert "<title>Sign In</title>" in response.text
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_dashboard_shows_today_and_month_distance_totals(monkeypatch) -> None:
    dashboard_now = datetime(
        2026,
        6,
        16,
        10,
        0,
        tzinfo=ZoneInfo("America/Detroit"),
    )
    monkeypatch.setattr("mileage_logger.web.routes.local_now", lambda: dashboard_now)
    monkeypatch.setattr("mileage_logger.web.routes.local_today", lambda: dashboard_now.date())
    monkeypatch.setattr(
        "mileage_logger.web.routes._monthly_gas_context",
        lambda _db, _year, _month: (None, ""),
    )
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            db.add_all(
                [
                    _location(
                        datetime(2026, 6, 1, 3, 30, tzinfo=UTC),
                        datetime(2026, 6, 1, 3, 30, tzinfo=UTC),
                        {"_type": "location"},
                        odometer_miles=Decimal("80.0"),
                    ),
                    _location(
                        datetime(2026, 6, 16, 3, 50, tzinfo=UTC),
                        datetime(2026, 6, 16, 3, 50, tzinfo=UTC),
                        {"_type": "location"},
                        odometer_miles=Decimal("100.0"),
                    ),
                    _location(
                        datetime(2026, 6, 16, 5, 0, tzinfo=UTC),
                        datetime(2026, 6, 16, 5, 0, tzinfo=UTC),
                        {"_type": "location"},
                        odometer_miles=Decimal("102.0"),
                    ),
                    _location(
                        datetime(2026, 6, 16, 16, 0, tzinfo=UTC),
                        datetime(2026, 6, 16, 16, 0, tzinfo=UTC),
                        {"_type": "location"},
                        odometer_miles=Decimal("108.5"),
                    ),
                    Trip(
                        trip_date=date(2026, 6, 12),
                        started_at=datetime(2026, 6, 12, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 12, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("4.0"),
                    ),
                    Trip(
                        trip_date=date(2026, 6, 16),
                        started_at=datetime(2026, 6, 16, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 16, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("5.5"),
                    ),
                ]
            )
            db.commit()

        response = client.get("/")

        assert response.status_code == 200
        assert "Distance driven summary" in response.text
        assert "Trips + non-trips" in response.text
        assert "Trips only" in response.text
        assert "<strong>8.5</strong>" in response.text
        assert "<strong>5.5</strong>" in response.text
        assert "<strong>28.5</strong>" in response.text
        assert "<strong>9.5</strong>" in response.text
    finally:
        app.dependency_overrides.clear()


def test_dashboard_keeps_today_distance_until_local_midnight(monkeypatch) -> None:
    dashboard_now = datetime(
        2026,
        6,
        16,
        23,
        30,
        tzinfo=ZoneInfo("America/Detroit"),
    )
    current_day = dashboard_now.date()
    next_day = current_day + timedelta(days=1)
    monkeypatch.setattr("mileage_logger.web.routes.local_now", lambda: dashboard_now)
    monkeypatch.setattr("mileage_logger.web.routes.local_today", lambda: current_day)
    monkeypatch.setattr(
        "mileage_logger.web.routes._monthly_gas_context",
        lambda _db, _year, _month: (None, ""),
    )
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            db.add_all(
                [
                    _location(
                        datetime(2026, 6, 16, 3, 55, tzinfo=UTC),
                        datetime(2026, 6, 16, 3, 55, tzinfo=UTC),
                        {"_type": "location"},
                        odometer_miles=Decimal("100.0"),
                    ),
                    _location(
                        datetime(2026, 6, 16, 4, 10, tzinfo=UTC),
                        datetime(2026, 6, 16, 4, 10, tzinfo=UTC),
                        {"_type": "location"},
                        odometer_miles=Decimal("101.0"),
                    ),
                    _location(
                        datetime(2026, 6, 17, 3, 30, tzinfo=UTC),
                        datetime(2026, 6, 17, 3, 30, tzinfo=UTC),
                        {"_type": "location"},
                        odometer_miles=Decimal("112.3"),
                    ),
                    _location(
                        datetime(2026, 6, 17, 4, 0, tzinfo=UTC),
                        datetime(2026, 6, 17, 4, 0, tzinfo=UTC),
                        {"_type": "location"},
                        odometer_miles=Decimal("125.0"),
                    ),
                    Trip(
                        trip_date=current_day,
                        started_at=datetime(2026, 6, 17, 1, 30, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 17, 2, 0, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("7.3"),
                    ),
                    Trip(
                        trip_date=next_day,
                        started_at=datetime(2026, 6, 17, 4, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 17, 4, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("12.7"),
                    ),
                ]
            )
            db.commit()

        response = client.get("/")

        assert response.status_code == 200
        assert "2026-06-16 11:30:00 PM" in response.text
        assert (
            "<span>Today</span>\n"
            "      <strong>12.3</strong>\n"
            "      <small>Trips + non-trips</small>"
        ) in response.text
        assert (
            "<span>Today</span>\n"
            "      <strong>7.3</strong>\n"
            "      <small>Trips only</small>"
        ) in response.text
    finally:
        app.dependency_overrides.clear()


def test_dashboard_replaces_vehicle_mpg_with_location_state(monkeypatch) -> None:
    monkeypatch.setattr(
        "mileage_logger.web.routes._monthly_gas_context",
        lambda _db, _year, _month: (None, ""),
    )
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            waypoint = Site(
                name="Home",
                latitude=Decimal("42.3314000"),
                longitude=Decimal("-83.0458000"),
                radius_m=150,
            )
            db.add(waypoint)
            db.add(
                _location(
                    datetime(2026, 6, 16, 13, 0, tzinfo=UTC),
                    datetime(2026, 6, 16, 13, 0, tzinfo=UTC),
                    {"_type": "location", "inregions": ["Home"]},
                    latitude="42.3314000",
                    longitude="-83.0458000",
                )
            )
            db.commit()

        response = client.get("/")

        assert response.status_code == 200
        assert "Vehicle MPG" not in response.text
        assert "Location State" in response.text
        assert "Inside waypoint" in response.text
        assert "Home" in response.text
    finally:
        app.dependency_overrides.clear()


def test_web_login_temporarily_locks_repeated_failures(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        web_login_username="admin",
        web_login_password="secret-password",
        web_login_max_attempts=2,
        web_login_lockout_seconds=300,
    )
    monkeypatch.setattr("mileage_logger.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("mileage_logger.web.routes.get_settings", lambda: settings)
    client, _ = _test_client_session()
    try:
        for _ in range(2):
            response = client.post(
                "/login",
                data={
                    "username": "admin",
                    "password": "wrong-password",
                    "next_url": "/trips?year=2026&month=6",
                },
            )
            assert response.status_code == 401

        locked_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/trips?year=2026&month=6",
            },
        )

        assert locked_response.status_code == 429
        assert "Login is temporarily unavailable." in locked_response.text
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


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


def test_waypoints_page_deletes_waypoint_and_preserves_trip_history() -> None:
    client, session_factory = _test_client_session()
    trip_date = datetime(2026, 6, 11, 12, 0, tzinfo=UTC).date()
    try:
        with session_factory() as db:
            home = Site(
                name="Home",
                latitude=Decimal("42.3314000"),
                longitude=Decimal("-83.0458000"),
                radius_m=150,
            )
            client_site = Site(
                name="Client",
                latitude=Decimal("42.3440000"),
                longitude=Decimal("-83.0600000"),
                radius_m=150,
            )
            db.add_all([home, client_site])
            db.flush()
            db.add(
                Trip(
                    trip_date=trip_date,
                    origin_site_id=home.id,
                    destination_site_id=client_site.id,
                    started_at=datetime(2026, 6, 11, 12, 0, tzinfo=UTC),
                    ended_at=datetime(2026, 6, 11, 12, 30, tzinfo=UTC),
                    start_latitude=home.latitude,
                    start_longitude=home.longitude,
                    end_latitude=client_site.latitude,
                    end_longitude=client_site.longitude,
                    miles=Decimal("12.34"),
                    source="auto",
                )
            )
            waypoint_id = client_site.id
            db.commit()

        page_response = client.get("/waypoints")
        delete_response = client.post(
            f"/waypoints/{waypoint_id}/delete",
            data={"page": "1"},
        )

        assert page_response.status_code == 200
        assert f"/waypoints/{waypoint_id}/delete" in page_response.text
        assert "Delete" in page_response.text
        assert delete_response.status_code == 200
        assert "Client" not in delete_response.text
        with session_factory() as db:
            assert db.get(Site, waypoint_id) is None
            trip = db.scalar(select(Trip))
            assert trip is not None
            assert trip.destination_site_id is None
            assert trip.destination_name == "Client"
            assert trip.miles == Decimal("12.3")
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


def test_diagnostics_shows_current_waypoint_state() -> None:
    client, session_factory = _test_client_session()
    arrived_at = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    try:
        with session_factory() as db:
            db.add(
                Site(
                    name="Client",
                    owntracks_region_id="client",
                    latitude=Decimal("42.3314000"),
                    longitude=Decimal("-83.0458000"),
                    radius_m=150,
                )
            )
            db.add_all(
                [
                    _location(
                        arrived_at,
                        arrived_at,
                        {"_type": "transition", "event": "enter", "desc": "Client"},
                    ),
                    _location(
                        arrived_at + timedelta(minutes=5),
                        arrived_at + timedelta(minutes=5),
                        {"_type": "location", "inregions": ["client"]},
                    ),
                ]
            )
            db.commit()

        response = client.get("/diagnostics")

        assert response.status_code == 200
        assert "OwnTracks State" in response.text
        assert "Inside waypoint" in response.text
        assert "Client" in response.text
        assert "Arrived" in response.text
        assert "OwnTracks State Changes" in response.text
        assert "Arrived at waypoint" in response.text
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_shows_travel_state_change_outside_waypoints(monkeypatch) -> None:
    monkeypatch.setattr(
        "mileage_logger.services.diagnostics.get_settings",
        lambda: Settings(
            database_url="sqlite://",
            owntracks_travel_distance_m=Decimal("50.0"),
        ),
    )
    client, session_factory = _test_client_session()
    start_at = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    try:
        with session_factory() as db:
            db.add(
                Site(
                    name="Home",
                    owntracks_region_id="home",
                    latitude=Decimal("42.3314000"),
                    longitude=Decimal("-83.0458000"),
                    radius_m=150,
                )
            )
            db.add_all(
                [
                    _location(
                        start_at,
                        start_at,
                        {"_type": "transition", "event": "enter", "desc": "Home"},
                    ),
                    _location(
                        start_at + timedelta(minutes=10),
                        start_at + timedelta(minutes=10),
                        {"_type": "transition", "event": "leave", "desc": "Home"},
                    ),
                    _location(
                        start_at + timedelta(minutes=11),
                        start_at + timedelta(minutes=11),
                        {"_type": "location"},
                        latitude="42.3440000",
                        longitude="-83.0600000",
                    ),
                ]
            )
            db.commit()

        response = client.get("/diagnostics")

        assert response.status_code == 200
        assert "Travel detected" in response.text
        assert "Left waypoint" in response.text
        assert "Home" in response.text
        assert "1.1 miles" in response.text
    finally:
        app.dependency_overrides.clear()


def test_trips_page_delete_button_removes_trip_and_records_exact_deletion() -> None:
    client, session_factory = _test_client_session()
    trip_date = datetime(2026, 6, 11, 12, 0, tzinfo=UTC).date()
    try:
        with session_factory() as db:
            home = Site(
                name="Home",
                latitude=Decimal("42.3314000"),
                longitude=Decimal("-83.0458000"),
                radius_m=150,
            )
            client_site = Site(
                name="Client",
                latitude=Decimal("42.3440000"),
                longitude=Decimal("-83.0600000"),
                radius_m=150,
            )
            db.add_all([home, client_site])
            db.flush()
            db.add(
                Trip(
                    trip_date=trip_date,
                    origin_site_id=home.id,
                    destination_site_id=client_site.id,
                    started_at=datetime(2026, 6, 11, 12, 0, tzinfo=UTC),
                    ended_at=datetime(2026, 6, 11, 12, 30, tzinfo=UTC),
                    start_latitude=home.latitude,
                    start_longitude=home.longitude,
                    end_latitude=client_site.latitude,
                    end_longitude=client_site.longitude,
                    origin_name="Home",
                    destination_name="Client",
                    miles=Decimal("4.25"),
                    mileage_source="waypoint_distance",
                    source="auto",
                )
            )
            db.commit()

        page_response = client.get("/trips?year=2026&month=6")
        delete_response = client.post("/trips/1/delete")

        assert page_response.status_code == 200
        assert "Delete" in page_response.text
        assert delete_response.status_code == 200
        assert "No trips for this month." in delete_response.text
        with session_factory() as db:
            assert db.get(Trip, 1) is None
            deleted_trip = db.scalar(select(DeletedTrip))
            assert deleted_trip is not None
            assert deleted_trip.origin_name == "Home"
            assert deleted_trip.destination_name == "Client"
            assert deleted_trip.reason == "user_deleted"
    finally:
        app.dependency_overrides.clear()


def test_trips_page_removes_deleted_trip_record() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            home = Site(
                name="Home",
                latitude=Decimal("42.3314000"),
                longitude=Decimal("-83.0458000"),
                radius_m=150,
            )
            client_site = Site(
                name="Client",
                latitude=Decimal("42.3440000"),
                longitude=Decimal("-83.0600000"),
                radius_m=150,
            )
            db.add_all([home, client_site])
            db.flush()
            db.add(
                DeletedTrip(
                    deleted_trip_id=42,
                    trip_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
                    origin_site_id=home.id,
                    destination_site_id=client_site.id,
                    started_at=datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
                    ended_at=datetime(2026, 6, 15, 12, 30, tzinfo=UTC),
                    origin_name="Home",
                    destination_name="Client",
                    miles=Decimal("12.50"),
                    source="auto",
                    mileage_source="owntracks_path",
                    reason="user_deleted",
                )
            )
            db.commit()

        page_response = client.get("/trips?year=2026&month=6")
        delete_response = client.post(
            "/trips/suppression/1/delete",
            data={"redirect_year": "2026", "redirect_month": "6"},
        )

        assert page_response.status_code == 200
        assert "Deleted Trip Records" in page_response.text
        assert "Remove Record" in page_response.text
        assert "Home" in page_response.text
        assert "Client" in page_response.text
        assert delete_response.status_code == 200
        assert "No deleted trip records for this month." in delete_response.text
        with session_factory() as db:
            assert db.get(DeletedTrip, 1) is None
    finally:
        app.dependency_overrides.clear()


def test_trips_page_creates_manual_trip() -> None:
    client, session_factory = _test_client_session()
    try:
        page_response = client.get("/trips?year=2026&month=6")
        create_response = client.post(
            "/trips",
            data={
                "trip_date": "2026-06-15",
                "origin_name": "Home",
                "destination_name": "Client",
                "miles": "12.34",
            },
        )

        assert page_response.status_code == 200
        assert "Add Trip" in page_response.text
        assert create_response.status_code == 200
        assert "2026-06-15" in create_response.text
        assert "Home" in create_response.text
        assert "Client" in create_response.text
        with session_factory() as db:
            trip = db.scalar(select(Trip))
            assert trip is not None
            assert trip.trip_date == datetime(2026, 6, 15, tzinfo=UTC).date()
            assert trip.origin_name == "Home"
            assert trip.destination_name == "Client"
            assert trip.miles == Decimal("12.3")
            assert trip.source == "manual"
            assert trip.mileage_source == "manual"
    finally:
        app.dependency_overrides.clear()


def test_trips_page_updates_existing_trip_date_and_values() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            db.add(
                Trip(
                    trip_date=datetime(2026, 6, 10, tzinfo=UTC).date(),
                    started_at=datetime(2026, 6, 10, 13, 0, tzinfo=UTC),
                    ended_at=datetime(2026, 6, 10, 13, 30, tzinfo=UTC),
                    start_latitude=Decimal("42.3314"),
                    start_longitude=Decimal("-83.0458"),
                    end_latitude=Decimal("42.3440"),
                    end_longitude=Decimal("-83.0600"),
                    origin_name="Old Home",
                    destination_name="Old Client",
                    miles=Decimal("5.00"),
                    start_odometer_miles=Decimal("1000.000"),
                    end_odometer_miles=Decimal("1005.000"),
                    start_odometer_source="estimated",
                    end_odometer_source="estimated",
                    source="auto",
                )
            )
            db.commit()

        response = client.post(
            "/trips/1",
            data={
                "trip_date": "2026-06-16",
                "miles": "15.50",
                "start_odometer_miles": "2000.1234",
                "end_odometer_miles": "2015.9876",
            },
        )

        assert response.status_code == 200
        assert "2026-06-16" in response.text
        assert "Old Home" in response.text
        assert "Old Client" in response.text
        with session_factory() as db:
            trip = db.get(Trip, 1)
            assert trip is not None
            assert trip.trip_date == datetime(2026, 6, 16, tzinfo=UTC).date()
            assert trip.origin_name == "Old Home"
            assert trip.destination_name == "Old Client"
            assert trip.miles == Decimal("15.50")
            assert trip.start_odometer_miles == Decimal("1000.000")
            assert trip.end_odometer_miles == Decimal("1015.500")
            assert trip.start_odometer_source == "previous_trip"
            assert trip.end_odometer_source == "estimated"
            assert trip.source == "manual"
            assert trip.mileage_source == "manual"
    finally:
        app.dependency_overrides.clear()


def test_trips_page_odometer_values_are_read_only() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            db.add(
                Trip(
                    trip_date=datetime(2026, 6, 10, tzinfo=UTC).date(),
                    started_at=datetime(2026, 6, 10, 13, 0, tzinfo=UTC),
                    ended_at=datetime(2026, 6, 10, 13, 30, tzinfo=UTC),
                    start_latitude=Decimal("42.3314"),
                    start_longitude=Decimal("-83.0458"),
                    end_latitude=Decimal("42.3440"),
                    end_longitude=Decimal("-83.0600"),
                    origin_name="Home",
                    destination_name="Client",
                    miles=Decimal("5.00"),
                    mileage_source="owntracks_path",
                    source="auto",
                )
            )
            db.commit()

        page_response = client.get("/trips?year=2026&month=6")
        response = client.post(
            "/trips/1",
            data={
                "trip_date": "2026-06-10",
                "miles": "5.00",
                "start_odometer_miles": "3000.111",
                "end_odometer_miles": "",
            },
        )

        assert page_response.status_code == 200
        assert '<td class="trip-name">Home</td>' in page_response.text
        assert '<td class="trip-name">Client</td>' in page_response.text
        assert 'name="origin_name" maxlength="160" required value="Home"' not in page_response.text
        assert (
            'name="destination_name" maxlength="160" required value="Client"'
            not in page_response.text
        )
        assert 'name="start_odometer_miles"' not in page_response.text
        assert 'name="end_odometer_miles"' not in page_response.text
        assert response.status_code == 200
        with session_factory() as db:
            trip = db.get(Trip, 1)
            assert trip is not None
            assert trip.origin_name == "Home"
            assert trip.destination_name == "Client"
            assert trip.miles == Decimal("5.00")
            assert trip.start_odometer_miles is None
            assert trip.end_odometer_miles is None
            assert trip.source == "auto"
            assert trip.mileage_source == "owntracks_path"
    finally:
        app.dependency_overrides.clear()


def test_trips_page_distance_edit_resequences_month_odometers() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            db.add_all(
                [
                    Trip(
                        trip_date=datetime(2026, 6, 10, tzinfo=UTC).date(),
                        started_at=datetime(2026, 6, 10, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 10, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        origin_name="Home",
                        destination_name="Client A",
                        miles=Decimal("5.00"),
                        start_odometer_miles=Decimal("1000.000"),
                        end_odometer_miles=Decimal("1005.000"),
                        source="auto",
                    ),
                    Trip(
                        trip_date=datetime(2026, 6, 11, tzinfo=UTC).date(),
                        started_at=datetime(2026, 6, 11, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 11, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3440"),
                        start_longitude=Decimal("-83.0600"),
                        end_latitude=Decimal("42.3600"),
                        end_longitude=Decimal("-83.0700"),
                        origin_name="Client A",
                        destination_name="Client B",
                        miles=Decimal("7.00"),
                        start_odometer_miles=Decimal("1005.000"),
                        end_odometer_miles=Decimal("1012.000"),
                        source="auto",
                    ),
                ]
            )
            db.commit()

        response = client.post(
            "/trips/1",
            data={
                "trip_date": "2026-06-10",
                "miles": "6.25",
            },
        )

        assert response.status_code == 200
        with session_factory() as db:
            trips = list(db.scalars(select(Trip).order_by(Trip.started_at.asc())))
            assert trips[0].start_odometer_miles == Decimal("1000.0")
            assert trips[0].end_odometer_miles == Decimal("1006.3")
            assert trips[1].start_odometer_miles == Decimal("1006.3")
            assert trips[1].end_odometer_miles == Decimal("1013.3")
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_manual_odometer_form_saves_reading() -> None:
    client, session_factory = _test_client_session()
    try:
        response = client.post(
            "/diagnostics/odometer",
            data={"odometer_miles": "12345.678"},
        )

        assert response.status_code == 200
        assert "Manual Odometer" in response.text
        assert "Pass" in response.text
        assert "12345.7 miles" in response.text
        assert "Manual" in response.text
        with session_factory() as db:
            checkpoint = db.scalar(
                select(TripProcessingCheckpoint).where(
                    TripProcessingCheckpoint.name == AUTOMATIC_TRIP_PROCESSING_CHECKPOINT
                )
            )
            assert checkpoint is not None
            assert checkpoint.odometer_anchor_miles == Decimal("12345.7")
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_manual_odometer_form_rejects_nonpositive_reading() -> None:
    client, session_factory = _test_client_session()
    try:
        response = client.post(
            "/diagnostics/odometer",
            data={"odometer_miles": "0"},
        )

        assert response.status_code == 200
        assert "Fail" in response.text
        assert "Odometer reading must be greater than zero." in response.text
        with session_factory() as db:
            assert db.scalar(select(TripProcessingCheckpoint)) is None
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
