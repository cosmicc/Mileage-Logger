import logging
from threading import Event, Thread

import paho.mqtt.client as mqtt

from mileage_logger.config import get_settings
from mileage_logger.database import SessionLocal
from mileage_logger.services.owntracks import (
    EmptyOwnTracksPayload,
    UnsupportedOwnTracksType,
    process_owntracks_payload,
)

logger = logging.getLogger(__name__)


class MqttOwnTracksWorker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._stop = Event()
        self._thread: Thread | None = None
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    def start(self) -> None:
        if not self.settings.mqtt_enabled or self._thread is not None:
            return
        if self.settings.mqtt_username:
            self._client.username_pw_set(self.settings.mqtt_username, self.settings.mqtt_password)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._thread = Thread(target=self._run, name="owntracks-mqtt", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._client.disconnect()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        self._client.connect(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=60)
        self._client.loop_start()
        self._stop.wait()
        self._client.loop_stop()

    def _on_connect(self, client: mqtt.Client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code == 0:
            client.subscribe(self.settings.mqtt_topic)
            logger.info("Subscribed to MQTT topic %s", self.settings.mqtt_topic)
        else:
            logger.error("MQTT connection failed: %s", reason_code)

    def _on_message(self, _client: mqtt.Client, _userdata, msg: mqtt.MQTTMessage) -> None:
        with SessionLocal() as db:
            try:
                process_owntracks_payload(db, msg.payload, topic=msg.topic)
            except (EmptyOwnTracksPayload, UnsupportedOwnTracksType):
                return
            except Exception:
                logger.exception("Could not process MQTT OwnTracks message on %s", msg.topic)
