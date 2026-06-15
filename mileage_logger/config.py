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

    owntracks_api_token: str = ""
    owntracks_username: str = ""
    owntracks_password: str = ""
    owntracks_sync_waypoints: bool = True
    owntracks_default_site_radius_m: int = 150
    automatic_trip_processing_enabled: bool = True
    automatic_trip_processing_interval_seconds: int = Field(default=60, ge=5)
    owntracks_purge_enabled: bool = True
    owntracks_location_retention_days: int = Field(default=14, ge=1)
    owntracks_driving_speed_mph: Decimal = Field(default=Decimal("10.0"), ge=Decimal("0"))
    owntracks_driving_window_minutes: int = Field(default=10, ge=1)

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

    smartcar_enabled: bool = False
    smartcar_management_token: str = ""
    smartcar_api_polling_enabled: bool = False
    smartcar_access_token: str = ""
    smartcar_client_id: str = ""
    smartcar_client_secret: str = ""
    smartcar_token_url: str = "https://iam.smartcar.com/oauth2/token"
    smartcar_scope: str = "read_odometer"
    smartcar_vehicle_id: str = ""
    smartcar_api_base_url: str = "https://api.smartcar.com/v2.0"
    smartcar_odometer_unit: str = "km"
    smartcar_timeout_seconds: float = Field(default=20.0, gt=0)
    smartcar_retry_attempts: int = Field(default=3, ge=1)
    smartcar_retry_delay_seconds: float = Field(default=2.0, ge=0)
    smartcar_auth_failure_cooldown_seconds: int = Field(default=3600, ge=0)
    smartcar_webhook_max_body_bytes: int = Field(default=262144, ge=1024)

    log_dir: str = "logs"
    log_level: LogLevel = "info"
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
