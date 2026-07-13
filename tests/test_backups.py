"""Automatic backup scheduler regression tests."""

import asyncio
import errno

import pytest

from mileage_logger.config import Settings
from mileage_logger.services import backups


def test_automatic_backup_scheduler_retries_stale_storage_until_success(
    monkeypatch,
    tmp_path,
) -> None:
    """A stale shared mount retries quickly, then resumes the normal interval."""

    settings = Settings(
        automatic_backup_dir=str(tmp_path / "backups"),
        automatic_backup_retry_seconds=60,
    )
    attempted_reasons: list[str] = []
    sleep_delays: list[float] = []

    monkeypatch.setattr(backups.database, "database_is_reachable", lambda: True)

    def fake_backup_once(_settings: Settings, *, reason: str):
        attempted_reasons.append(reason)
        if len(attempted_reasons) == 1:
            raise OSError(errno.ESTALE, "Stale file handle")
        return object()

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)
        if len(sleep_delays) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(backups, "run_automatic_backup_once", fake_backup_once)
    monkeypatch.setattr(backups.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(backups.automatic_backup_scheduler(settings))

    assert attempted_reasons == ["startup", "startup"]
    assert sleep_delays == [60, backups.AUTOMATIC_BACKUP_INTERVAL_SECONDS]
