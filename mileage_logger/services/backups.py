"""Portable full-data backup and restore helpers for Mileage Logger."""

import gzip
import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import insert, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.schema import Column, Table
from sqlalchemy.sql.sqltypes import Date, DateTime, Numeric

from mileage_logger.models import Base, Site
from mileage_logger.services.waypoints import owntracks_waypoints_export

logger = logging.getLogger(__name__)

BACKUP_FORMAT = "mileage_logger.full_backup"
BACKUP_VERSION = 1
BACKUP_MEDIA_TYPE = "application/gzip"
BACKUP_UPLOAD_MAX_BYTES = 250 * 1024 * 1024
BACKUP_TABLES = tuple(Base.metadata.sorted_tables)
BACKUP_TABLE_NAMES = tuple(table.name for table in BACKUP_TABLES)
_BACKUP_RESTORE_LOCK = threading.Lock()


class BackupValidationError(ValueError):
    """Raised when an uploaded backup is not a valid Mileage Logger backup."""


@dataclass(frozen=True)
class FullBackup:
    """Downloadable backup content and the row counts it contains."""

    filename: str
    content: bytes
    table_counts: dict[str, int]

    @property
    def total_rows(self) -> int:
        return sum(self.table_counts.values())


@dataclass(frozen=True)
class RestoreSummary:
    """Summary of rows restored from a validated backup."""

    table_counts: dict[str, int]

    @property
    def total_rows(self) -> int:
        return sum(self.table_counts.values())


def full_backup_filename(now: datetime | None = None) -> str:
    """Return a timestamped backup filename safe for browser downloads."""

    current_dt = (now or datetime.now(UTC)).astimezone(UTC)
    timestamp = current_dt.strftime("%Y%m%d-%H%M%SZ")
    return f"mileage-logger-full-backup-{timestamp}.json.gz"


def create_full_backup(db: Session, *, now: datetime | None = None) -> FullBackup:
    """Create a gzip-compressed JSON snapshot of every application data table."""

    current_dt = (now or datetime.now(UTC)).astimezone(UTC)
    table_payloads: dict[str, list[dict[str, Any]]] = {}
    table_counts: dict[str, int] = {}

    for table in BACKUP_TABLES:
        rows = [_serialize_row(table, row) for row in db.execute(_select_table(table)).mappings()]
        table_payloads[table.name] = rows
        table_counts[table.name] = len(rows)

    sites = list(db.scalars(select(Site).order_by(Site.name.asc(), Site.id.asc())))
    payload = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "created_at": current_dt.isoformat(),
        "tables": table_payloads,
        "table_counts": table_counts,
        "schema": {
            table.name: [column.name for column in table.columns] for table in BACKUP_TABLES
        },
        "owntracks_waypoints": owntracks_waypoints_export(sites),
    }
    raw_content = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return FullBackup(
        filename=full_backup_filename(current_dt),
        content=gzip.compress(raw_content, mtime=0),
        table_counts=table_counts,
    )


def restore_full_backup(db: Session, content: bytes) -> RestoreSummary:
    """Replace all application table rows with data from a validated backup."""

    rows_by_table = _validated_table_rows(_load_backup_payload(content))
    table_counts = {table.name: len(rows_by_table[table.name]) for table in BACKUP_TABLES}

    with _BACKUP_RESTORE_LOCK:
        try:
            _lock_postgresql_tables(db)
            for table in reversed(BACKUP_TABLES):
                db.execute(table.delete().execution_options(synchronize_session=False))
            for table in BACKUP_TABLES:
                rows = rows_by_table[table.name]
                if rows:
                    db.execute(insert(table), rows)
            _reset_postgresql_sequences(db)
            db.commit()
        except Exception:
            db.rollback()
            raise

    logger.warning(
        "Restored full database backup tables=%s total_rows=%s",
        len(table_counts),
        sum(table_counts.values()),
    )
    return RestoreSummary(table_counts=table_counts)


def _select_table(table: Table):
    """Build a deterministic select statement for one table."""

    statement = select(table)
    primary_key_columns = list(table.primary_key.columns)
    if primary_key_columns:
        statement = statement.order_by(*(column.asc() for column in primary_key_columns))
    return statement


def _serialize_row(table: Table, row: dict[str, Any]) -> dict[str, Any]:
    return {
        column.name: _serialize_value(column, row[column.name])
        for column in table.columns
    }


def _serialize_value(column: Column, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        if not isinstance(value, datetime):
            raise BackupValidationError(
                f"Column {column.table.name}.{column.name} expected datetime"
            )
        return value.isoformat()
    if isinstance(column.type, Date):
        if not isinstance(value, date):
            raise BackupValidationError(f"Column {column.table.name}.{column.name} expected date")
        return value.isoformat()
    if isinstance(column.type, Numeric):
        return str(value)
    return value


def _load_backup_payload(content: bytes) -> dict[str, Any]:
    if not content:
        raise BackupValidationError("Backup file is empty.")
    try:
        raw_content = gzip.decompress(content)
    except OSError:
        raw_content = content

    try:
        payload = json.loads(raw_content.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise BackupValidationError("Backup file is not valid UTF-8 JSON.") from exc
    except json.JSONDecodeError as exc:
        raise BackupValidationError("Backup file is not valid JSON.") from exc

    if not isinstance(payload, dict):
        raise BackupValidationError("Backup root must be a JSON object.")
    if payload.get("format") != BACKUP_FORMAT:
        raise BackupValidationError("Backup format is not supported.")
    if payload.get("version") != BACKUP_VERSION:
        raise BackupValidationError("Backup version is not supported.")
    return payload


def _validated_table_rows(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    tables_payload = payload.get("tables")
    if not isinstance(tables_payload, dict):
        raise BackupValidationError("Backup does not contain a tables object.")

    missing_tables = set(BACKUP_TABLE_NAMES) - set(tables_payload)
    if missing_tables:
        missing = ", ".join(sorted(missing_tables))
        raise BackupValidationError(f"Backup is missing required tables: {missing}.")

    rows_by_table: dict[str, list[dict[str, Any]]] = {}
    for table in BACKUP_TABLES:
        raw_rows = tables_payload.get(table.name)
        if not isinstance(raw_rows, list):
            raise BackupValidationError(f"Backup table {table.name} must be a list.")
        rows_by_table[table.name] = [
            _validated_row(table, row, index) for index, row in enumerate(raw_rows, start=1)
        ]
    return rows_by_table


def _validated_row(table: Table, row: Any, index: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise BackupValidationError(f"Backup table {table.name} row {index} must be an object.")

    expected_columns = {column.name for column in table.columns}
    actual_columns = set(row)
    unexpected_columns = actual_columns - expected_columns
    if unexpected_columns:
        columns = ", ".join(sorted(unexpected_columns))
        raise BackupValidationError(
            f"Backup table {table.name} row {index} has unexpected columns: {columns}."
        )
    missing_columns = expected_columns - actual_columns
    if missing_columns:
        columns = ", ".join(sorted(missing_columns))
        raise BackupValidationError(
            f"Backup table {table.name} row {index} is missing columns: {columns}."
        )

    return {
        column.name: _deserialize_value(column, row[column.name])
        for column in table.columns
    }


def _deserialize_value(column: Column, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        if not isinstance(value, str):
            raise BackupValidationError(
                f"Column {column.table.name}.{column.name} must be an ISO datetime string."
            )
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BackupValidationError(
                f"Column {column.table.name}.{column.name} has an invalid datetime."
            ) from exc
    if isinstance(column.type, Date):
        if not isinstance(value, str):
            raise BackupValidationError(
                f"Column {column.table.name}.{column.name} must be an ISO date string."
            )
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise BackupValidationError(
                f"Column {column.table.name}.{column.name} has an invalid date."
            ) from exc
    if isinstance(column.type, Numeric):
        try:
            return Decimal(str(value))
        except Exception as exc:
            raise BackupValidationError(
                f"Column {column.table.name}.{column.name} has an invalid decimal value."
            ) from exc
    return value


def _lock_postgresql_tables(db: Session) -> None:
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return

    preparer = bind.dialect.identifier_preparer
    table_names = ", ".join(_quoted_table_name(table, preparer) for table in BACKUP_TABLES)
    db.execute(text(f"LOCK TABLE {table_names} IN ACCESS EXCLUSIVE MODE"))


def _quoted_table_name(table: Table, preparer) -> str:
    if table.schema:
        return f"{preparer.quote_schema(table.schema)}.{preparer.quote(table.name)}"
    return preparer.quote(table.name)


def _reset_postgresql_sequences(db: Session) -> None:
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table in BACKUP_TABLES:
        integer_primary_keys = [
            column
            for column in table.primary_key.columns
            if column.autoincrement and column.type.python_type is int
        ]
        if len(integer_primary_keys) != 1:
            continue

        column = integer_primary_keys[0]
        sequence_name = db.scalar(
            text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
            {
                "table_name": table.name,
                "column_name": column.name,
            },
        )
        if not sequence_name:
            continue

        max_value = db.scalar(select(column).order_by(column.desc()).limit(1))
        if max_value is None:
            db.execute(
                text("SELECT setval(CAST(:sequence_name AS regclass), 1, false)"),
                {"sequence_name": sequence_name},
            )
        else:
            db.execute(
                text("SELECT setval(CAST(:sequence_name AS regclass), :value, true)"),
                {"sequence_name": sequence_name, "value": int(max_value)},
            )
