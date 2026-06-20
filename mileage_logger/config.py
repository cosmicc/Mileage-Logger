from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["debug", "info", "warning"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Mileage Logger"
    app_env: str = "local"
    secret_key: str = "change-me"
    local_timezone: str = "America/Detroit"
    database_url: str = "postgresql+psycopg://mileage:mileage@localhost:5432/mileage_logger"
    create_tables_on_startup: bool = False
    web_login_username: str = ""
    web_login_password: str = ""
    web_session_cookie_secure: bool = False
    web_login_max_attempts: int = Field(default=5, ge=1)
    web_login_lockout_seconds: int = Field(default=300, ge=1)

    owntracks_api_token: str = ""
    owntracks_username: str = ""
    owntracks_password: str = ""
    owntracks_sync_waypoints: bool = True
    owntracks_default_site_radius_m: int = 150
    automatic_trip_processing_enabled: bool = True
    automatic_trip_processing_interval_seconds: int = Field(default=60, ge=5)
    owntracks_purge_enabled: bool = True
    owntracks_location_retention_days: int = Field(default=14, ge=1)
    owntracks_waypoint_dwell_minutes: int = Field(default=5, ge=1)
    owntracks_travel_distance_m: Decimal = Field(default=Decimal("50.0"), ge=Decimal("0"))

    mqtt_enabled: bool = False
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_topic: str = "owntracks/#"

    gas_price_state: str = "MI"
    gas_price_buffer: Decimal = Decimal("0.50")
    gas_price_source: str = "aaa_current"
    eia_api_key: str = ""
    eia_series_id: str = ""
    vehicle_mpg: Decimal = Field(default=Decimal("25.0"), gt=Decimal("0"))

    log_dir: str = "logs"
    log_level: LogLevel = "info"
    login_failure_log_path: str = "/var/log/mileage-logger-login-failures.log"
    min_trip_miles: Decimal = Field(default=Decimal("0.10"), ge=Decimal("0"))

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> str:
        normalized = str(value or "info").strip().casefold()
        if normalized not in {"debug", "info", "warning"}:
            raise ValueError("LOG_LEVEL must be debug, info, or warning")
        return normalized


@lru_cache
def get_settings() -> Settings:
    return Settings()
