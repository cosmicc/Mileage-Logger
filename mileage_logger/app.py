import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from mileage_logger.api.deps import WEB_API_AUTH_EXEMPT_PATHS, verify_web_api_auth
from mileage_logger.api.routes import router as api_router
from mileage_logger.config import get_settings
from mileage_logger.database import engine, is_database_unavailable_error
from mileage_logger.logging_config import configure_logging
from mileage_logger.models import Base
from mileage_logger.services.backups import automatic_backup_scheduler
from mileage_logger.services.gas_prices import gas_snapshot_scheduler
from mileage_logger.services.mqtt import MqttOwnTracksWorker
from mileage_logger.services.owntracks_buffer import (
    OwnTracksBufferReplayer,
)
from mileage_logger.services.runtime_status import build_runtime_status
from mileage_logger.services.trip_processor import AutomaticTripProcessor
from mileage_logger.web.auth import enforce_web_login
from mileage_logger.web.routes import router as web_router
from mileage_logger.web.routes import templates

settings = get_settings()
mqtt_worker = MqttOwnTracksWorker()
trip_processor = AutomaticTripProcessor()
owntracks_buffer_replayer = OwnTracksBufferReplayer()
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    log_path = configure_logging("app")
    logger.info("Starting Mileage Logger app, log_path=%s", log_path)
    if settings.create_tables_on_startup:
        try:
            Base.metadata.create_all(bind=engine)
        except Exception as exc:
            if not settings.owntracks_buffer_enabled or not is_database_unavailable_error(exc):
                raise
            logger.warning(
                "Database unavailable during create_tables_on_startup; starting in limp mode"
            )
    if settings.automatic_backups_enabled:
        _app.state.automatic_backup_task = asyncio.create_task(
            automatic_backup_scheduler(settings)
        )
    if settings.gas_snapshot_enabled:
        _app.state.gas_snapshot_task = asyncio.create_task(gas_snapshot_scheduler(settings))
    owntracks_buffer_replayer.start()
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
        owntracks_buffer_replayer.stop()
        mqtt_worker.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.middleware("http")
async def database_limp_mode(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        if not is_database_unavailable_error(exc):
            raise
        logger.warning(
            "Database unavailable; returning limp-mode response path=%s",
            request.url.path,
        )
        if request.url.path == "/api" or request.url.path.startswith("/api/"):
            return JSONResponse(
                content={
                    "detail": "Database is unavailable; app is in OwnTracks ingest buffer mode."
                },
                status_code=503,
                headers={"X-Mileage-Logger-Limp-Mode": "true"},
            )
        runtime_status = build_runtime_status(settings, database_available=False)
        return templates.TemplateResponse(
            request,
            "limp_mode.html",
            {
                "settings": settings,
                "buffer_stats": runtime_status.buffer_stats,
                "runtime_status": runtime_status,
            },
            headers={"X-Mileage-Logger-Limp-Mode": "true"},
        )


@app.middleware("http")
async def require_web_login(request: Request, call_next):
    return await enforce_web_login(request, call_next)


@app.middleware("http")
async def require_web_api_bearer_auth(request: Request, call_next):
    path = request.url.path
    if (path == "/api" or path.startswith("/api/")) and path not in WEB_API_AUTH_EXEMPT_PATHS:
        try:
            verify_web_api_auth(request)
        except HTTPException as exc:
            return JSONResponse(
                content={"detail": exc.detail},
                status_code=exc.status_code,
                headers=exc.headers,
            )
    return await call_next(request)


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
