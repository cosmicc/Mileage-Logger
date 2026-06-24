import gzip
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from mileage_logger.app import app
from mileage_logger.config import Settings
from mileage_logger.database import get_db
from mileage_logger.models import (
    AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
    Base,
    CloudflareIPBlock,
    DeletedTrip,
    GasPriceSnapshot,
    HiddenLoginFailure,
    MonthlyGasPrice,
    OwnTracksLocation,
    Site,
    Trip,
    TripProcessingCheckpoint,
)
from mileage_logger.services.backups import create_automatic_backup, list_automatic_backup_files
from mileage_logger.services.cloudflare_blocks import (
    CloudflareAccessRule,
    create_cloudflare_ip_block,
    ip_is_allowlisted,
)
from mileage_logger.services.diagnostics import (
    paginated_owntracks_entries,
    recent_owntracks_entries,
)
from mileage_logger.services.gas_prices import AaaMichiganGasPriceProvider, GasPriceReading
from mileage_logger.services.login_failures import tail_login_failure_entries
from mileage_logger.services.mileage import haversine_miles
from mileage_logger.web.auth import FAILED_LOGIN_ATTEMPTS
from mileage_logger.web.routes import (
    _dashboard_distance_summary,
    _dashboard_reimbursement_summary,
    _diagnostic_database_summary,
    _diagnostic_disk_usages,
    _human_duration_since,
    templates,
)


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


def _html_section(html: str, start_marker: str, end_marker: str | None = None) -> str:
    start = html.index(start_marker)
    if end_marker is None:
        return html[start:]
    end = html.index(end_marker, start)
    return html[start:end]


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


def _site(
    name: str,
    latitude: str = "42.3314000",
    longitude: str = "-83.0458000",
    *,
    active: bool = True,
) -> Site:
    return Site(
        name=name,
        latitude=Decimal(latitude),
        longitude=Decimal(longitude),
        radius_m=150,
        active=active,
    )


def _seed_full_backup_data(db: Session) -> None:
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    site = Site(
        name="Client",
        latitude=Decimal("42.3314000"),
        longitude=Decimal("-83.0458000"),
        radius_m=175,
        active=True,
        created_at=now,
        last_visited_at=now,
    )
    db.add(site)
    db.flush()

    location = _location(
        now,
        now + timedelta(seconds=5),
        {"_type": "transition", "event": "enter", "desc": "Client"},
        odometer_miles=Decimal("1000.0"),
    )
    db.add(location)
    db.flush()

    db.add_all(
        [
            Trip(
                trip_date=now.date(),
                origin_site_id=site.id,
                destination_site_id=site.id,
                started_at=now,
                ended_at=now + timedelta(minutes=20),
                start_latitude=site.latitude,
                start_longitude=site.longitude,
                end_latitude=site.latitude,
                end_longitude=site.longitude,
                origin_name="Client",
                destination_name="Client",
                miles=Decimal("12.3"),
                start_odometer_miles=Decimal("1000.0"),
                end_odometer_miles=Decimal("1012.3"),
                start_odometer_source="manual",
                end_odometer_source="estimated",
                mileage_source="owntracks_path",
                source="auto",
                notes="backup test trip",
                created_at=now,
                updated_at=now,
            ),
            DeletedTrip(
                deleted_trip_id=99,
                trip_date=now.date(),
                origin_site_id=site.id,
                destination_site_id=site.id,
                started_at=now + timedelta(hours=1),
                ended_at=now + timedelta(hours=2),
                origin_name="Client",
                destination_name="Client",
                miles=Decimal("0.4"),
                source="auto",
                mileage_source="owntracks_path",
                reason="invalid_same_waypoint_under_one_mile",
                deleted_at=now,
                notes="suppressed test trip",
            ),
            TripProcessingCheckpoint(
                name=AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
                last_owntracks_location_id=location.id,
                odometer_anchor_miles=Decimal("1012.3"),
                odometer_anchor_recorded_at=now,
                created_at=now,
                updated_at=now,
            ),
            GasPriceSnapshot(
                observed_on=now.date(),
                state="MI",
                grade="regular",
                price_per_gallon=Decimal("3.250"),
                source="test",
                source_detail="backup test",
                created_at=now,
            ),
            MonthlyGasPrice(
                year=2026,
                month=6,
                state="MI",
                average_price_per_gallon=Decimal("3.250"),
                buffer_per_gallon=Decimal("0.50"),
                effective_rate=Decimal("3.750"),
                source="test",
                source_detail="backup test",
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    db.commit()


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


def test_web_login_rejects_invalid_credentials(monkeypatch, tmp_path) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    login_failure_log_path = tmp_path / "login-failures.log"
    settings = Settings(
        database_url="sqlite://",
        web_login_username="admin",
        web_login_password="secret-password",
        login_failure_log_path=str(login_failure_log_path),
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
        assert login_failure_log_path.exists()
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_web_login_records_failed_attempt_audit_log(monkeypatch, tmp_path) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    login_failure_log_path = tmp_path / "login-failures.log"
    settings = Settings(
        database_url="sqlite://",
        web_login_username="admin",
        web_login_password="secret-password",
        login_failure_log_path=str(login_failure_log_path),
    )
    monkeypatch.setattr("mileage_logger.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("mileage_logger.web.routes.get_settings", lambda: settings)
    client, _ = _test_client_session()
    try:
        response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "wrong-password",
                "next_url": "/diagnostics",
            },
            headers={
                "User-Agent": "ExampleBrowser/1.0",
                "CF-Connecting-IP": "198.51.100.77",
                "X-Real-IP": "203.0.113.10",
                "X-Forwarded-For": "203.0.113.10, 10.0.0.8",
                "X-Forwarded-Proto": "https",
            },
        )

        log_text = login_failure_log_path.read_text(encoding="utf-8")
        payload = json.loads(log_text.splitlines()[0])

        assert response.status_code == 401
        assert payload["event"] == "web_login_failed"
        assert payload["client_ip"] == "198.51.100.77"
        assert payload["cf_connecting_ip"] == "198.51.100.77"
        assert payload["username"] == "admin"
        assert payload["password_length"] == len("wrong-password")
        assert payload["user_agent"] == "ExampleBrowser/1.0"
        assert payload["reason"] == "invalid_credentials"
        assert payload["next_url"] == "/diagnostics"
        assert payload["failed_count"] == 1
        assert "wrong-password" not in log_text
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


def test_web_layout_includes_mobile_install_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        "mileage_logger.web.routes._monthly_gas_context",
        lambda _db, _year, _month: (None, ""),
    )
    client, _ = _test_client_session()
    try:
        response = client.get("/")

        assert response.status_code == 200
        assert "viewport-fit=cover" in response.text
        assert 'name="apple-mobile-web-app-capable" content="yes"' in response.text
        assert (
            'name="apple-mobile-web-app-status-bar-style" content="black-translucent"'
            in response.text
        )
        assert 'rel="manifest" href="/manifest.webmanifest"' in response.text
        assert 'rel="apple-touch-icon" href="/apple-touch-icon.png"' in response.text
        assert "/static/icons/mileage-logger-icon.svg" in response.text
        assert 'class="app-close-button"' in response.text
        assert 'data-app-close aria-label="Close app"' in response.text
        assert ".app-close-button {\n  display: none;" in response.text
        assert ".app-close-button {\n    display: inline-flex;" in response.text
        assert "window.close()" in response.text
    finally:
        app.dependency_overrides.clear()


def test_install_assets_stay_available_when_web_login_is_enabled(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("mileage_logger.web.auth.get_settings", lambda: settings)
    client, _ = _test_client_session()
    try:
        manifest_response = client.get("/manifest.webmanifest", follow_redirects=False)
        service_worker_response = client.get("/service-worker.js", follow_redirects=False)
        favicon_response = client.get("/favicon.ico", follow_redirects=False)
        apple_icon_response = client.get("/apple-touch-icon.png", follow_redirects=False)

        assert manifest_response.status_code == 200
        assert manifest_response.headers["content-type"].startswith("application/manifest+json")
        manifest = manifest_response.json()
        assert manifest["display"] == "fullscreen"
        assert manifest["display_override"][:2] == ["fullscreen", "standalone"]
        assert manifest["start_url"] == "/"
        assert manifest["scope"] == "/"
        assert {icon["purpose"] for icon in manifest["icons"]} == {"any", "maskable"}
        assert "/static/icons/mileage-logger-icon-512.png" in {
            icon["src"] for icon in manifest["icons"]
        }

        assert service_worker_response.status_code == 200
        assert service_worker_response.headers["service-worker-allowed"] == "/"
        assert "fetch(event.request)" in service_worker_response.text
        assert "caches.open" not in service_worker_response.text

        assert favicon_response.status_code == 200
        assert favicon_response.headers["content-type"].startswith("image/x-icon")
        assert apple_icon_response.status_code == 200
        assert apple_icon_response.headers["content-type"].startswith("image/png")
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
        month_origin_latitude = Decimal("42.3314")
        month_origin_longitude = Decimal("-83.0458")
        prior_month_latitude = Decimal("42.3314")
        prior_month_longitude = Decimal("-83.0458")
        prior_today_latitude = Decimal("42.3440")
        prior_today_longitude = Decimal("-83.0600")
        today_point_one_latitude = Decimal("42.3314")
        today_point_one_longitude = Decimal("-83.0600")
        today_point_two_latitude = Decimal("42.3380")
        today_point_two_longitude = Decimal("-83.0700")
        today_total = (
            haversine_miles(
                prior_today_latitude,
                prior_today_longitude,
                today_point_one_latitude,
                today_point_one_longitude,
            )
            + haversine_miles(
                today_point_one_latitude,
                today_point_one_longitude,
                today_point_two_latitude,
                today_point_two_longitude,
            )
        ).quantize(Decimal("0.1"))
        month_total = (
            haversine_miles(
                prior_month_latitude,
                prior_month_longitude,
                month_origin_latitude,
                month_origin_longitude,
            )
            + haversine_miles(
                month_origin_latitude,
                month_origin_longitude,
                prior_today_latitude,
                prior_today_longitude,
            )
            + today_total
        ).quantize(Decimal("0.1"))
        with session_factory() as db:
            db.add_all(
                [
                    _location(
                        datetime(2026, 6, 1, 3, 30, tzinfo=UTC),
                        datetime(2026, 6, 1, 3, 30, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(prior_month_latitude),
                        longitude=str(prior_month_longitude),
                        odometer_miles=Decimal("80.0"),
                    ),
                    _location(
                        datetime(2026, 6, 12, 13, 30, tzinfo=UTC),
                        datetime(2026, 6, 12, 13, 30, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(month_origin_latitude),
                        longitude=str(month_origin_longitude),
                        odometer_miles=Decimal("95.0"),
                    ),
                    _location(
                        datetime(2026, 6, 16, 3, 50, tzinfo=UTC),
                        datetime(2026, 6, 16, 3, 50, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(prior_today_latitude),
                        longitude=str(prior_today_longitude),
                        odometer_miles=Decimal("100.0"),
                    ),
                    _location(
                        datetime(2026, 6, 16, 5, 0, tzinfo=UTC),
                        datetime(2026, 6, 16, 5, 0, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(today_point_one_latitude),
                        longitude=str(today_point_one_longitude),
                        odometer_miles=Decimal("102.0"),
                    ),
                    _location(
                        datetime(2026, 6, 16, 16, 0, tzinfo=UTC),
                        datetime(2026, 6, 16, 16, 0, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(today_point_two_latitude),
                        longitude=str(today_point_two_longitude),
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
        assert (
            "<span>Today</span>\n"
            f"      <strong>{max(today_total, Decimal('5.5'))}</strong>\n"
            "      <small>Trips + non-trips</small>"
        ) in response.text
        assert "<strong>5.5</strong>" in response.text
        assert (
            "<span>This Month</span>\n"
            f"      <strong>{max(month_total, Decimal('9.5'))}</strong>\n"
            "      <small>Trips + non-trips</small>"
        ) in response.text
        assert "<strong>9.5</strong>" in response.text
    finally:
        app.dependency_overrides.clear()


def test_dashboard_replaces_waypoints_card_with_month_reimbursement(monkeypatch) -> None:
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
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            db.add(
                Site(
                    name="Client",
                    latitude=Decimal("42.3314000"),
                    longitude=Decimal("-83.0458000"),
                    radius_m=150,
                )
            )
            db.add_all(
                [
                    Trip(
                        trip_date=date(2026, 6, 12),
                        started_at=datetime(2026, 6, 12, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 12, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("25.0"),
                    ),
                    Trip(
                        trip_date=date(2026, 6, 16),
                        started_at=datetime(2026, 6, 16, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 16, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("75.0"),
                    ),
                    MonthlyGasPrice(
                        year=2026,
                        month=6,
                        state="MI",
                        average_price_per_gallon=Decimal("3.500"),
                        buffer_per_gallon=Decimal("0.00"),
                        effective_rate=Decimal("3.500"),
                        source="test",
                        source_detail="dashboard reimbursement test",
                    ),
                ]
            )
            db.commit()

        response = client.get("/")

        assert response.status_code == 200
        stats_section = _html_section(
            response.text,
            '<section class="stats-grid">',
            '<section class="panel">',
        )
        assert "Waypoints" not in stats_section
        assert "Month Reimbursement" in stats_section
        assert "$14.00" in stats_section
        assert "4 reimbursement gallons" in stats_section
        assert "mi PDF total" not in stats_section
        assert stats_section.index("<span>OwnTracks Events</span>") < stats_section.index(
            "<span>Trips</span>"
        )
        assert stats_section.index("<span>Trips</span>") < stats_section.index(
            "<span>Month Reimbursement</span>"
        )
        with session_factory() as db:
            monthly_gas = db.scalar(select(MonthlyGasPrice))
            assert monthly_gas is not None
            reimbursement_summary = _dashboard_reimbursement_summary(
                db,
                year=2026,
                month=6,
                monthly_gas=monthly_gas,
                vehicle_mpg=Decimal("25.0"),
            )
            assert reimbursement_summary["total"] == Decimal("14.00")
            assert reimbursement_summary["reimbursement_gallons"] == Decimal("4.000")
            assert reimbursement_summary["reimbursement_gallons_display"] == "4"
    finally:
        app.dependency_overrides.clear()


def test_dashboard_trip_plus_non_trip_total_is_never_below_trip_total(monkeypatch) -> None:
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
                        datetime(2026, 6, 16, 13, 30, tzinfo=UTC),
                        datetime(2026, 6, 16, 13, 30, tzinfo=UTC),
                        {"_type": "location"},
                        latitude="42.3314000",
                        longitude="-83.0458000",
                    ),
                    Trip(
                        trip_date=date(2026, 6, 16),
                        started_at=datetime(2026, 6, 16, 14, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 16, 14, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("8.4"),
                    ),
                ]
            )
            db.commit()
            summary = _dashboard_distance_summary(
                db,
                today=date(2026, 6, 16),
                year=2026,
                month=6,
            )

        response = client.get("/")

        assert summary["today_total"] == Decimal("8.4")
        assert summary["today_trips"] == Decimal("8.4")
        assert summary["today_non_trips"] == Decimal("0.0")
        assert summary["today_total"] - summary["today_trips"] == summary["today_non_trips"]
        assert summary["month_total"] == Decimal("8.4")
        assert summary["month_total"] - summary["month_trips"] == summary["month_non_trips"]
        assert response.status_code == 200
        assert (
            "<span>Today</span>\n"
            "      <strong>8.4</strong>\n"
            "      <small>Trips + non-trips</small>"
        ) in response.text
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
        prior_today_latitude = Decimal("42.3314")
        prior_today_longitude = Decimal("-83.0458")
        point_one_latitude = Decimal("42.3314")
        point_one_longitude = Decimal("-83.0600")
        point_two_latitude = Decimal("42.3380")
        point_two_longitude = Decimal("-83.0700")
        next_day_latitude = Decimal("42.3500")
        next_day_longitude = Decimal("-83.0800")
        today_total = (
            haversine_miles(
                prior_today_latitude,
                prior_today_longitude,
                point_one_latitude,
                point_one_longitude,
            )
            + haversine_miles(
                point_one_latitude,
                point_one_longitude,
                point_two_latitude,
                point_two_longitude,
            )
        ).quantize(Decimal("0.1"))
        with session_factory() as db:
            db.add_all(
                [
                    _location(
                        datetime(2026, 6, 16, 3, 55, tzinfo=UTC),
                        datetime(2026, 6, 16, 3, 55, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(prior_today_latitude),
                        longitude=str(prior_today_longitude),
                        odometer_miles=Decimal("100.0"),
                    ),
                    _location(
                        datetime(2026, 6, 16, 4, 10, tzinfo=UTC),
                        datetime(2026, 6, 16, 4, 10, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(point_one_latitude),
                        longitude=str(point_one_longitude),
                        odometer_miles=Decimal("101.0"),
                    ),
                    _location(
                        datetime(2026, 6, 17, 3, 30, tzinfo=UTC),
                        datetime(2026, 6, 17, 3, 30, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(point_two_latitude),
                        longitude=str(point_two_longitude),
                        odometer_miles=Decimal("112.3"),
                    ),
                    _location(
                        datetime(2026, 6, 17, 4, 0, tzinfo=UTC),
                        datetime(2026, 6, 17, 4, 0, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(next_day_latitude),
                        longitude=str(next_day_longitude),
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
            f"      <strong>{max(today_total, Decimal('7.3'))}</strong>\n"
            "      <small>Trips + non-trips</small>"
        ) in response.text
        assert (
            "<span>Today</span>\n"
            "      <strong>7.3</strong>\n"
            "      <small>Trips only</small>"
        ) in response.text
    finally:
        app.dependency_overrides.clear()


def test_dashboard_distance_totals_ignore_manual_odometer_reset(monkeypatch) -> None:
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
        prior_month_latitude = Decimal("42.3314")
        prior_month_longitude = Decimal("-83.0458")
        prior_today_latitude = Decimal("42.3440")
        prior_today_longitude = Decimal("-83.0600")
        reset_point_latitude = Decimal("42.3314")
        reset_point_longitude = Decimal("-83.0600")
        end_point_latitude = Decimal("42.3380")
        end_point_longitude = Decimal("-83.0700")
        today_total = (
            haversine_miles(
                prior_today_latitude,
                prior_today_longitude,
                reset_point_latitude,
                reset_point_longitude,
            )
            + haversine_miles(
                reset_point_latitude,
                reset_point_longitude,
                end_point_latitude,
                end_point_longitude,
            )
        ).quantize(Decimal("0.1"))
        month_total = (
            haversine_miles(
                prior_month_latitude,
                prior_month_longitude,
                prior_today_latitude,
                prior_today_longitude,
            )
            + today_total
        ).quantize(Decimal("0.1"))
        with session_factory() as db:
            db.add_all(
                [
                    _location(
                        datetime(2026, 6, 1, 3, 30, tzinfo=UTC),
                        datetime(2026, 6, 1, 3, 30, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(prior_month_latitude),
                        longitude=str(prior_month_longitude),
                        odometer_miles=Decimal("5.0"),
                    ),
                    _location(
                        datetime(2026, 6, 16, 3, 50, tzinfo=UTC),
                        datetime(2026, 6, 16, 3, 50, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(prior_today_latitude),
                        longitude=str(prior_today_longitude),
                        odometer_miles=Decimal("10.0"),
                    ),
                    _location(
                        datetime(2026, 6, 16, 5, 0, tzinfo=UTC),
                        datetime(2026, 6, 16, 5, 0, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(reset_point_latitude),
                        longitude=str(reset_point_longitude),
                        odometer_miles=Decimal("20000.0"),
                    ),
                    _location(
                        datetime(2026, 6, 16, 16, 0, tzinfo=UTC),
                        datetime(2026, 6, 16, 16, 0, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(end_point_latitude),
                        longitude=str(end_point_longitude),
                        odometer_miles=Decimal("20003.0"),
                    ),
                ]
            )
            db.commit()

        response = client.get("/")

        assert response.status_code == 200
        assert f"<strong>{today_total}</strong>" in response.text
        assert f"<strong>{month_total}</strong>" in response.text
        assert "<strong>19993.0</strong>" not in response.text
        assert "<strong>19998.0</strong>" not in response.text
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


def test_web_login_temporarily_locks_repeated_failures(monkeypatch, tmp_path) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    login_failure_log_path = tmp_path / "login-failures.log"
    settings = Settings(
        database_url="sqlite://",
        web_login_username="admin",
        web_login_password="secret-password",
        web_login_max_attempts=2,
        web_login_lockout_seconds=300,
        login_failure_log_path=str(login_failure_log_path),
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
        payloads = [
            json.loads(line)
            for line in login_failure_log_path.read_text(encoding="utf-8").splitlines()
        ]
        assert [payload["reason"] for payload in payloads] == [
            "invalid_credentials",
            "invalid_credentials",
            "locked_out",
        ]
        assert payloads[-1]["lockout_applied"] is True
        assert payloads[-1]["password_length"] == len("secret-password")
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_web_login_auto_blocks_cloudflare_ip_after_five_consecutive_failures(
    monkeypatch,
    tmp_path,
) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    login_failure_log_path = tmp_path / "login-failures.log"
    settings = Settings(
        database_url="sqlite://",
        web_login_username="admin",
        web_login_password="secret-password",
        web_login_max_attempts=10,
        login_failure_log_path=str(login_failure_log_path),
        cloudflare_ip_blocking_enabled=True,
        cloudflare_api_token="test-token",
        cloudflare_zone_id="test-zone",
        cloudflare_auto_block_failed_login_attempts=5,
    )
    created_blocks: list[str] = []

    def fake_create_cloudflare_ip_block(ip_address: str, *, note: str, settings: Settings):
        created_blocks.append(ip_address)
        assert "5 consecutive failed web login attempts" in note
        return CloudflareAccessRule(rule_id="cf-rule-1", ip_address=ip_address)

    monkeypatch.setattr("mileage_logger.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("mileage_logger.web.routes.get_settings", lambda: settings)
    monkeypatch.setattr(
        "mileage_logger.web.routes.create_cloudflare_ip_block",
        fake_create_cloudflare_ip_block,
    )
    client, session_factory = _test_client_session()
    try:
        for _ in range(5):
            response = client.post(
                "/login",
                data={
                    "username": "admin",
                    "password": "wrong-password",
                    "next_url": "/diagnostics",
                },
                headers={"CF-Connecting-IP": "198.51.100.55"},
            )
            assert response.status_code == 401

        assert created_blocks == ["198.51.100.55"]
        with session_factory() as db:
            block = db.scalar(select(CloudflareIPBlock))
            assert block is not None
            assert block.ip_address == "198.51.100.55"
            assert block.cloudflare_rule_id == "cf-rule-1"
            assert block.source == "automatic"
            assert block.failure_count == 5
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_successful_web_login_resets_consecutive_failures_before_auto_block(
    monkeypatch,
    tmp_path,
) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    login_failure_log_path = tmp_path / "login-failures.log"
    settings = Settings(
        database_url="sqlite://",
        web_login_username="admin",
        web_login_password="secret-password",
        web_login_max_attempts=10,
        login_failure_log_path=str(login_failure_log_path),
        cloudflare_ip_blocking_enabled=True,
        cloudflare_api_token="test-token",
        cloudflare_zone_id="test-zone",
        cloudflare_auto_block_failed_login_attempts=5,
    )

    def fail_create_cloudflare_ip_block(ip_address: str, *, note: str, settings: Settings):
        raise AssertionError(f"unexpected block for {ip_address}: {note}")

    monkeypatch.setattr("mileage_logger.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("mileage_logger.web.routes.get_settings", lambda: settings)
    monkeypatch.setattr(
        "mileage_logger.web.routes.create_cloudflare_ip_block",
        fail_create_cloudflare_ip_block,
    )
    client, session_factory = _test_client_session()
    try:
        for _ in range(4):
            response = client.post(
                "/login",
                data={
                    "username": "admin",
                    "password": "wrong-password",
                    "next_url": "/diagnostics",
                },
                headers={"CF-Connecting-IP": "198.51.100.56"},
            )
            assert response.status_code == 401

        success_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/diagnostics",
            },
            headers={"CF-Connecting-IP": "198.51.100.56"},
            follow_redirects=False,
        )
        assert success_response.status_code == 303

        response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "wrong-password",
                "next_url": "/diagnostics",
            },
            headers={"CF-Connecting-IP": "198.51.100.56"},
        )
        assert response.status_code == 401
        with session_factory() as db:
            assert db.scalar(select(CloudflareIPBlock)) is None
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_cloudflare_ip_block_allowlist_matches_ips_and_cidrs() -> None:
    settings = Settings(
        database_url="sqlite://",
        cloudflare_ip_block_allowlist="198.51.100.20, 203.0.113.0/24",
    )

    assert ip_is_allowlisted("198.51.100.20", settings)
    assert ip_is_allowlisted("203.0.113.44", settings)
    assert not ip_is_allowlisted("198.51.100.21", settings)


def test_create_cloudflare_ip_block_sends_zone_access_rule_payload(monkeypatch) -> None:
    settings = Settings(
        database_url="sqlite://",
        cloudflare_ip_blocking_enabled=True,
        cloudflare_api_token="test-token",
        cloudflare_zone_id="test-zone",
    )
    captured_request = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"success": True, "result": {"id": "rule-123"}}

    def fake_post(url, *, headers, json, timeout):
        captured_request["url"] = url
        captured_request["headers"] = headers
        captured_request["json"] = json
        captured_request["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("mileage_logger.services.cloudflare_blocks.httpx.post", fake_post)

    result = create_cloudflare_ip_block(
        "203.0.113.44",
        note="Mileage Logger manual block",
        settings=settings,
    )

    assert result.rule_id == "rule-123"
    assert captured_request["url"].endswith(
        "/zones/test-zone/firewall/access_rules/rules"
    )
    assert captured_request["headers"]["Authorization"] == "Bearer test-token"
    assert captured_request["json"] == {
        "mode": "block",
        "configuration": {"target": "ip", "value": "203.0.113.44"},
        "notes": "Mileage Logger manual block",
    }


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


def test_diagnostics_shows_failed_login_attempts_without_footer_actions(
    tmp_path,
    monkeypatch,
) -> None:
    login_failure_log_path = tmp_path / "login-failures.log"
    payload = {
        "event": "web_login_failed",
        "occurred_at_utc": "2026-06-19T12:00:00Z",
        "occurred_at_local": "2026-06-19T08:00:00-04:00",
        "client_ip": "203.0.113.10",
        "direct_client_ip": "10.0.0.12",
        "x_real_ip": "203.0.113.10",
        "x_forwarded_for": "203.0.113.10, 10.0.0.12",
        "forwarded_proto": "https",
        "host": "mileage.example.test",
        "user_agent": "ExampleBrowser/1.0",
        "method": "POST",
        "path": "/login",
        "next_url": "/diagnostics",
        "reason": "invalid_credentials",
        "username": "admin",
        "username_length": 5,
        "username_truncated": False,
        "password_length": 14,
        "failed_count": 1,
        "max_attempts": 5,
        "lockout_applied": False,
        "lockout_remaining_seconds": 0,
    }
    login_failure_log_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        "mileage_logger.web.routes.get_settings",
        lambda: Settings(
            database_url="sqlite://",
            log_dir=str(tmp_path),
            login_failure_log_path=str(login_failure_log_path),
        ),
    )
    client, _ = _test_client_session()
    try:
        response = client.get("/diagnostics")
        download_response = client.get("/diagnostics/logs/login-failures")

        assert response.status_code == 200
        assert "Failed Login Attempts" in response.text
        assert "203.0.113.10" in response.text
        assert "admin" in response.text
        assert "ExampleBrowser/1.0" in response.text
        assert "Refresh Login Failures" not in response.text
        assert "Download Login Failure Log" not in response.text
        assert "/diagnostics/logs/login-failures" not in response.text
        assert download_response.status_code == 200
        assert "web_login_failed" in download_response.text
        assert "attachment" in download_response.headers["content-disposition"]
        assert "mileage-logger-login-failures.log" in download_response.headers[
            "content-disposition"
        ]
        assert download_response.headers["cache-control"] == "no-store"
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_paginates_failed_logins_and_cloudflare_blocks(
    tmp_path,
    monkeypatch,
) -> None:
    login_failure_log_path = tmp_path / "login-failures.log"
    lines = []
    for index in range(12):
        lines.append(
            json.dumps(
                {
                    "event": "web_login_failed",
                    "occurred_at_utc": f"2026-06-19T12:{index:02d}:00Z",
                    "occurred_at_local": f"2026-06-19T08:{index:02d}:00-04:00",
                    "client_ip": f"203.0.113.{100 + index}",
                    "direct_client_ip": "10.0.0.12",
                    "cf_connecting_ip": f"203.0.113.{100 + index}",
                    "x_real_ip": "",
                    "x_forwarded_for": "",
                    "forwarded_proto": "https",
                    "host": "mileage.example.test",
                    "user_agent": f"ExampleBrowser/{index}",
                    "method": "POST",
                    "path": "/login",
                    "next_url": "/diagnostics",
                    "reason": "invalid_credentials",
                    "username": f"user-{index:02d}",
                    "username_length": 7,
                    "username_truncated": False,
                    "password_length": 14,
                    "failed_count": index + 1,
                    "max_attempts": 5,
                    "lockout_applied": False,
                    "lockout_remaining_seconds": 0,
                }
            )
        )
    login_failure_log_path.write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(
        "mileage_logger.web.routes.get_settings",
        lambda: Settings(
            database_url="sqlite://",
            log_dir=str(tmp_path),
            login_failure_log_path=str(login_failure_log_path),
        ),
    )
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            for index in range(12):
                db.add(
                    CloudflareIPBlock(
                        ip_address=f"198.51.100.{100 + index}",
                        cloudflare_rule_id=f"cf-rule-{index:02d}",
                        source="manual",
                        reason=f"test block {index:02d}",
                        created_at=datetime(2026, 6, 19, 12, index, tzinfo=UTC),
                    )
                )
            db.commit()

        response = client.get("/diagnostics")
        assert response.status_code == 200

        login_section = _html_section(
            response.text,
            '<section id="login-failures" class="panel">',
            '<section id="cloudflare-blocked-ips" class="panel">',
        )
        assert "Showing 1-10 of 12 from" in login_section
        assert login_section.count("<tr>") == 11
        assert "user-11" in login_section
        assert "user-02" in login_section
        assert "user-01" not in login_section
        assert "user-00" not in login_section

        second_login_page = client.get("/diagnostics?login_failures_page=2")
        second_login_section = _html_section(
            second_login_page.text,
            '<section id="login-failures" class="panel">',
            '<section id="cloudflare-blocked-ips" class="panel">',
        )
        assert "Showing 11-12 of 12 from" in second_login_section
        assert "user-01" in second_login_section
        assert "user-00" in second_login_section
        assert "user-02" not in second_login_section

        cloudflare_section = _html_section(
            response.text,
            '<section id="cloudflare-blocked-ips" class="panel">',
            '<section id="app-log" class="panel log-panel">',
        )
        assert "Showing 1-10 of 12 app-managed Cloudflare" in cloudflare_section
        assert cloudflare_section.count("<tr>") == 11
        assert "198.51.100.111" in cloudflare_section
        assert "198.51.100.102" in cloudflare_section
        assert "198.51.100.101" not in cloudflare_section
        assert "198.51.100.100" not in cloudflare_section

        second_cloudflare_page = client.get("/diagnostics?cloudflare_blocks_page=2")
        second_cloudflare_section = _html_section(
            second_cloudflare_page.text,
            '<section id="cloudflare-blocked-ips" class="panel">',
            '<section id="app-log" class="panel log-panel">',
        )
        assert "Showing 11-12 of 12 app-managed Cloudflare" in second_cloudflare_section
        assert "198.51.100.101" in second_cloudflare_section
        assert "198.51.100.100" in second_cloudflare_section
        assert "198.51.100.102" not in second_cloudflare_section
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_hides_failed_login_entry_without_rewriting_log(
    tmp_path,
    monkeypatch,
) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    login_failure_log_path = tmp_path / "login-failures.log"
    payload = {
        "event": "web_login_failed",
        "occurred_at_utc": "2026-06-19T12:00:00Z",
        "occurred_at_local": "2026-06-19T08:00:00-04:00",
        "client_ip": "203.0.113.10",
        "direct_client_ip": "10.0.0.12",
        "cf_connecting_ip": "203.0.113.10",
        "x_real_ip": "",
        "x_forwarded_for": "",
        "forwarded_proto": "https",
        "host": "mileage.example.test",
        "user_agent": "ExampleBrowser/1.0",
        "method": "POST",
        "path": "/login",
        "next_url": "/diagnostics",
        "reason": "invalid_credentials",
        "username": "admin",
        "username_length": 5,
        "username_truncated": False,
        "password_length": 14,
        "failed_count": 1,
        "max_attempts": 5,
        "lockout_applied": False,
        "lockout_remaining_seconds": 0,
    }
    raw_log_line = json.dumps(payload)
    login_failure_log_path.write_text(raw_log_line, encoding="utf-8")
    settings = Settings(
        database_url="sqlite://",
        web_login_username="admin",
        web_login_password="secret-password",
        log_dir=str(tmp_path),
        login_failure_log_path=str(login_failure_log_path),
    )
    monkeypatch.setattr("mileage_logger.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("mileage_logger.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session()
    try:
        login_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/diagnostics",
            },
            follow_redirects=False,
        )
        assert login_response.status_code == 303
        entry = tail_login_failure_entries(login_failure_log_path)[0]

        response = client.post(
            "/diagnostics/login-failures/hide",
            data={
                "entry_id": entry.entry_id,
                "client_ip": entry.client_ip,
                "occurred_at_utc": entry.occurred_at_utc,
            },
            follow_redirects=False,
        )
        diagnostics_response = client.get("/diagnostics")

        assert response.status_code == 303
        assert "203.0.113.10" not in diagnostics_response.text
        assert login_failure_log_path.read_text(encoding="utf-8") == raw_log_line
        with session_factory() as db:
            hidden_entry = db.scalar(select(HiddenLoginFailure))
            assert hidden_entry is not None
            assert hidden_entry.entry_id == entry.entry_id
            assert hidden_entry.client_ip == "203.0.113.10"
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_diagnostics_cloudflare_block_buttons_create_and_remove_app_managed_block(
    tmp_path,
    monkeypatch,
) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    login_failure_log_path = tmp_path / "login-failures.log"
    payload = {
        "event": "web_login_failed",
        "occurred_at_utc": "2026-06-19T12:00:00Z",
        "occurred_at_local": "2026-06-19T08:00:00-04:00",
        "client_ip": "203.0.113.20",
        "direct_client_ip": "10.0.0.12",
        "cf_connecting_ip": "203.0.113.20",
        "x_real_ip": "",
        "x_forwarded_for": "",
        "forwarded_proto": "https",
        "host": "mileage.example.test",
        "user_agent": "ExampleBrowser/1.0",
        "method": "POST",
        "path": "/login",
        "next_url": "/diagnostics",
        "reason": "invalid_credentials",
        "username": "admin",
        "username_length": 5,
        "username_truncated": False,
        "password_length": 14,
        "failed_count": 1,
        "max_attempts": 5,
        "lockout_applied": False,
        "lockout_remaining_seconds": 0,
    }
    login_failure_log_path.write_text(json.dumps(payload), encoding="utf-8")
    settings = Settings(
        database_url="sqlite://",
        web_login_username="admin",
        web_login_password="secret-password",
        log_dir=str(tmp_path),
        login_failure_log_path=str(login_failure_log_path),
        cloudflare_ip_blocking_enabled=True,
        cloudflare_api_token="test-token",
        cloudflare_zone_id="test-zone",
    )
    deleted_rule_ids: list[str] = []

    def fake_create_cloudflare_ip_block(ip_address: str, *, note: str, settings: Settings):
        assert ip_address == "203.0.113.20"
        assert "Diagnostics failed-login row block button" in note
        return CloudflareAccessRule(rule_id="cf-manual-rule", ip_address=ip_address)

    def fake_delete_cloudflare_ip_block(rule_id: str, *, settings: Settings):
        deleted_rule_ids.append(rule_id)

    monkeypatch.setattr("mileage_logger.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("mileage_logger.web.routes.get_settings", lambda: settings)
    monkeypatch.setattr(
        "mileage_logger.web.routes.create_cloudflare_ip_block",
        fake_create_cloudflare_ip_block,
    )
    monkeypatch.setattr(
        "mileage_logger.web.routes.delete_cloudflare_ip_block",
        fake_delete_cloudflare_ip_block,
    )
    client, session_factory = _test_client_session()
    try:
        login_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/diagnostics",
            },
            follow_redirects=False,
        )
        assert login_response.status_code == 303

        initial_response = client.get("/diagnostics")
        assert 'aria-label="Block IP at Cloudflare"' in initial_response.text
        assert ">Off</button>" in initial_response.text

        block_response = client.post(
            "/diagnostics/cloudflare-blocks/block",
            data={"ip_address": "203.0.113.20"},
            follow_redirects=False,
        )
        assert block_response.status_code == 303
        with session_factory() as db:
            block = db.scalar(select(CloudflareIPBlock))
            assert block is not None
            assert block.ip_address == "203.0.113.20"
            assert block.cloudflare_rule_id == "cf-manual-rule"
            assert block.source == "manual"

        blocked_response = client.get("/diagnostics")
        assert "Cloudflare Blocked IPs" in blocked_response.text
        assert "cf-manual-rule" in blocked_response.text
        assert 'aria-label="Unblock IP at Cloudflare"' in blocked_response.text
        assert ">On</button>" in blocked_response.text

        unblock_response = client.post(
            "/diagnostics/cloudflare-blocks/unblock",
            data={"ip_address": "203.0.113.20"},
            follow_redirects=False,
        )
        assert unblock_response.status_code == 303
        assert deleted_rule_ids == ["cf-manual-rule"]
        with session_factory() as db:
            assert db.scalar(select(CloudflareIPBlock)) is None
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_diagnostics_full_backup_download_and_restore_round_trip(monkeypatch, tmp_path) -> None:
    settings = Settings(
        database_url="sqlite://",
        web_login_username="admin",
        web_login_password="secret-password",
        log_dir=str(tmp_path),
        login_failure_log_path=str(tmp_path / "login-failures.log"),
    )
    monkeypatch.setattr("mileage_logger.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("mileage_logger.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session()
    try:
        login_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/diagnostics",
            },
            follow_redirects=False,
        )
        assert login_response.status_code == 303
        with session_factory() as db:
            _seed_full_backup_data(db)

        diagnostics_response = client.get("/diagnostics")
        backup_response = client.get("/diagnostics/backup")
        payload = json.loads(gzip.decompress(backup_response.content).decode("utf-8"))
        backup_section_start = diagnostics_response.text.index(
            '<section id="data-backup" class="panel">'
        )
        automatic_backups_start = diagnostics_response.text.index(
            '<div id="automatic-backups" class="backup-subsection">',
            backup_section_start,
        )
        manual_backup_start = diagnostics_response.text.index(
            '<div class="backup-subsection manual-backup-subsection">',
            automatic_backups_start,
        )
        backup_header = diagnostics_response.text[
            backup_section_start:automatic_backups_start
        ]
        manual_backup_section = diagnostics_response.text[manual_backup_start:]
        assert diagnostics_response.status_code == 200
        assert "Application database tables and OwnTracks waypoint export." not in backup_header
        assert "Download Full Backup" not in backup_header
        assert automatic_backups_start < manual_backup_start
        assert "Full Backup Download" in manual_backup_section
        assert "Application database tables and OwnTracks waypoint export." in manual_backup_section
        assert "Download Full Backup" in manual_backup_section
        assert manual_backup_section.index("Download Full Backup") < manual_backup_section.index(
            "Upload Restore"
        )
        assert backup_response.status_code == 200
        assert backup_response.headers["cache-control"] == "no-store"
        assert "mileage-logger-full-backup" in backup_response.headers["content-disposition"]
        assert payload["format"] == "mileage_logger.full_backup"
        assert payload["table_counts"]["sites"] == 1
        assert payload["owntracks_waypoints"]["waypoints"][0]["desc"] == "Client"

        with session_factory() as db:
            existing_site = db.scalar(select(Site))
            assert existing_site is not None
            existing_site.name = "Changed Client"
            db.add(
                Site(
                    name="Temporary",
                    latitude=Decimal("40.0000000"),
                    longitude=Decimal("-80.0000000"),
                    radius_m=100,
                )
            )
            db.commit()

        restore_response = client.post(
            "/diagnostics/restore",
            data={"confirmation": "RESTORE"},
            files={
                "backup_file": (
                    "mileage-logger-full-backup.json.gz",
                    backup_response.content,
                    "application/gzip",
                )
            },
        )

        assert restore_response.status_code == 200
        assert "Full database restore completed." in restore_response.text
        assert "rows restored across" in restore_response.text
        with session_factory() as db:
            assert db.scalar(select(func.count(Site.id))) == 1
            assert db.scalar(select(Site.name)) == "Client"
            assert db.scalar(select(func.count(Trip.id))) == 1
            assert db.scalar(select(func.count(DeletedTrip.id))) == 1
            assert db.scalar(select(func.count(OwnTracksLocation.id))) == 1
            assert db.scalar(select(func.count(TripProcessingCheckpoint.id))) == 1
            assert db.scalar(select(func.count(GasPriceSnapshot.id))) == 1
            assert db.scalar(select(func.count(MonthlyGasPrice.id))) == 1
            trip = db.scalar(select(Trip))
            assert trip is not None
            assert trip.miles == Decimal("12.3")
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_restores_retained_automatic_backup(monkeypatch, tmp_path) -> None:
    settings = Settings(
        database_url="sqlite://",
        web_login_username="admin",
        web_login_password="secret-password",
        log_dir=str(tmp_path),
        login_failure_log_path=str(tmp_path / "login-failures.log"),
        automatic_backup_dir=str(tmp_path / "backups"),
    )
    monkeypatch.setattr("mileage_logger.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("mileage_logger.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session()
    try:
        login_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/diagnostics",
            },
            follow_redirects=False,
        )
        assert login_response.status_code == 303
        with session_factory() as db:
            _seed_full_backup_data(db)
            backup_result = create_automatic_backup(
                db,
                settings.automatic_backup_dir,
                now=datetime(2026, 6, 20, 12, 0, tzinfo=UTC),
            )

        diagnostics_response = client.get("/diagnostics")
        download_response = client.get(
            "/diagnostics/automatic-backups/download",
            params={"filename": backup_result.backup_file.filename},
        )
        assert diagnostics_response.status_code == 200
        assert "Automatic Backups" in diagnostics_response.text
        assert backup_result.backup_file.filename in diagnostics_response.text
        automatic_backup_section = _html_section(
            diagnostics_response.text,
            '<div id="automatic-backups" class="backup-subsection">',
            '<div class="backup-subsection manual-backup-subsection">',
        )
        assert (
            f'class="backup-file-name" title="{backup_result.backup_file.filename}"'
            in automatic_backup_section
        )
        assert ">Confirmation" not in automatic_backup_section
        assert "Type RESTORE to confirm automatic backup restore" in automatic_backup_section
        assert "/diagnostics/automatic-backups/download" in diagnostics_response.text
        assert download_response.status_code == 200
        assert download_response.content == backup_result.backup_file.path.read_bytes()
        assert download_response.headers["cache-control"] == "no-store"
        assert backup_result.backup_file.filename in download_response.headers[
            "content-disposition"
        ]

        with session_factory() as db:
            existing_site = db.scalar(select(Site))
            assert existing_site is not None
            existing_site.name = "Changed Client"
            db.add(
                Site(
                    name="Temporary",
                    latitude=Decimal("40.0000000"),
                    longitude=Decimal("-80.0000000"),
                    radius_m=100,
                )
            )
            db.commit()

        restore_response = client.post(
            "/diagnostics/automatic-backups/restore",
            data={
                "filename": backup_result.backup_file.filename,
                "confirmation": "RESTORE",
            },
        )

        assert restore_response.status_code == 200
        assert "Automatic backup restore completed." in restore_response.text
        with session_factory() as db:
            assert db.scalar(select(func.count(Site.id))) == 1
            assert db.scalar(select(Site.name)) == "Client"
            assert db.scalar(select(func.count(Trip.id))) == 1
    finally:
        app.dependency_overrides.clear()


def test_automatic_backup_retention_keeps_hourly_and_recent_daily(tmp_path) -> None:
    db = _session()
    _seed_full_backup_data(db)
    backup_dir = tmp_path / "backups"
    backup_times = [
        datetime(2026, 6, 17, 12, 0, tzinfo=UTC),
        datetime(2026, 6, 18, 12, 0, tzinfo=UTC),
        datetime(2026, 6, 19, 12, 0, tzinfo=UTC),
        *[
            datetime(2026, 6, 20, hour, 0, tzinfo=UTC)
            for hour in range(3, 13)
        ],
    ]
    for backup_time in backup_times:
        create_automatic_backup(db, backup_dir, now=backup_time)

    retained_backups = list_automatic_backup_files(backup_dir)
    retained_filenames = {backup.filename for backup in retained_backups}

    assert len(retained_backups) == 8
    assert "mileage-logger-auto-backup-20260617-120000Z.json.gz" not in retained_filenames
    assert "mileage-logger-auto-backup-20260618-120000Z.json.gz" in retained_filenames
    assert "mileage-logger-auto-backup-20260619-120000Z.json.gz" not in retained_filenames
    assert "mileage-logger-auto-backup-20260620-030000Z.json.gz" in retained_filenames
    for hour in range(7, 13):
        assert f"mileage-logger-auto-backup-20260620-{hour:02d}0000Z.json.gz" in retained_filenames


def test_diagnostics_full_restore_requires_confirmation(monkeypatch, tmp_path) -> None:
    settings = Settings(
        database_url="sqlite://",
        web_login_username="admin",
        web_login_password="secret-password",
        log_dir=str(tmp_path),
        login_failure_log_path=str(tmp_path / "login-failures.log"),
    )
    monkeypatch.setattr("mileage_logger.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("mileage_logger.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session()
    try:
        client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/diagnostics",
            },
        )
        with session_factory() as db:
            _seed_full_backup_data(db)

        backup_response = client.get("/diagnostics/backup")
        response = client.post(
            "/diagnostics/restore",
            data={"confirmation": "restore"},
            files={
                "backup_file": (
                    "mileage-logger-full-backup.json.gz",
                    backup_response.content,
                    "application/gzip",
                )
            },
        )

        assert response.status_code == 200
        assert "Type RESTORE to confirm full database restore." in response.text
        with session_factory() as db:
            assert db.scalar(select(func.count(Site.id))) == 1
            assert db.scalar(select(Site.name)) == "Client"
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


def test_diagnostics_paginates_owntracks_entries_and_state_changes() -> None:
    client, session_factory = _test_client_session()
    captured_at = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    try:
        with session_factory() as db:
            for index in range(12):
                site = Site(
                    name=f"State {index:02d}",
                    owntracks_region_id=f"state-{index:02d}",
                    latitude=Decimal("42.3314000"),
                    longitude=Decimal("-83.0458000"),
                    radius_m=150,
                )
                location = _location(
                    captured_at + timedelta(minutes=index),
                    captured_at + timedelta(minutes=index),
                    {
                        "_type": "transition",
                        "event": "enter",
                        "desc": site.name,
                        "seq": index,
                    },
                )
                location.topic = f"owntracks/user/device-{index:02d}"
                db.add(site)
                db.add(location)
            db.commit()

        response = client.get("/diagnostics")

        assert response.status_code == 200
        state_section = _html_section(
            response.text,
            '<section id="owntracks-state-log" class="panel">',
            '<section id="owntracks-entries" class="panel">',
        )
        assert "Showing 1-10 of 12 state changes." in state_section
        assert state_section.count("<tr>") == 11
        assert "State 11" in state_section
        assert "State 02" in state_section
        assert "State 01" not in state_section
        assert "State 00" not in state_section

        second_state_page = client.get("/diagnostics?state_changes_page=2")
        second_state_section = _html_section(
            second_state_page.text,
            '<section id="owntracks-state-log" class="panel">',
            '<section id="owntracks-entries" class="panel">',
        )
        assert "Showing 11-12 of 12 state changes." in second_state_section
        assert "State 01" in second_state_section
        assert "State 00" in second_state_section
        assert "State 02" not in second_state_section

        entries_section = _html_section(
            response.text,
            '<section id="owntracks-entries" class="panel">',
            '<section id="login-failures" class="panel">',
        )
        assert "Showing 1-10 of 12 entries." in entries_section
        assert entries_section.count("<tr>") == 11
        assert "owntracks/user/device-11" in entries_section
        assert "owntracks/user/device-02" in entries_section
        assert "owntracks/user/device-01" not in entries_section
        assert "owntracks/user/device-00" not in entries_section

        second_entries_page = client.get("/diagnostics?owntracks_page=2")
        second_entries_section = _html_section(
            second_entries_page.text,
            '<section id="owntracks-entries" class="panel">',
            '<section id="login-failures" class="panel">',
        )
        assert "Showing 11-12 of 12 entries." in second_entries_section
        assert "owntracks/user/device-01" in second_entries_section
        assert "owntracks/user/device-00" in second_entries_section
        assert "owntracks/user/device-02" not in second_entries_section
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


def test_trips_page_creates_manual_trip(monkeypatch) -> None:
    monkeypatch.setattr("mileage_logger.web.routes.local_today", lambda: date(2026, 6, 22))
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            home = _site("Home", "42.3314000", "-83.0458000")
            client_site = _site("Client", "42.3440000", "-83.0600000")
            db.add_all([home, client_site])
            db.commit()
            home_id = home.id
            client_site_id = client_site.id

        page_response = client.get("/trips?year=2026&month=6")
        create_response = client.post(
            "/trips",
            data={
                "trip_date": "2026-06-15",
                "origin_site_id": str(home_id),
                "destination_site_id": str(client_site_id),
                "miles": "12.34",
            },
        )

        assert page_response.status_code == 200
        assert "Add Trip" in page_response.text
        assert 'name="trip_date" value="2026-06-22"' in page_response.text
        assert 'name="origin_site_id"' in page_response.text
        assert 'name="destination_site_id"' in page_response.text
        assert create_response.status_code == 200
        assert "2026-06-15" in create_response.text
        assert "Home" in create_response.text
        assert "Client" in create_response.text
        with session_factory() as db:
            trip = db.scalar(select(Trip))
            assert trip is not None
            assert trip.trip_date == datetime(2026, 6, 15, tzinfo=UTC).date()
            assert trip.origin_site_id == home_id
            assert trip.destination_site_id == client_site_id
            assert trip.origin_name == "Home"
            assert trip.destination_name == "Client"
            assert trip.start_latitude == Decimal("42.3314000")
            assert trip.end_latitude == Decimal("42.3440000")
            assert trip.miles == Decimal("12.3")
            assert trip.source == "manual"
            assert trip.mileage_source == "manual"
    finally:
        app.dependency_overrides.clear()


def test_trips_page_lists_newest_trips_first() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            db.add_all(
                [
                    Trip(
                        trip_date=date(2026, 6, 10),
                        started_at=datetime(2026, 6, 10, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 10, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        origin_name="First Stop",
                        destination_name="First Client",
                        miles=Decimal("5.0"),
                        source="manual",
                    ),
                    Trip(
                        trip_date=date(2026, 6, 10),
                        started_at=datetime(2026, 6, 10, 16, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 10, 16, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        origin_name="Second Stop",
                        destination_name="Second Client",
                        miles=Decimal("7.0"),
                        source="manual",
                    ),
                ]
            )
            db.commit()

        response = client.get("/trips?year=2026&month=6")

        assert response.status_code == 200
        assert response.text.index("trip-form-2") < response.text.index("trip-form-1")
    finally:
        app.dependency_overrides.clear()


def test_trips_page_updates_existing_trip_distance_without_editing_date() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            old_home = _site("Old Home", "42.3314000", "-83.0458000")
            old_client = _site("Old Client", "42.3440000", "-83.0600000")
            new_home = _site("New Home", "42.3600000", "-83.0700000")
            new_client = _site("New Client", "42.3700000", "-83.0800000")
            db.add_all([old_home, old_client, new_home, new_client])
            db.flush()
            db.add(
                Trip(
                    trip_date=datetime(2026, 6, 10, tzinfo=UTC).date(),
                    origin_site_id=old_home.id,
                    destination_site_id=old_client.id,
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
            new_home_id = new_home.id
            new_client_id = new_client.id

        response = client.post(
            "/trips/1",
            data={
                "trip_date": "2026-06-16",
                "origin_site_id": str(new_home_id),
                "destination_site_id": str(new_client_id),
                "miles": "15.50",
                "start_odometer_miles": "2000.1234",
                "end_odometer_miles": "2015.9876",
            },
        )

        assert response.status_code == 200
        assert "2026-06-10" in response.text
        assert "2026-06-16" not in response.text
        assert "New Home" in response.text
        assert "New Client" in response.text
        with session_factory() as db:
            trip = db.get(Trip, 1)
            assert trip is not None
            assert trip.trip_date == datetime(2026, 6, 10, tzinfo=UTC).date()
            assert trip.origin_site_id == new_home_id
            assert trip.destination_site_id == new_client_id
            assert trip.origin_name == "New Home"
            assert trip.destination_name == "New Client"
            assert trip.start_latitude == Decimal("42.3600000")
            assert trip.end_latitude == Decimal("42.3700000")
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
            home = _site("Home", "42.3314000", "-83.0458000")
            client_site = _site("Client", "42.3440000", "-83.0600000")
            db.add_all([home, client_site])
            db.flush()
            db.add(
                Trip(
                    trip_date=datetime(2026, 6, 10, tzinfo=UTC).date(),
                    origin_site_id=home.id,
                    destination_site_id=client_site.id,
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
            home_id = home.id
            client_site_id = client_site.id

        page_response = client.get("/trips?year=2026&month=6")
        response = client.post(
            "/trips/1",
            data={
                "trip_date": "2026-06-10",
                "origin_site_id": str(home_id),
                "destination_site_id": str(client_site_id),
                "miles": "5.00",
                "start_odometer_miles": "3000.111",
                "end_odometer_miles": "",
            },
        )

        assert page_response.status_code == 200
        assert '<span class="trip-date">2026-06-10</span>' in page_response.text
        assert 'class="date-input" type="date" name="trip_date"' not in page_response.text
        assert 'name="origin_site_id"' in page_response.text
        assert 'name="destination_site_id"' in page_response.text
        assert f'value="{home_id}" selected' in page_response.text
        assert f'value="{client_site_id}" selected' in page_response.text
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
            assert trip.origin_site_id == home_id
            assert trip.destination_site_id == client_site_id
            assert trip.origin_name == "Home"
            assert trip.destination_name == "Client"
            assert trip.miles == Decimal("5.00")
            assert trip.start_odometer_miles is None
            assert trip.end_odometer_miles is None
            assert trip.source == "auto"
            assert trip.mileage_source == "owntracks_path"
    finally:
        app.dependency_overrides.clear()


def test_trips_page_waypoint_dropdowns_preselect_matching_trip_names() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            home = _site("Home", "42.3314000", "-83.0458000")
            client_site = _site("Client", "42.3440000", "-83.0600000")
            db.add_all([home, client_site])
            db.flush()
            db.add(
                Trip(
                    trip_date=datetime(2026, 6, 10, tzinfo=UTC).date(),
                    started_at=datetime(2026, 6, 10, 13, 0, tzinfo=UTC),
                    ended_at=datetime(2026, 6, 10, 13, 30, tzinfo=UTC),
                    start_latitude=Decimal("0.0000000"),
                    start_longitude=Decimal("0.0000000"),
                    end_latitude=Decimal("0.0000000"),
                    end_longitude=Decimal("0.0000000"),
                    origin_name="Home",
                    destination_name="Client",
                    miles=Decimal("5.0"),
                    source="manual",
                )
            )
            db.commit()
            home_id = home.id
            client_site_id = client_site.id

        response = client.get("/trips?year=2026&month=6")

        assert response.status_code == 200
        assert f'<option value="{home_id}" selected>Home</option>' in response.text
        assert f'<option value="{client_site_id}" selected>Client</option>' in response.text
    finally:
        app.dependency_overrides.clear()


def test_trips_page_distance_edit_resequences_month_odometers() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            home = _site("Home", "42.3314000", "-83.0458000")
            client_a = _site("Client A", "42.3440000", "-83.0600000")
            client_b = _site("Client B", "42.3600000", "-83.0700000")
            db.add_all([home, client_a, client_b])
            db.flush()
            db.add_all(
                [
                    Trip(
                        trip_date=datetime(2026, 6, 10, tzinfo=UTC).date(),
                        origin_site_id=home.id,
                        destination_site_id=client_a.id,
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
                        origin_site_id=client_a.id,
                        destination_site_id=client_b.id,
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
            home_id = home.id
            client_a_id = client_a.id

        response = client.post(
            "/trips/1",
            data={
                "trip_date": "2026-06-10",
                "origin_site_id": str(home_id),
                "destination_site_id": str(client_a_id),
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


def test_diagnostics_manual_odometer_card_shows_current_odometer() -> None:
    now = datetime(2026, 6, 20, 13, 30, tzinfo=UTC)
    rendered = templates.env.get_template("diagnostics.html").render(
        {
            "settings": Settings(database_url="sqlite://"),
            "database_url": "sqlite://",
            "location_count": 0,
            "site_count": 0,
            "trip_count": 0,
            "gas_snapshot_count": 0,
            "latest_location": None,
            "last_owntracks_received_at": None,
            "last_owntracks_received_age": "Never",
            "latest_snapshot": None,
            "latest_monthly_gas": None,
            "latest_odometer": {
                "value": Decimal("43210.4"),
                "source": "owntracks_estimate",
                "position": "Rolling",
                "recorded_at": now,
            },
            "disk_usages": [
                SimpleNamespace(
                    primary_path="/data/logs",
                    paths=("/data/logs",),
                    total_display="100.0 GB",
                    used_display="62.0 GB",
                    free_display="38.0 GB",
                    used_percent_display="62.0%",
                    used_percent_style="62.0%",
                )
            ],
            "database_summary": SimpleNamespace(
                size_display="128.0 KB",
                total_records_display="42 records",
            ),
            "recent_locations": [],
            "owntracks_entries_page": SimpleNamespace(
                first_item=0,
                last_item=0,
                total=0,
                has_previous=False,
                has_next=False,
                page=1,
                total_pages=1,
            ),
            "movement_state": SimpleNamespace(
                state="none",
                label="No data",
                site_name=None,
                arrived_at=None,
                detected_at=None,
                distance_miles=None,
            ),
            "movement_state_changes": [],
            "movement_state_changes_page": SimpleNamespace(
                first_item=0,
                last_item=0,
                total=0,
                has_previous=False,
                has_next=False,
                page=1,
                total_pages=1,
            ),
            "app_log_lines": [],
            "login_failure_log_path": "/tmp/mileage-logger-login-failures.log",
            "login_failure_entries": [],
            "login_failure_entries_page": SimpleNamespace(
                first_item=0,
                last_item=0,
                total=0,
                has_previous=False,
                has_next=False,
                page=1,
                total_pages=1,
            ),
            "login_failure_ip_statuses": {},
            "cloudflare_ip_blocks": [],
            "cloudflare_ip_blocks_page": SimpleNamespace(
                first_item=0,
                last_item=0,
                total=0,
                has_previous=False,
                has_next=False,
                page=1,
                total_pages=1,
            ),
            "cloudflare_ip_blocking_configured": False,
            "cloudflare_block_result": None,
            "manual_odometer_result": None,
            "eia_test_result": None,
            "restore_result": None,
            "backup_restore_enabled": False,
            "automatic_backups_enabled": False,
            "automatic_backup_dir": "/tmp/mileage-logger-backups",
            "automatic_backups": [],
            "backup_upload_max_mb": 10,
        }
    )

    manual_card_start = rendered.index("<h2>Manual Odometer</h2>")
    manual_card_end = rendered.index("<h2>EIA API</h2>")
    manual_card = rendered[manual_card_start:manual_card_end]
    assert "Current Odometer" in manual_card
    assert "43210.4 miles" in manual_card
    assert "OwnTracks estimate" in manual_card
    assert "Rolling" in manual_card

    api_tests_start = rendered.index('<section id="api-tests" class="diagnostics-grid">')
    owntracks_card_start = rendered.index('<div id="owntracks-current-state" class="panel">')
    state_log_start = rendered.index('<section id="owntracks-state-log" class="panel">')
    api_tests_section = rendered[api_tests_start:state_log_start]
    assert api_tests_start < manual_card_start < manual_card_end < owntracks_card_start
    assert api_tests_section.count('class="panel"') == 3
    assert "OwnTracks State" in api_tests_section
    assert "Used space as a share of each drive" in rendered
    assert "drive-space-track" in rendered
    assert 'style="width: 62.0%"' in rendered
    hard_drive_start = rendered.index("<h2>Hard Drive Space</h2>")
    latest_records_start = rendered.index("<h2>Latest Records</h2>")
    disk_details_start = rendered.index('<dl class="diagnostic-list">', hard_drive_start)
    database_summary_start = rendered.index('<div class="database-summary">')
    assert hard_drive_start < disk_details_start < database_summary_start < latest_records_start
    database_summary = rendered[database_summary_start:latest_records_start]
    assert "Database Data" in database_summary
    assert "Database Size" in database_summary
    assert "128.0 KB" in database_summary
    assert "Total Records" in database_summary
    assert "42 records" in database_summary

    app_log_start = rendered.index('<section id="app-log" class="panel log-panel">')
    backup_start = rendered.index('<section id="data-backup" class="panel">')
    assert app_log_start < backup_start
    assert "Full Data Backup" in rendered[backup_start:]


def test_diagnostics_compact_table_and_log_styles() -> None:
    stylesheet = Path("mileage_logger/web/static/styles.css").read_text(encoding="utf-8")

    assert ".automatic-backup-table .backup-file-name" in stylesheet
    assert "text-overflow: ellipsis;" in stylesheet
    assert ".log-view {\n  height: 450px;" in stylesheet
    assert "  .log-view {\n    height: 42vh;" in stylesheet


def test_diagnostics_disk_usage_combines_paths_on_same_drive(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    backup_dir = log_dir / "backups"
    other_dir = tmp_path / "other"
    backup_dir.mkdir(parents=True)
    other_dir.mkdir()

    def fake_disk_usage(path):
        if path in {log_dir, backup_dir}:
            return SimpleNamespace(total=1_000, used=600, free=400)
        return SimpleNamespace(total=2_000, used=500, free=1_500)

    disk_usages = _diagnostic_disk_usages(
        (str(log_dir), str(backup_dir), str(other_dir)),
        disk_usage_func=fake_disk_usage,
    )

    assert len(disk_usages) == 2
    combined_disk = next(item for item in disk_usages if item.total_bytes == 1_000)
    assert combined_disk.paths == (str(log_dir), str(backup_dir))
    assert combined_disk.free_bytes == 400
    assert combined_disk.used_percent_style == "60.0%"


def test_diagnostics_database_summary_counts_all_app_records() -> None:
    db = _session()
    _seed_full_backup_data(db)

    summary = _diagnostic_database_summary(db, "sqlite://")

    assert summary.total_records == 7
    assert summary.total_records_display == "7 records"
    assert summary.size_bytes is not None
    assert summary.size_bytes > 0
    assert summary.size_display.endswith(("B", "KB", "MB"))


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
