import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread

from sqlalchemy.orm import Session

from mileage_logger.config import Settings, get_settings
from mileage_logger.database import SessionLocal, is_database_unavailable_error
from mileage_logger.database_migrations import run_migrations_once_on_reconnect
from mileage_logger.services.owntracks import process_owntracks_payload

logger = logging.getLogger(__name__)


class OwnTracksBufferUnavailable(RuntimeError):
    """Raised when no configured OwnTracks buffer can accept a payload."""


@dataclass(frozen=True)
class OwnTracksBufferEntry:
    """One buffered OwnTracks payload waiting for database replay."""

    id: int
    received_at: str
    body: bytes
    topic: str | None
    user: str | None
    device: str | None
    source: str
    attempt_count: int
    buffer_name: str = "primary"


@dataclass(frozen=True)
class OwnTracksBufferStats:
    """Small status snapshot for the local OwnTracks queue."""

    queued_count: int
    oldest_received_at: str | None = None
    newest_received_at: str | None = None
    last_error: str | None = None
    primary_queued_count: int = 0
    fallback_queued_count: int = 0
    primary_available: bool = True
    fallback_available: bool = True
    active_buffer: str = "primary"
    replay_waiting_for_primary: bool = False


@dataclass(frozen=True)
class OwnTracksIngestOutcome:
    """Result of accepting an OwnTracks payload through direct DB write or buffer."""

    buffered: bool
    queue_id: int | None = None
    reason: str = ""
    buffer_name: str = ""


@dataclass(frozen=True)
class OwnTracksBufferReplayResult:
    """Result from one buffer replay pass."""

    processed_count: int
    remaining_count: int
    error: str = ""
    waiting_for_primary: bool = False


@dataclass(frozen=True)
class OwnTracksBufferStatus:
    """Availability and queue depths for both OwnTracks buffer stores."""

    primary_available: bool
    fallback_available: bool
    primary_count: int = 0
    fallback_count: int = 0
    primary_error: str = ""
    fallback_error: str = ""

    @property
    def queued_count(self) -> int:
        """Return the number of queued entries visible from available buffers."""

        return self.primary_count + self.fallback_count

    @property
    def has_known_pending_entries(self) -> bool:
        """Return whether either readable queue has entries waiting for replay."""

        return self.queued_count > 0


class OwnTracksBufferFailoverState:
    """Persist fallback-ordering metadata beside the local fallback queue."""

    def __init__(self, fallback_path: str | Path) -> None:
        fallback_path = Path(fallback_path)
        self.path = fallback_path.with_name(f"{fallback_path.stem}.state.json")

    def record_primary_available_count(self, count: int) -> None:
        """Remember whether the primary queue had pending work while readable."""

        data = self._read()
        data["primary_had_pending_when_last_available"] = count > 0
        data["updated_at"] = datetime.now(UTC).isoformat()
        self._write(data)

    def record_primary_pending(self) -> None:
        """Remember that the primary queue contains entries that must replay first."""

        data = self._read()
        data["primary_had_pending_when_last_available"] = True
        data["updated_at"] = datetime.now(UTC).isoformat()
        self._write(data)

    def record_fallback_enqueue(
        self,
        *,
        primary_unavailable_before_database_outage: bool | None,
    ) -> None:
        """Record fallback usage and whether primary-down replay is order-safe."""

        data = self._read()
        primary_had_pending = data.get("primary_had_pending_when_last_available", False)
        existing_safe = data.get("primary_unavailable_before_database_outage", False)
        if primary_unavailable_before_database_outage is None:
            safe_to_replay = bool(existing_safe and not primary_had_pending)
        else:
            safe_to_replay = bool(
                primary_unavailable_before_database_outage and not primary_had_pending
            )
            if data.get("fallback_used", False):
                safe_to_replay = bool(existing_safe and safe_to_replay)
        data["fallback_used"] = True
        data["primary_unavailable_before_database_outage"] = safe_to_replay
        data["updated_at"] = datetime.now(UTC).isoformat()
        self._write(data)

    def fallback_replay_allowed_with_primary_down(self) -> bool:
        """Return whether fallback entries may replay before primary recovers."""

        data = self._read()
        return bool(
            data.get("fallback_used", False)
            and data.get("primary_unavailable_before_database_outage", False)
            and not data.get("primary_had_pending_when_last_available", False)
        )

    def clear_if_drained(self, status: OwnTracksBufferStatus) -> None:
        """Remove stale failover metadata after both queues are readable and empty."""

        if (
            status.primary_available
            and status.fallback_available
            and status.primary_count == 0
            and status.fallback_count == 0
        ):
            try:
                self.path.unlink()
            except FileNotFoundError:
                return
            except Exception as exc:
                logger.warning("Could not clear OwnTracks buffer failover state: %s", exc)

    def _read(self) -> dict[str, object]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning("Could not read OwnTracks buffer failover state: %s", exc)
            return {}

    def _write(self, data: dict[str, object]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
            temp_path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
            temp_path.replace(self.path)
        except Exception as exc:
            logger.warning("Could not write OwnTracks buffer failover state: %s", exc)


class OwnTracksPayloadBuffer:
    """Persistent FIFO SQLite queue for OwnTracks payloads accepted during DB outages."""

    def __init__(self, path: str | Path, *, name: str = "primary") -> None:
        self.path = Path(path)
        self.name = name

    def initialize(self) -> None:
        """Create the local buffer database and table if needed."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS owntracks_payload_buffer (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at TEXT NOT NULL,
                    body BLOB NOT NULL,
                    topic TEXT,
                    user TEXT,
                    device TEXT,
                    source TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT,
                    last_error TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_owntracks_payload_buffer_id
                ON owntracks_payload_buffer (id)
                """
            )

    def enqueue(
        self,
        body: bytes,
        *,
        topic: str | None = None,
        user: str | None = None,
        device: str | None = None,
        source: str = "http",
    ) -> int:
        """Append an OwnTracks payload to the durable FIFO buffer."""

        self.initialize()
        received_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO owntracks_payload_buffer
                    (received_at, body, topic, user, device, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (received_at, body, topic, user, device, source),
            )
            queue_id = int(cursor.lastrowid)
        logger.warning(
            "Buffered OwnTracks payload buffer=%s queue_id=%s source=%s topic=%s user=%s device=%s",
            self.name,
            queue_id,
            source,
            topic or "",
            user or "",
            device or "",
        )
        return queue_id

    def count(self) -> int:
        self.initialize()
        with self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM owntracks_payload_buffer"
                ).fetchone()[0]
            )

    def stats(self) -> OwnTracksBufferStats:
        self.initialize()
        with self._connect() as connection:
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
        return OwnTracksBufferStats(
            queued_count=int(row["queued_count"] or 0),
            oldest_received_at=row["oldest_received_at"],
            newest_received_at=row["newest_received_at"],
            last_error=error_row["last_error"] if error_row else None,
        )

    def peek_batch(self, limit: int) -> list[OwnTracksBufferEntry]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, received_at, body, topic, user, device, source, attempt_count
                FROM owntracks_payload_buffer
                ORDER BY received_at ASC, id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            OwnTracksBufferEntry(
                id=int(row["id"]),
                received_at=row["received_at"],
                body=bytes(row["body"]),
                topic=row["topic"],
                user=row["user"],
                device=row["device"],
                source=row["source"],
                attempt_count=int(row["attempt_count"]),
                buffer_name=self.name,
            )
            for row in rows
        ]

    def delete(self, queue_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM owntracks_payload_buffer WHERE id = ?", (queue_id,))

    def record_failure(self, queue_id: int, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE owntracks_payload_buffer
                SET
                    attempt_count = attempt_count + 1,
                    last_attempt_at = ?,
                    last_error = ?
                WHERE id = ?
                """,
                (datetime.now(UTC).isoformat(), error[:500], queue_id),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


class OwnTracksBufferManager:
    """Coordinate primary and fallback OwnTracks queues without breaking receive order."""

    def __init__(self, settings: Settings) -> None:
        self.primary = OwnTracksPayloadBuffer(settings.owntracks_buffer_path, name="primary")
        self.fallback = OwnTracksPayloadBuffer(
            settings.owntracks_buffer_fallback_path,
            name="fallback",
        )
        self.state = OwnTracksBufferFailoverState(settings.owntracks_buffer_fallback_path)

    def initialize(self) -> None:
        """Initialize available buffer stores while tolerating primary/NFS outages."""

        try:
            self.fallback.initialize()
        except Exception as exc:
            raise OwnTracksBufferUnavailable(
                f"OwnTracks fallback buffer is unavailable: {exc}"
            ) from exc
        try:
            self.primary.initialize()
        except Exception as exc:
            logger.warning("OwnTracks primary buffer is unavailable: %s", exc)

    def status(self) -> OwnTracksBufferStatus:
        """Return queue counts and availability for both buffer stores."""

        primary_available, primary_count, primary_error = self._safe_count(self.primary)
        fallback_available, fallback_count, fallback_error = self._safe_count(self.fallback)
        status = OwnTracksBufferStatus(
            primary_available=primary_available,
            fallback_available=fallback_available,
            primary_count=primary_count,
            fallback_count=fallback_count,
            primary_error=primary_error,
            fallback_error=fallback_error,
        )
        if primary_available:
            self.state.record_primary_available_count(primary_count)
        self.state.clear_if_drained(status)
        return status

    def stats(self) -> OwnTracksBufferStats:
        """Return combined queue stats for limp-mode UI without touching PostgreSQL."""

        status = self.status()
        primary_stats = self._safe_stats(self.primary) if status.primary_available else None
        fallback_stats = self._safe_stats(self.fallback) if status.fallback_available else None
        received_values = [
            value
            for value in (
                primary_stats.oldest_received_at if primary_stats else None,
                fallback_stats.oldest_received_at if fallback_stats else None,
            )
            if value
        ]
        newest_values = [
            value
            for value in (
                primary_stats.newest_received_at if primary_stats else None,
                fallback_stats.newest_received_at if fallback_stats else None,
            )
            if value
        ]
        errors = [
            value
            for value in (
                primary_stats.last_error if primary_stats else status.primary_error,
                fallback_stats.last_error if fallback_stats else status.fallback_error,
            )
            if value
        ]
        waiting_for_primary = self.replay_waiting_for_primary(status)
        return OwnTracksBufferStats(
            queued_count=status.queued_count,
            oldest_received_at=min(received_values) if received_values else None,
            newest_received_at=max(newest_values) if newest_values else None,
            last_error=errors[0] if errors else None,
            primary_queued_count=status.primary_count,
            fallback_queued_count=status.fallback_count,
            primary_available=status.primary_available,
            fallback_available=status.fallback_available,
            active_buffer="primary" if status.primary_available else "fallback",
            replay_waiting_for_primary=waiting_for_primary,
        )

    def enqueue(
        self,
        body: bytes,
        *,
        topic: str | None = None,
        user: str | None = None,
        device: str | None = None,
        source: str = "http",
        force_fallback: bool = False,
        primary_unavailable_before_database_outage: bool | None = None,
    ) -> tuple[int, str]:
        """Append a payload to the primary queue, falling back to local storage if needed."""

        if not force_fallback:
            try:
                queue_id = self.primary.enqueue(
                    body,
                    topic=topic,
                    user=user,
                    device=device,
                    source=source,
                )
                self.state.record_primary_pending()
                return queue_id, self.primary.name
            except Exception as exc:
                logger.warning("OwnTracks primary buffer write failed; using fallback: %s", exc)
                primary_unavailable_before_database_outage = False
        try:
            queue_id = self.fallback.enqueue(
                body,
                topic=topic,
                user=user,
                device=device,
                source=source,
            )
        except Exception as exc:
            raise OwnTracksBufferUnavailable(
                f"OwnTracks fallback buffer is unavailable: {exc}"
            ) from exc
        self.state.record_fallback_enqueue(
            primary_unavailable_before_database_outage=(
                primary_unavailable_before_database_outage
            )
        )
        return queue_id, self.fallback.name

    def replay_waiting_for_primary(self, status: OwnTracksBufferStatus) -> bool:
        """Return whether fallback replay must wait for primary queue recovery."""

        return (
            not status.primary_available
            and status.fallback_count > 0
            and not self.state.fallback_replay_allowed_with_primary_down()
        )

    def first_replayable_entry(
        self,
        status: OwnTracksBufferStatus | None = None,
    ) -> tuple[OwnTracksPayloadBuffer, OwnTracksBufferEntry] | None:
        """Return the next queue entry that can replay without violating ordering."""

        current_status = status or self.status()
        if not current_status.primary_available:
            if (
                current_status.fallback_available
                and current_status.fallback_count > 0
                and self.state.fallback_replay_allowed_with_primary_down()
            ):
                return self._first_entry(self.fallback)
            return None
        if not current_status.fallback_available:
            return self._first_entry(self.primary)
        primary_entry = self._first_entry(self.primary) if current_status.primary_count else None
        fallback_entry = (
            self._first_entry(self.fallback) if current_status.fallback_count else None
        )
        if primary_entry and fallback_entry:
            return min(
                (primary_entry, fallback_entry),
                key=lambda item: self._entry_sort_key(item[1]),
            )
        return primary_entry or fallback_entry

    def visible_count(self) -> int:
        """Return the number of queued entries visible from available buffers."""

        return self.status().queued_count

    @staticmethod
    def _entry_sort_key(entry: OwnTracksBufferEntry) -> tuple[str, int, int]:
        buffer_rank = 0 if entry.buffer_name == "primary" else 1
        return (entry.received_at, buffer_rank, entry.id)

    @staticmethod
    def _first_entry(
        payload_buffer: OwnTracksPayloadBuffer,
    ) -> tuple[OwnTracksPayloadBuffer, OwnTracksBufferEntry] | None:
        entries = payload_buffer.peek_batch(1)
        if not entries:
            return None
        return payload_buffer, entries[0]

    @staticmethod
    def _safe_count(payload_buffer: OwnTracksPayloadBuffer) -> tuple[bool, int, str]:
        try:
            return True, payload_buffer.count(), ""
        except Exception as exc:
            logger.warning(
                "OwnTracks %s buffer is unavailable while counting: %s",
                payload_buffer.name,
                exc,
            )
            return False, 0, str(exc)

    @staticmethod
    def _safe_stats(payload_buffer: OwnTracksPayloadBuffer) -> OwnTracksBufferStats:
        return payload_buffer.stats()


def owntracks_buffer(settings: Settings | None = None) -> OwnTracksPayloadBuffer:
    active_settings = settings or get_settings()
    return OwnTracksPayloadBuffer(active_settings.owntracks_buffer_path, name="primary")


def owntracks_buffer_manager(settings: Settings | None = None) -> OwnTracksBufferManager:
    """Return the OwnTracks buffer manager for primary and fallback queues."""

    active_settings = settings or get_settings()
    return OwnTracksBufferManager(active_settings)


def owntracks_buffer_stats(settings: Settings | None = None) -> OwnTracksBufferStats:
    """Return local OwnTracks buffer status without touching PostgreSQL."""

    active_settings = settings or get_settings()
    if not active_settings.owntracks_buffer_enabled:
        return OwnTracksBufferStats(queued_count=0)
    return owntracks_buffer_manager(active_settings).stats()


def ingest_or_buffer_owntracks_payload(
    body: bytes,
    *,
    topic: str | None = None,
    user: str | None = None,
    device: str | None = None,
    source: str = "http",
    settings: Settings | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> OwnTracksIngestOutcome:
    """Store OwnTracks data directly, or durably buffer it when the DB is unavailable."""

    active_settings = settings or get_settings()
    active_session_factory = session_factory or SessionLocal
    buffer_manager = owntracks_buffer_manager(active_settings)
    initial_status = (
        buffer_manager.status()
        if active_settings.owntracks_buffer_enabled
        else OwnTracksBufferStatus(primary_available=True, fallback_available=True)
    )
    if active_settings.owntracks_buffer_enabled and not initial_status.primary_available:
        queue_id, buffer_name = buffer_manager.enqueue(
            body,
            topic=topic,
            user=user,
            device=device,
            source=source,
            force_fallback=True,
            primary_unavailable_before_database_outage=True,
        )
        return OwnTracksIngestOutcome(
            buffered=True,
            queue_id=queue_id,
            reason=(
                "buffer_not_empty"
                if initial_status.has_known_pending_entries
                else "primary_buffer_unavailable"
            ),
            buffer_name=buffer_name,
        )
    if (
        active_settings.owntracks_buffer_enabled
        and initial_status.has_known_pending_entries
    ):
        queue_id, buffer_name = buffer_manager.enqueue(
            body,
            topic=topic,
            user=user,
            device=device,
            source=source,
            force_fallback=not initial_status.primary_available,
            primary_unavailable_before_database_outage=None,
        )
        return OwnTracksIngestOutcome(
            buffered=True,
            queue_id=queue_id,
            reason="buffer_not_empty",
            buffer_name=buffer_name,
        )

    try:
        with active_session_factory() as db:
            process_owntracks_payload(db, body, topic=topic, user=user, device=device)
    except Exception as exc:
        if not active_settings.owntracks_buffer_enabled or not is_database_unavailable_error(exc):
            raise
        queue_id, buffer_name = buffer_manager.enqueue(
            body,
            topic=topic,
            user=user,
            device=device,
            source=source,
            force_fallback=not initial_status.primary_available,
            primary_unavailable_before_database_outage=not initial_status.primary_available,
        )
        return OwnTracksIngestOutcome(
            buffered=True,
            queue_id=queue_id,
            reason="database_unavailable",
            buffer_name=buffer_name,
        )

    return OwnTracksIngestOutcome(buffered=False)


def replay_owntracks_buffer_once(
    settings: Settings | None = None,
    *,
    session_factory: Callable[[], Session] | None = None,
) -> OwnTracksBufferReplayResult:
    """Replay buffered OwnTracks payloads in receive order until the batch drains or DB fails."""

    active_settings = settings or get_settings()
    active_session_factory = session_factory or SessionLocal
    if not active_settings.owntracks_buffer_enabled:
        return OwnTracksBufferReplayResult(processed_count=0, remaining_count=0)

    buffer_manager = owntracks_buffer_manager(active_settings)
    status = buffer_manager.status()
    if buffer_manager.replay_waiting_for_primary(status):
        logger.info(
            "OwnTracks fallback replay is waiting for primary buffer recovery "
            "to preserve receive order"
        )
        return OwnTracksBufferReplayResult(
            processed_count=0,
            remaining_count=status.queued_count,
            error="waiting for primary OwnTracks buffer to preserve receive order",
            waiting_for_primary=True,
        )
    first_replayable = buffer_manager.first_replayable_entry(status)
    if first_replayable is None:
        return OwnTracksBufferReplayResult(processed_count=0, remaining_count=0)

    if active_settings.database_run_migrations_on_reconnect:
        try:
            run_migrations_once_on_reconnect()
        except Exception as exc:
            logger.warning("Database migrations are not ready for OwnTracks replay: %s", exc)
            failed_buffer, failed_entry = first_replayable
            failed_buffer.record_failure(failed_entry.id, str(exc))
            return OwnTracksBufferReplayResult(
                processed_count=0,
                remaining_count=buffer_manager.visible_count(),
                error=str(exc),
            )

    processed_count = 0
    while processed_count < active_settings.owntracks_buffer_replay_batch_size:
        status = buffer_manager.status()
        if buffer_manager.replay_waiting_for_primary(status):
            return OwnTracksBufferReplayResult(
                processed_count=processed_count,
                remaining_count=status.queued_count,
                error="waiting for primary OwnTracks buffer to preserve receive order",
                waiting_for_primary=True,
            )
        next_replayable = buffer_manager.first_replayable_entry(status)
        if next_replayable is None:
            break
        payload_buffer, entry = next_replayable
        try:
            with active_session_factory() as db:
                process_owntracks_payload(
                    db,
                    entry.body,
                    topic=entry.topic,
                    user=entry.user,
                    device=entry.device,
                )
        except Exception as exc:
            payload_buffer.record_failure(entry.id, str(exc))
            if is_database_unavailable_error(exc):
                logger.warning("Database unavailable during OwnTracks buffer replay: %s", exc)
            else:
                logger.exception(
                    "OwnTracks buffer replay stopped at buffer=%s queue_id=%s",
                    entry.buffer_name,
                    entry.id,
                )
            return OwnTracksBufferReplayResult(
                processed_count=processed_count,
                remaining_count=buffer_manager.visible_count(),
                error=str(exc),
            )
        payload_buffer.delete(entry.id)
        processed_count += 1

    remaining_count = buffer_manager.visible_count()
    if processed_count:
        logger.info(
            "Replayed buffered OwnTracks payloads processed=%s remaining=%s",
            processed_count,
            remaining_count,
        )
    return OwnTracksBufferReplayResult(
        processed_count=processed_count,
        remaining_count=remaining_count,
    )


class OwnTracksBufferReplayer:
    """Background worker that drains the local OwnTracks buffer when PostgreSQL returns."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if not self.settings.owntracks_buffer_enabled:
            logger.info("OwnTracks buffering is disabled")
            return
        if self._thread is not None:
            logger.debug("OwnTracks buffer replayer already running")
            return
        owntracks_buffer_manager(self.settings).initialize()
        self._stop.clear()
        self._thread = Thread(target=self._run, name="owntracks-buffer-replay", daemon=True)
        self._thread.start()
        logger.info(
            "OwnTracks buffer replayer started primary_path=%s fallback_path=%s "
            "interval_seconds=%s batch_size=%s",
            self.settings.owntracks_buffer_path,
            self.settings.owntracks_buffer_fallback_path,
            self.settings.owntracks_buffer_replay_interval_seconds,
            self.settings.owntracks_buffer_replay_batch_size,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
            logger.info("OwnTracks buffer replayer stopped")

    def _run(self) -> None:
        self._process_once()
        while not self._stop.wait(self.settings.owntracks_buffer_replay_interval_seconds):
            self._process_once()

    def _process_once(self) -> None:
        try:
            replay_owntracks_buffer_once(self.settings)
        except Exception:
            logger.exception("OwnTracks buffer replay failed")
