"""Structured audit logging for failed web UI login attempts."""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from fastapi import Request

from mileage_logger.config import Settings, get_settings
from mileage_logger.logging_config import (
    LOGIN_FAILURE_LOGGER,
    configure_login_failure_logging,
    redact_sensitive_text,
)
from mileage_logger.services.timezone import datetime_to_local
from mileage_logger.web.auth import login_client_key

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger(LOGIN_FAILURE_LOGGER)
audit_logger.propagate = False

MAX_TEXT_FIELD_LENGTH = 512
MAX_USERNAME_LOG_LENGTH = 256
LOGIN_FAILURE_TAIL_BYTES = 160_000


@dataclass(frozen=True)
class LoginFailureEntry:
    """One structured failed web-login audit entry shown on Diagnostics."""

    entry_id: str
    occurred_at_local: str
    occurred_at_utc: str
    client_ip: str
    username: str
    username_length: int
    username_truncated: bool
    password_length: int
    user_agent: str
    reason: str
    failed_count: int
    max_attempts: int
    lockout_applied: bool
    lockout_remaining_seconds: int
    method: str
    path: str
    next_url: str
    host: str
    direct_client_ip: str
    cf_connecting_ip: str
    x_real_ip: str
    x_forwarded_for: str
    forwarded_proto: str
    raw_payload: dict[str, Any]


def _bounded_text(value: object, *, max_length: int = MAX_TEXT_FIELD_LENGTH) -> str:
    """Return single-line audit text capped to keep hostile inputs from bloating logs."""

    text = str(value or "").replace("\x00", "")
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    return text[:max_length]


def _request_header(request: Request, name: str) -> str:
    return _bounded_text(request.headers.get(name, ""))


def _direct_client_ip(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return _bounded_text(request.client.host)


def _utc_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _payload_int(payload: dict[str, Any], key: str) -> int:
    try:
        return int(payload.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _build_login_failure_payload(
    *,
    request: Request,
    username: str,
    password_length: int,
    reason: str,
    failed_count: int,
    max_attempts: int,
    lockout_applied: bool,
    lockout_remaining_seconds: int,
    next_url: str,
) -> dict[str, Any]:
    occurred_at_utc = datetime.now(UTC)
    cleaned_username = _bounded_text(username, max_length=MAX_USERNAME_LOG_LENGTH)
    return {
        "event": "web_login_failed",
        "occurred_at_utc": _utc_timestamp(occurred_at_utc),
        "occurred_at_local": datetime_to_local(occurred_at_utc).isoformat(timespec="seconds"),
        "client_ip": _bounded_text(login_client_key(request)),
        "direct_client_ip": _direct_client_ip(request),
        "cf_connecting_ip": _request_header(request, "cf-connecting-ip"),
        "x_real_ip": _request_header(request, "x-real-ip"),
        "x_forwarded_for": _request_header(request, "x-forwarded-for"),
        "forwarded_proto": _request_header(request, "x-forwarded-proto"),
        "host": _request_header(request, "host"),
        "user_agent": _request_header(request, "user-agent"),
        "method": _bounded_text(request.method, max_length=24),
        "path": _bounded_text(request.url.path),
        "next_url": _bounded_text(next_url),
        "reason": _bounded_text(reason, max_length=64),
        "username": cleaned_username,
        "username_length": len(username),
        "username_truncated": len(str(username or "")) > MAX_USERNAME_LOG_LENGTH,
        "password_length": password_length,
        "failed_count": failed_count,
        "max_attempts": max_attempts,
        "lockout_applied": lockout_applied,
        "lockout_remaining_seconds": lockout_remaining_seconds,
    }


def record_web_login_failure(
    *,
    request: Request,
    username: str,
    password: str,
    reason: str,
    failed_count: int,
    max_attempts: int,
    lockout_applied: bool,
    lockout_remaining_seconds: int,
    next_url: str,
    settings: Settings | None = None,
) -> None:
    """Append a structured failed-login audit record without storing the password value."""

    active_settings = settings or get_settings()
    log_path = configure_login_failure_logging(active_settings)
    if log_path is None:
        logger.error(
            "Failed web-login audit entry was not written; login_failure_log_path is unavailable"
        )
        return

    payload = _build_login_failure_payload(
        request=request,
        username=username,
        password_length=len(password),
        reason=reason,
        failed_count=failed_count,
        max_attempts=max_attempts,
        lockout_applied=lockout_applied,
        lockout_remaining_seconds=lockout_remaining_seconds,
        next_url=next_url,
    )
    audit_logger.info(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def _entry_id_from_line(line: str) -> str:
    """Return a stable identifier for one raw login-failure log line."""

    return sha256(line.encode("utf-8", errors="replace")).hexdigest()


def _entry_from_payload(payload: dict[str, Any], *, entry_id: str) -> LoginFailureEntry:
    return LoginFailureEntry(
        entry_id=entry_id,
        occurred_at_local=_bounded_text(payload.get("occurred_at_local", "")),
        occurred_at_utc=_bounded_text(payload.get("occurred_at_utc", "")),
        client_ip=_bounded_text(payload.get("client_ip", "unknown")),
        username=redact_sensitive_text(
            _bounded_text(payload.get("username", ""), max_length=MAX_USERNAME_LOG_LENGTH)
        ),
        username_length=_payload_int(payload, "username_length"),
        username_truncated=bool(payload.get("username_truncated")),
        password_length=_payload_int(payload, "password_length"),
        user_agent=redact_sensitive_text(_bounded_text(payload.get("user_agent", ""))),
        reason=_bounded_text(payload.get("reason", "")),
        failed_count=_payload_int(payload, "failed_count"),
        max_attempts=_payload_int(payload, "max_attempts"),
        lockout_applied=bool(payload.get("lockout_applied")),
        lockout_remaining_seconds=_payload_int(payload, "lockout_remaining_seconds"),
        method=_bounded_text(payload.get("method", "")),
        path=_bounded_text(payload.get("path", "")),
        next_url=redact_sensitive_text(_bounded_text(payload.get("next_url", ""))),
        host=redact_sensitive_text(_bounded_text(payload.get("host", ""))),
        direct_client_ip=_bounded_text(payload.get("direct_client_ip", "")),
        cf_connecting_ip=_bounded_text(payload.get("cf_connecting_ip", "")),
        x_real_ip=_bounded_text(payload.get("x_real_ip", "")),
        x_forwarded_for=_bounded_text(payload.get("x_forwarded_for", "")),
        forwarded_proto=_bounded_text(payload.get("forwarded_proto", "")),
        raw_payload=payload,
    )


def tail_login_failure_entries(
    path: Path,
    max_entries: int = 50,
    hidden_entry_ids: set[str] | None = None,
) -> list[LoginFailureEntry]:
    """Read recent structured login-failure audit records newest-first."""

    if not path.exists():
        return []
    with path.open("rb") as file:
        file.seek(0, 2)
        size = file.tell()
        file.seek(max(size - LOGIN_FAILURE_TAIL_BYTES, 0))
        text = file.read().decode("utf-8", errors="replace")

    entries: list[LoginFailureEntry] = []
    hidden_ids = hidden_entry_ids or set()
    for line in reversed(text.splitlines()):
        if len(entries) >= max_entries:
            break
        entry_id = _entry_id_from_line(line)
        if entry_id in hidden_ids:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("event") != "web_login_failed":
            continue
        entries.append(_entry_from_payload(payload, entry_id=entry_id))
    return entries
