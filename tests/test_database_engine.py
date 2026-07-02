from mileage_logger.config import Settings
from mileage_logger.database_engine import database_engine_options


def test_postgresql_engine_options_are_configurable_for_network_database() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://mileage:secret@db-server:5432/mileage_logger",
        database_pool_size=3,
        database_max_overflow=4,
        database_pool_timeout_seconds=12,
        database_pool_recycle_seconds=900,
        database_connect_timeout_seconds=7,
    )

    options = database_engine_options(settings)

    assert options["pool_pre_ping"] is True
    assert options["pool_size"] == 3
    assert options["max_overflow"] == 4
    assert options["pool_timeout"] == 12
    assert options["pool_recycle"] == 900
    assert options["pool_use_lifo"] is True
    assert options["connect_args"] == {"connect_timeout": 7}


def test_sqlite_engine_options_skip_postgresql_pool_arguments() -> None:
    settings = Settings(
        database_url="sqlite://",
        database_pool_size=3,
        database_max_overflow=4,
        database_pool_timeout_seconds=12,
        database_pool_recycle_seconds=900,
        database_connect_timeout_seconds=7,
    )

    assert database_engine_options(settings) == {"pool_pre_ping": True}
