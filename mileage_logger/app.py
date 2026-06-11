from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from mileage_logger.api.routes import router as api_router
from mileage_logger.config import get_settings
from mileage_logger.database import engine
from mileage_logger.models import Base
from mileage_logger.services.mqtt import MqttOwnTracksWorker
from mileage_logger.web.routes import router as web_router

settings = get_settings()
mqtt_worker = MqttOwnTracksWorker()
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    if settings.create_tables_on_startup:
        Base.metadata.create_all(bind=engine)
    mqtt_worker.start()
    try:
        yield
    finally:
        mqtt_worker.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(api_router, prefix="/api", tags=["api"])
app.include_router(web_router, tags=["web"])
