import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from mileage_logger.config import Settings
from mileage_logger.services.owntracks_buffer import (
    OwnTracksBufferFailoverState,
    OwnTracksBufferStats,
)

LOCAL_POSTGRES_HOSTS = {"", "localhost", "127.0.0.1", "::1", "postgres"}


@dataclass(frozen=True)
class RuntimeDatabaseStatus:
    """Database availability and configured endpoint placement for status cards."""

    available: bool
    engine_label: str
    placement_label: str
    host_label: str

    @property
    def indicator_class(self) -> str:
        """Return the CSS state class for the status indicator dot."""

        return "good" if self.available else "bad"

    @property
    def state_label(self) -> str:
        """Return a short database state label."""

        return "Reachable" if self.available else "Unavailable"

    @property
    def detail_label(self) -> str:
        """Return a compact endpoint summary."""

        if self.host_label:
            return f"{self.placement_label} - {self.host_label}"
        return self.placement_label


@dataclass(frozen=True)
class RuntimeBufferStatus:
    """Buffer availability and visible queued-payload count for status cards."""

    label: str
    available: bool
    queued_count: int

    @property
    def indicator_class(self) -> str:
        """Return the CSS state class for the status indicator dot."""

        return "good" if self.available else "bad"

    @property
    def state_label(self) -> str:
        """Return a short buffer state label."""

        return "Available" if self.available else "Unavailable"

    @property
    def queued_label(self) -> str:
        """Return the queued payload count for display."""

        return f"{self.queued_count} queued"


@dataclass(frozen=True)
class RuntimeStatus:
    """Combined database and OwnTracks buffer status for web diagnostics."""

    database: RuntimeDatabaseStatus
    primary_buffer: RuntimeBufferStatus
    backup_buffer: RuntimeBufferStatus
    buffer_stats: OwnTracksBufferStats


def build_runtime_status(
    settings: Settings,
    *,
    database_available: bool,
) -> RuntimeStatus:
    """Build a database and OwnTracks buffer status snapshot without querying PostgreSQL."""

    buffer_stats = _read_runtime_buffer_stats(settings)
    return RuntimeStatus(
        database=_database_status(settings, available=database_available),
        primary_buffer=RuntimeBufferStatus(
            label="Primary Buffer",
            available=buffer_stats.primary_available,
            queued_count=buffer_stats.primary_queued_count,
        ),
        backup_buffer=RuntimeBufferStatus(
            label="Backup Buffer",
            available=buffer_stats.fallback_available,
            queued_count=buffer_stats.fallback_queued_count,
        ),
        buffer_stats=buffer_stats,
    )


def _database_status(settings: Settings, *, available: bool) -> RuntimeDatabaseStatus:
    """Describe the configured database endpoint and whether it is reachable."""

    try:
        parsed_url = make_url(settings.database_url)
    except ArgumentError:
        return RuntimeDatabaseStatus(
            available=available,
            engine_label="Database",
            placement_label="Invalid URL",
            host_label="",
        )
    backend_name = parsed_url.get_backend_name()
    if backend_name != "postgresql":
        return RuntimeDatabaseStatus(
            available=available,
            engine_label=backend_name.upper(),
            placement_label="Local test database" if backend_name == "sqlite" else "Configured",
            host_label=str(parsed_url.database or ""),
        )

    host = (parsed_url.host or "").strip()
    normalized_host = host.casefold()
    placement = "Local/Bundled PostgreSQL"
    if normalized_host not in LOCAL_POSTGRES_HOSTS:
        placement = "Remote PostgreSQL"
    return RuntimeDatabaseStatus(
        available=available,
        engine_label="PostgreSQL",
        placement_label=placement,
        host_label=host or "local socket",
    )


@dataclass(frozen=True)
class _BufferSnapshot:
    available: bool
    queued_count: int = 0
    oldest_received_at: str | None = None
    newest_received_at: str | None = None
    last_error: str | None = None


def _read_runtime_buffer_stats(settings: Settings) -> OwnTracksBufferStats:
    """Read buffer status without creating SQLite files or directories."""

    if not settings.owntracks_buffer_enabled:
        return OwnTracksBufferStats(
            queued_count=0,
            primary_available=False,
            fallback_available=False,
        )
    primary = _read_buffer_snapshot(settings.owntracks_buffer_path)
    backup = _read_buffer_snapshot(settings.owntracks_buffer_fallback_path)
    state = OwnTracksBufferFailoverState(settings.owntracks_buffer_fallback_path)
    received_values = [
        value
        for value in (
            primary.oldest_received_at,
            backup.oldest_received_at,
        )
        if value
    ]
    newest_values = [
        value
        for value in (
            primary.newest_received_at,
            backup.newest_received_at,
        )
        if value
    ]
    errors = [value for value in (primary.last_error, backup.last_error) if value]
    return OwnTracksBufferStats(
        queued_count=primary.queued_count + backup.queued_count,
        oldest_received_at=min(received_values) if received_values else None,
        newest_received_at=max(newest_values) if newest_values else None,
        last_error=errors[0] if errors else None,
        primary_queued_count=primary.queued_count,
        fallback_queued_count=backup.queued_count,
        primary_available=primary.available,
        fallback_available=backup.available,
        active_buffer="primary" if primary.available else "fallback",
        replay_waiting_for_primary=(
            not primary.available
            and backup.queued_count > 0
            and not state.fallback_replay_allowed_with_primary_down()
        ),
    )


def _read_buffer_snapshot(path_text: str) -> _BufferSnapshot:
    """Read one SQLite buffer file if it exists, without creating it."""

    path = Path(path_text)
    if not path.exists():
        return _BufferSnapshot(available=_buffer_parent_available(path))
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            table_exists = connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'owntracks_payload_buffer'
                """
            ).fetchone()
            if table_exists is None:
                return _BufferSnapshot(available=True)
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS queued_count,
                    MIN(received_at) AS oldest_received_at,
                    MAX(received_at) AS newest_received_at
                FROM owntracks_payload_buffer
                """
            ).fetchone()
            error_row = connection.execute(
                """
                SELECT last_error
                FROM owntracks_payload_buffer
                WHERE last_error IS NOT NULL AND last_error != ''
                ORDER BY last_attempt_at DESC, id ASC
                LIMIT 1
                """
            ).fetchone()
    except Exception as exc:
        return _BufferSnapshot(available=False, last_error=str(exc))
    return _BufferSnapshot(
        available=True,
        queued_count=int(row["queued_count"] or 0),
        oldest_received_at=row["oldest_received_at"],
        newest_received_at=row["newest_received_at"],
        last_error=error_row["last_error"] if error_row else None,
    )


def _buffer_parent_available(path: Path) -> bool:
    """Return whether the configured buffer file's parent path is present and writable."""

    parent = path.parent
    return parent.exists() and parent.is_dir()
