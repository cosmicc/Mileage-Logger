"""Structured audit logging for web UI login attempts."""

import ipaddress
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
from mileage_logger.web.auth import login_client_key, login_direct_client_is_trusted_proxy

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


@dataclass(frozen=True)
class LoginSuccessEntry:
    """One structured successful web-login audit entry shown on Diagnostics."""

    entry_id: str
    occurred_at_local: str
    occurred_at_utc: str
    client_ip: str
    username: str
    username_length: int
    username_truncated: bool
    account: str
    authentication_method: str
    user_agent: str
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

    @property
    def authentication_method_label(self) -> str:
        if self.authentication_method == "passkey":
            return "Passkey"
        return "Password"

    @property
    def authentication_method_pill_class(self) -> str:
        if self.authentication_method == "passkey":
            return "good"
        return "muted"


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


def _build_common_login_payload(
    *,
    request: Request,
    username: str,
    next_url: str,
    settings: Settings,
) -> dict[str, Any]:
    """Return bounded request metadata shared by successful and failed login audit records."""

    occurred_at_utc = datetime.now(UTC)
    cleaned_username = _bounded_text(username, max_length=MAX_USERNAME_LOG_LENGTH)
    return {
        "occurred_at_utc": _utc_timestamp(occurred_at_utc),
        "occurred_at_local": datetime_to_local(occurred_at_utc).isoformat(timespec="seconds"),
        "client_ip": _bounded_text(login_client_key(request, settings)),
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
        "username": cleaned_username,
        "username_length": len(username),
        "username_truncated": len(str(username or "")) > MAX_USERNAME_LOG_LENGTH,
    }


def _payload_int(payload: dict[str, Any], key: str) -> int:
    try:
        return int(payload.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _login_success_authentication_method(payload: dict[str, Any]) -> str:
    """Return the successful-login method, inferring old log entries from their request path."""

    method = str(payload.get("authentication_method") or "").strip().casefold()
    if method in {"passkey", "webauthn", "device"}:
        return "passkey"
    if method in {"password", "credentials"}:
        return "password"

    path = str(payload.get("path") or "").strip().casefold()
    if path.startswith("/passkeys/"):
        return "passkey"
    return "password"


def _payload_ip(value: object) -> str:
    """Return a normalized stored IP address or an empty string for invalid values."""

    try:
        return str(ipaddress.ip_address(str(value or "").strip()))
    except ValueError:
        return ""


def _forwarded_for_first_ip(value: object) -> str:
    """Return the first usable IP from an X-Forwarded-For audit value."""

    first_value = str(value or "").split(",", maxsplit=1)[0]
    return _payload_ip(first_value)


def _ip_is_loopback(value: str) -> bool:
    """Return whether an IP is loopback and should not be a Cloudflare block target."""

    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _client_ip_from_payload(payload: dict[str, Any], settings: Settings | None) -> str:
    """Resolve the effective failed-login client IP from a stored audit payload."""

    stored_client_ip = _bounded_text(payload.get("client_ip", "unknown"))
    if settings is None:
        return stored_client_ip

    direct_client_ip = _bounded_text(payload.get("direct_client_ip", ""))
    if not login_direct_client_is_trusted_proxy(direct_client_ip, settings):
        return stored_client_ip
    stored_ip = _payload_ip(stored_client_ip)
    direct_ip = _payload_ip(direct_client_ip)

    for candidate in (
        _forwarded_for_first_ip(payload.get("x_forwarded_for", "")),
        _payload_ip(payload.get("x_real_ip", "")),
        _payload_ip(payload.get("cf_connecting_ip", "")),
    ):
        if candidate and candidate != direct_ip and not _ip_is_loopback(candidate):
            return candidate

    if stored_ip and stored_ip != direct_ip:
        return stored_client_ip
    return stored_client_ip


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
    settings: Settings,
) -> dict[str, Any]:
    return {
        "event": "web_login_failed",
        **_build_common_login_payload(
            request=request,
            username=username,
            next_url=next_url,
            settings=settings,
        ),
        "reason": _bounded_text(reason, max_length=64),
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
        settings=active_settings,
    )
    audit_logger.info(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def _build_login_success_payload(
    *,
    request: Request,
    username: str,
    account: str,
    authentication_method: str,
    next_url: str,
    settings: Settings,
) -> dict[str, Any]:
    """Return a structured successful-login audit payload without storing the password."""

    return {
        "event": "web_login_succeeded",
        **_build_common_login_payload(
            request=request,
            username=username,
            next_url=next_url,
            settings=settings,
        ),
        "account": _bounded_text(account, max_length=MAX_USERNAME_LOG_LENGTH),
        "authentication_method": _bounded_text(authentication_method, max_length=32),
    }


def record_web_login_success(
    *,
    request: Request,
    username: str,
    account: str,
    authentication_method: str = "password",
    next_url: str,
    settings: Settings | None = None,
) -> None:
    """Append a structured successful-login audit record without storing the password value."""

    active_settings = settings or get_settings()
    log_path = configure_login_failure_logging(active_settings)
    if log_path is None:
        logger.error(
            "Successful web-login audit entry was not written; login audit log path is unavailable"
        )
        return

    payload = _build_login_success_payload(
        request=request,
        username=username,
        account=account,
        authentication_method=authentication_method,
        next_url=next_url,
        settings=active_settings,
    )
    audit_logger.info(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def _entry_id_from_line(line: str) -> str:
    """Return a stable identifier for one raw login-failure log line."""

    return sha256(line.encode("utf-8", errors="replace")).hexdigest()


def _entry_from_payload(
    payload: dict[str, Any],
    *,
    entry_id: str,
    settings: Settings | None = None,
) -> LoginFailureEntry:
    return LoginFailureEntry(
        entry_id=entry_id,
        occurred_at_local=_bounded_text(payload.get("occurred_at_local", "")),
        occurred_at_utc=_bounded_text(payload.get("occurred_at_utc", "")),
        client_ip=_client_ip_from_payload(payload, settings),
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


def _success_entry_from_payload(
    payload: dict[str, Any],
    *,
    entry_id: str,
    settings: Settings | None = None,
) -> LoginSuccessEntry:
    return LoginSuccessEntry(
        entry_id=entry_id,
        occurred_at_local=_bounded_text(payload.get("occurred_at_local", "")),
        occurred_at_utc=_bounded_text(payload.get("occurred_at_utc", "")),
        client_ip=_client_ip_from_payload(payload, settings),
        username=redact_sensitive_text(
            _bounded_text(payload.get("username", ""), max_length=MAX_USERNAME_LOG_LENGTH)
        ),
        username_length=_payload_int(payload, "username_length"),
        username_truncated=bool(payload.get("username_truncated")),
        account=redact_sensitive_text(
            _bounded_text(payload.get("account", ""), max_length=MAX_USERNAME_LOG_LENGTH)
        ),
        authentication_method=_login_success_authentication_method(payload),
        user_agent=redact_sensitive_text(_bounded_text(payload.get("user_agent", ""))),
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
    settings: Settings | None = None,
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
        entries.append(_entry_from_payload(payload, entry_id=entry_id, settings=settings))
    return entries


def tail_login_success_entries(
    path: Path,
    max_entries: int = 50,
    settings: Settings | None = None,
) -> list[LoginSuccessEntry]:
    """Read recent structured successful-login audit records newest-first."""

    if not path.exists():
        return []
    with path.open("rb") as file:
        file.seek(0, 2)
        size = file.tell()
        file.seek(max(size - LOGIN_FAILURE_TAIL_BYTES, 0))
        text = file.read().decode("utf-8", errors="replace")

    entries: list[LoginSuccessEntry] = []
    for line in reversed(text.splitlines()):
        if len(entries) >= max_entries:
            break
        entry_id = _entry_id_from_line(line)
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("event") != "web_login_succeeded":
            continue
        entries.append(_success_entry_from_payload(payload, entry_id=entry_id, settings=settings))
    return entries
