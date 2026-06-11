from decimal import Decimal
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Mileage Logger"
    app_env: str = "local"
    secret_key: str = "change-me"
    database_url: str = "postgresql+psycopg://mileage:mileage@localhost:5432/mileage_logger"
    create_tables_on_startup: bool = False

    owntracks_api_token: str = ""
    owntracks_username: str = ""
    owntracks_password: str = ""
    owntracks_auto_create_sites: bool = True
    owntracks_default_site_radius_m: int = 150
    owntracks_stop_minutes: int = 10
    owntracks_unknown_stop_radius_m: int = 150
    google_places_api_key: str = ""
    google_places_radius_m: int = 100
    google_places_auto_create_sites: bool = True

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

    report_output_dir: str = "reports"
    log_dir: str = "logs"
    min_trip_miles: Decimal = Field(default=Decimal("0.10"), ge=Decimal("0"))


@lru_cache
def get_settings() -> Settings:
    return Settings()
