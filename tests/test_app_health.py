from datetime import UTC, datetime
from types import SimpleNamespace

from mileage_logger.config import Settings
from mileage_logger.services.app_health import (
    AppHealthIssue,
    AppHealthSnapshot,
    PushoverAppHealthNotifier,
    build_app_health_snapshot,
    pushover_configured,
)
from mileage_logger.services.runtime_status import (
    RuntimeDatabaseStatus,
    RuntimeStatus,
)


def _runtime_status(
    *,
    database_available: bool = True,
) -> RuntimeStatus:
    return RuntimeStatus(
        database=RuntimeDatabaseStatus(
            available=database_available,
            engine_label="PostgreSQL",
            placement_label="Remote PostgreSQL",
            host_label="db.internal",
        ),
    )


def test_app_health_snapshot_tracks_degraded_signals() -> None:
    settings = Settings(
        app_health_db_latency_warning_ms=100,
        app_health_db_latency_critical_ms=500,
        app_health_disk_warning_percent=80,
        app_health_disk_critical_percent=95,
    )

    snapshot = build_app_health_snapshot(
        settings=settings,
        runtime_status=_runtime_status(),
        database_latency_ms=150,
        disk_usages=[SimpleNamespace(primary_path="/data/logs", used_bytes=90, total_bytes=100)],
        active_lockout_count=2,
        cloudflare_block_count=1,
    )

    issue_keys = {issue.key for issue in snapshot.issues}
    assert snapshot.status == "degraded"
    assert snapshot.severity == "warning"
    assert "database.latency_warning" in issue_keys
    assert "disk./data/logs" in issue_keys
    assert "security.login_lockout" in issue_keys
    assert "security.cloudflare_blocks" in issue_keys


def test_app_health_snapshot_marks_database_outage_unavailable() -> None:
    snapshot = build_app_health_snapshot(
        settings=Settings(),
        runtime_status=_runtime_status(database_available=False),
        database_latency_ms=None,
        active_lockout_count=0,
        cloudflare_block_count=0,
    )

    assert snapshot.status == "unavailable"
    assert snapshot.severity == "critical"
    assert snapshot.banner_class == "critical"
    assert snapshot.banner_title == "App Unavailable"
    assert "database.unavailable" in {issue.key for issue in snapshot.issues}


def test_pushover_notifier_sends_degraded_once_and_restored(monkeypatch, tmp_path) -> None:
    settings = Settings(
        pushover_enabled=True,
        pushover_token="app-token",
        pushover_user="user-key",
        app_health_state_path=str(tmp_path / "app-health-state.json"),
    )
    calls: list[dict] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, int]:
            return {"status": 1}

    def fake_post(_url, *, data, timeout):
        calls.append({"data": data, "timeout": timeout})
        return Response()

    monkeypatch.setattr("mileage_logger.services.app_health.httpx.post", fake_post)
    degraded = AppHealthSnapshot(
        status="degraded",
        severity="warning",
        issues=(
            AppHealthIssue(
                key="database.latency_warning",
                severity="warning",
                title="Database latency elevated",
                detail="Database round trip is 150.0 ms.",
            ),
        ),
        checked_at=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    restored = AppHealthSnapshot(
        status="ok",
        severity="ok",
        issues=(),
        checked_at=datetime(2026, 7, 4, 12, 5, tzinfo=UTC),
    )

    notifier = PushoverAppHealthNotifier(settings)

    assert notifier.notify_if_needed(degraded) is True
    assert notifier.notify_if_needed(degraded) is False
    assert notifier.notify_if_needed(restored) is True

    assert [call["data"]["title"] for call in calls] == [
        "Mileage Logger degraded",
        "Mileage Logger restored",
    ]
    assert calls[0]["data"]["token"] == "app-token"
    assert calls[0]["data"]["user"] == "user-key"


def test_pushover_accepts_app_and_user_key_aliases() -> None:
    settings = Settings(
        pushover_enabled=True,
        pushover_app_key="alias-app-token",
        pushover_user_key="alias-user-key",
    )

    assert pushover_configured(settings)
