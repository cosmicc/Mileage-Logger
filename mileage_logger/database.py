from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from mileage_logger.config import get_settings
from mileage_logger.database_engine import database_engine_options

settings = get_settings()

engine = create_engine(settings.database_url, **database_engine_options(settings))
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def is_database_unavailable_error(exc: BaseException) -> bool:
    """Return whether an exception represents an unavailable database connection."""

    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, BaseExceptionGroup):
            return any(is_database_unavailable_error(child) for child in current.exceptions)
        if isinstance(current, (OperationalError, SQLAlchemyTimeoutError)):
            return True
        if isinstance(current, DBAPIError) and current.connection_invalidated:
            return True
        current = current.__cause__ if isinstance(current.__cause__, BaseException) else None
    return False


def database_is_reachable() -> bool:
    """Check whether the configured database accepts a simple query."""

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        if is_database_unavailable_error(exc):
            return False
        raise
    return True


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
