import logging
import sys

import pytest
from pydantic import ValidationError

from mileage_logger.config import Settings
from mileage_logger.logging_config import LocalTimezoneFormatter, log_level_value
from mileage_logger.web.routes import _log_line_is_visible, _tail_file


def test_log_level_normalizes_supported_values() -> None:
    settings = Settings(log_level="WARNING")

    assert settings.log_level == "warning"
    assert log_level_value(settings) == logging.WARNING


def test_log_level_rejects_error_as_configured_level() -> None:
    with pytest.raises(ValidationError):
        Settings(log_level="error")


def test_web_login_requires_complete_credentials() -> None:
    with pytest.raises(ValidationError, match="WEB_LOGIN_USERNAME and WEB_LOGIN_PASSWORD"):
        Settings(web_login_username="admin")

    with pytest.raises(ValidationError, match="WEB_LOGIN_USERNAME and WEB_LOGIN_PASSWORD"):
        Settings(web_login_password="secret-password")


def test_web_login_rejects_default_secret_key() -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(
            web_login_username="admin",
            web_login_password="secret-password",
        )


def test_production_requires_web_login_and_changed_secret_key() -> None:
    with pytest.raises(ValidationError, match="WEB_LOGIN_USERNAME"):
        Settings(
            app_env="production",
            secret_key="production-test-secret",
        )

    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(
            app_env="production",
            secret_key="change-me",
            web_login_username="admin",
            web_login_password="secret-password",
        )

    settings = Settings(
        app_env="production",
        secret_key="production-test-secret",
        web_login_username="admin",
        web_login_password="secret-password",
        web_api_key="web-api-secret",
        owntracks_username="owntracks",
        owntracks_password="owntracks-password",
        owntracks_encryption_key="owntracks-secret",
    )

    assert settings.app_env == "production"


def test_log_line_visibility_uses_threshold_but_keeps_errors() -> None:
    assert _log_line_is_visible("2026-06-13 09:00:00 EDT INFO [app] started", logging.INFO)
    assert not _log_line_is_visible(
        "2026-06-13 09:00:00 EDT INFO [app] started",
        logging.WARNING,
    )
    assert _log_line_is_visible(
        "2026-06-13 09:00:00 EDT ERROR [app] failed",
        logging.WARNING,
    )


def test_formatter_adds_level_to_exception_traceback_lines() -> None:
    formatter = LocalTimezoneFormatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %Z",
    )
    try:
        raise RuntimeError("sample failure")
    except RuntimeError:
        record = logging.LogRecord(
            "mileage_logger.test",
            logging.ERROR,
            __file__,
            1,
            "failed",
            (),
            sys.exc_info(),
        )

    lines = formatter.format(record).splitlines()

    assert len(lines) > 1
    assert all(" ERROR [mileage_logger.test] " in line for line in lines)


def test_formatter_redacts_sensitive_query_values() -> None:
    formatter = LocalTimezoneFormatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %Z",
    )
    record = logging.LogRecord(
        "mileage_logger.test",
        logging.INFO,
        __file__,
        1,
        "GET https://api.example.test/path?api_key=secret-value&series=test",
        (),
        None,
    )

    formatted = formatter.format(record)

    assert "api_key=***" in formatted
    assert "secret-value" not in formatted


def test_formatter_redacts_bearer_tokens() -> None:
    formatter = LocalTimezoneFormatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %Z",
    )
    record = logging.LogRecord(
        "mileage_logger.test",
        logging.INFO,
        __file__,
        1,
        "Authorization: Bearer secret-provider-token",
        (),
        None,
    )

    formatted = formatter.format(record)

    assert "Authorization: Bearer ***" in formatted
    assert "secret-provider-token" not in formatted


def test_tail_file_returns_level_classes_newest_first(tmp_path) -> None:
    log_path = tmp_path / "app.log"
    log_path.write_text(
        "\n".join(
            [
                "2026-06-13 09:00:00 EDT DEBUG [app] details",
                "2026-06-13 09:01:00 EDT INFO [app] started",
                "2026-06-13 09:02:00 EDT WARNING [app] slow",
                "2026-06-13 09:03:00 EDT ERROR [app] failed",
            ]
        ),
        encoding="utf-8",
    )

    entries = _tail_file(log_path, max_lines=10, log_level="debug")

    assert [entry.level for entry in entries] == ["error", "warning", "info", "debug"]
    assert [entry.css_class for entry in entries] == [
        "log-line-error",
        "log-line-warning",
        "log-line-info",
        "log-line-debug",
    ]


def test_tail_file_redacts_sensitive_query_values(tmp_path) -> None:
    log_path = tmp_path / "app.log"
    log_path.write_text(
        "2026-06-13 09:00:00 EDT INFO [httpx] GET "
        "https://api.example.test/path?api_key=secret-value&series=test",
        encoding="utf-8",
    )

    entries = _tail_file(log_path, max_lines=10, log_level="debug")

    assert entries[0].text.endswith("api_key=***&series=test")
    assert "secret-value" not in entries[0].text
