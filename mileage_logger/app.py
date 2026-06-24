import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from mileage_logger.api.routes import router as api_router
from mileage_logger.config import get_settings
from mileage_logger.database import engine
from mileage_logger.logging_config import configure_logging
from mileage_logger.models import Base
from mileage_logger.services.backups import automatic_backup_scheduler
from mileage_logger.services.gas_prices import gas_snapshot_scheduler
from mileage_logger.services.mqtt import MqttOwnTracksWorker
from mileage_logger.services.trip_processor import AutomaticTripProcessor
from mileage_logger.web.auth import enforce_web_login
from mileage_logger.web.routes import router as web_router

settings = get_settings()
mqtt_worker = MqttOwnTracksWorker()
trip_processor = AutomaticTripProcessor()
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    log_path = configure_logging("app")
    logger.info("Starting Mileage Logger app, log_path=%s", log_path)
    if settings.create_tables_on_startup:
        Base.metadata.create_all(bind=engine)
    if settings.automatic_backups_enabled:
        _app.state.automatic_backup_task = asyncio.create_task(
            automatic_backup_scheduler(settings)
        )
    if settings.gas_snapshot_enabled:
        _app.state.gas_snapshot_task = asyncio.create_task(gas_snapshot_scheduler(settings))
    trip_processor.start()
    mqtt_worker.start()
    try:
        yield
    finally:
        logger.info("Stopping Mileage Logger app")
        gas_snapshot_task = getattr(_app.state, "gas_snapshot_task", None)
        if gas_snapshot_task is not None:
            gas_snapshot_task.cancel()
            with suppress(asyncio.CancelledError):
                await gas_snapshot_task
        backup_task = getattr(_app.state, "automatic_backup_task", None)
        if backup_task is not None:
            backup_task.cancel()
            with suppress(asyncio.CancelledError):
                await backup_task
        trip_processor.stop()
        mqtt_worker.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.middleware("http")
async def require_web_login(request: Request, call_next):
    return await enforce_web_login(request, call_next)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "request method=%s path=%s status=%s duration_ms=%.1f",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="mileage_logger_session",
    same_site="lax",
    https_only=settings.web_session_cookie_secure,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(api_router, prefix="/api", tags=["api"])
app.include_router(web_router, tags=["web"])
