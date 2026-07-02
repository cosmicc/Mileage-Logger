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


@dataclass(frozen=True)
class OwnTracksBufferEntry:
    """One buffered OwnTracks payload waiting for database replay."""

    id: int
    body: bytes
    topic: str | None
    user: str | None
    device: str | None
    source: str
    attempt_count: int


@dataclass(frozen=True)
class OwnTracksBufferStats:
    """Small status snapshot for the local OwnTracks queue."""

    queued_count: int
    oldest_received_at: str | None = None
    newest_received_at: str | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class OwnTracksIngestOutcome:
    """Result of accepting an OwnTracks payload through direct DB write or buffer."""

    buffered: bool
    queue_id: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class OwnTracksBufferReplayResult:
    """Result from one buffer replay pass."""

    processed_count: int
    remaining_count: int
    error: str = ""


class OwnTracksPayloadBuffer:
    """Persistent FIFO SQLite queue for OwnTracks payloads accepted during DB outages."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

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
            "Buffered OwnTracks payload queue_id=%s source=%s topic=%s user=%s device=%s",
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
                SELECT id, body, topic, user, device, source, attempt_count
                FROM owntracks_payload_buffer
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            OwnTracksBufferEntry(
                id=int(row["id"]),
                body=bytes(row["body"]),
                topic=row["topic"],
                user=row["user"],
                device=row["device"],
                source=row["source"],
                attempt_count=int(row["attempt_count"]),
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


def owntracks_buffer(settings: Settings | None = None) -> OwnTracksPayloadBuffer:
    active_settings = settings or get_settings()
    return OwnTracksPayloadBuffer(active_settings.owntracks_buffer_path)


def owntracks_buffer_stats(settings: Settings | None = None) -> OwnTracksBufferStats:
    """Return local OwnTracks buffer status without touching PostgreSQL."""

    active_settings = settings or get_settings()
    if not active_settings.owntracks_buffer_enabled:
        return OwnTracksBufferStats(queued_count=0)
    return owntracks_buffer(active_settings).stats()


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
    payload_buffer = owntracks_buffer(active_settings)
    if active_settings.owntracks_buffer_enabled and payload_buffer.count() > 0:
        queue_id = payload_buffer.enqueue(
            body,
            topic=topic,
            user=user,
            device=device,
            source=source,
        )
        return OwnTracksIngestOutcome(
            buffered=True,
            queue_id=queue_id,
            reason="buffer_not_empty",
        )

    try:
        with active_session_factory() as db:
            process_owntracks_payload(db, body, topic=topic, user=user, device=device)
    except Exception as exc:
        if not active_settings.owntracks_buffer_enabled or not is_database_unavailable_error(exc):
            raise
        queue_id = payload_buffer.enqueue(
            body,
            topic=topic,
            user=user,
            device=device,
            source=source,
        )
        return OwnTracksIngestOutcome(
            buffered=True,
            queue_id=queue_id,
            reason="database_unavailable",
        )

    return OwnTracksIngestOutcome(buffered=False)


def replay_owntracks_buffer_once(
    settings: Settings | None = None,
    *,
    session_factory: Callable[[], Session] | None = None,
) -> OwnTracksBufferReplayResult:
    """Replay buffered OwnTracks payloads in FIFO order until the batch is drained or DB fails."""

    active_settings = settings or get_settings()
    active_session_factory = session_factory or SessionLocal
    if not active_settings.owntracks_buffer_enabled:
        return OwnTracksBufferReplayResult(processed_count=0, remaining_count=0)

    payload_buffer = owntracks_buffer(active_settings)
    entries = payload_buffer.peek_batch(active_settings.owntracks_buffer_replay_batch_size)
    if not entries:
        return OwnTracksBufferReplayResult(processed_count=0, remaining_count=0)

    if active_settings.database_run_migrations_on_reconnect:
        try:
            run_migrations_once_on_reconnect()
        except Exception as exc:
            logger.warning("Database migrations are not ready for OwnTracks replay: %s", exc)
            payload_buffer.record_failure(entries[0].id, str(exc))
            return OwnTracksBufferReplayResult(
                processed_count=0,
                remaining_count=payload_buffer.count(),
                error=str(exc),
            )

    processed_count = 0
    for entry in entries:
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
                logger.exception("OwnTracks buffer replay stopped at queue_id=%s", entry.id)
            return OwnTracksBufferReplayResult(
                processed_count=processed_count,
                remaining_count=payload_buffer.count(),
                error=str(exc),
            )
        payload_buffer.delete(entry.id)
        processed_count += 1

    remaining_count = payload_buffer.count()
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
        owntracks_buffer(self.settings).initialize()
        self._stop.clear()
        self._thread = Thread(target=self._run, name="owntracks-buffer-replay", daemon=True)
        self._thread.start()
        logger.info(
            "OwnTracks buffer replayer started path=%s interval_seconds=%s batch_size=%s",
            self.settings.owntracks_buffer_path,
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
