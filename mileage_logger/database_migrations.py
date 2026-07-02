import logging
from pathlib import Path
from threading import Lock

from alembic.config import Config

from alembic import command

logger = logging.getLogger(__name__)
_MIGRATION_LOCK = Lock()
_migration_checked = False


def run_migrations_once_on_reconnect() -> None:
    """Run Alembic migrations once after a successful database reconnect."""

    global _migration_checked
    if _migration_checked:
        return

    with _MIGRATION_LOCK:
        if _migration_checked:
            return
        alembic_ini = Path("alembic.ini")
        if not alembic_ini.exists():
            logger.warning("Skipping reconnect migrations; alembic.ini was not found")
            _migration_checked = True
            return
        command.upgrade(Config(str(alembic_ini)), "head")
        _migration_checked = True
        logger.info("Verified database migrations after reconnect")
