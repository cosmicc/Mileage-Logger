import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from mileage_logger.api.routes import router as api_router
from mileage_logger.config import get_settings
from mileage_logger.database import engine
from mileage_logger.logging_config import configure_logging
from mileage_logger.models import Base
from mileage_logger.services.mqtt import MqttOwnTracksWorker
from mileage_logger.web.routes import router as web_router

settings = get_settings()
mqtt_worker = MqttOwnTracksWorker()
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    log_path = configure_logging("app")
    logger.info("Starting Mileage Logger app, log_path=%s", log_path)
    if settings.create_tables_on_startup:
        Base.metadata.create_all(bind=engine)
    mqtt_worker.start()
    try:
        yield
    finally:
        logger.info("Stopping Mileage Logger app")
        mqtt_worker.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)


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


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(api_router, prefix="/api", tags=["api"])
app.include_router(web_router, tags=["web"])
