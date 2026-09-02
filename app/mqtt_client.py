import logging
import ssl
import threading
import time

import paho.mqtt.client as mqtt

from .config import get_settings
from .mqtt_handlers import handle_data_message, handle_status_message

logger = logging.getLogger("smart_energy.mqtt_client")

settings = get_settings()

DATA_TOPIC = f"{settings.mqtt_topic_prefix}/+/+/+/data"
STATUS_TOPIC = f"{settings.mqtt_topic_prefix}/+/+/+/status"


class MQTTService:
    def __init__(self):
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=settings.mqtt_client_id,
            clean_session=True,
            protocol=mqtt.MQTTv311,
        )
        self._connected = threading.Event()
        self._stopping = False

        if settings.mqtt_username:
            self._client.username_pw_set(settings.mqtt_username, settings.mqtt_password)

        if settings.mqtt_use_tls:
            self._client.tls_set(cert_reqs=ssl.CERT_REQUIRED)

        self._client.reconnect_delay_set(
            min_delay=settings.mqtt_reconnect_min_delay,
            max_delay=settings.mqtt_reconnect_max_delay,
        )

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    def start(self) -> None:
        if not settings.mqtt_enabled:
            logger.info("MQTT 비활성화 상태입니다 (MQTT_ENABLED=false). 브로커 연결을 시도하지 않습니다.")
            return
        self._stopping = False
        threading.Thread(target=self._connect_with_retry, name="mqtt-initial-connect", daemon=True).start()

    def stop(self) -> None:
        self._stopping = True
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            logger.exception("MQTT 클라이언트 종료 중 오류")

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def _connect_with_retry(self) -> None:
        delay = settings.mqtt_reconnect_min_delay
        attempt = 0
        while not self._stopping:
            attempt += 1
            try:
                logger.log(
                    logging.INFO if attempt == 1 else logging.DEBUG,
                    "MQTT 브로커 연결 시도: %s:%s",
                    settings.mqtt_host,
                    settings.mqtt_port,
                )
                self._client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=settings.mqtt_keepalive)
                self._client.loop_start()
                if attempt > 1:
                    logger.info("MQTT 브로커에 연결되었습니다 (%d회 시도).", attempt)
                return
            except Exception as exc:
                if attempt == 1:
                    logger.warning("MQTT 브로커(%s:%s)에 연결하지 못했습니다: %s", settings.mqtt_host, settings.mqtt_port, exc)
                    logger.warning("  해결: 브로커(mosquitto 등)를 실행하거나, 브로커 없이 API만 개발 중이라면")
                    logger.warning("        .env에 MQTT_ENABLED=false 를 넣으면 이 메시지가 사라집니다.")
                    logger.warning("  %d초 후부터 백그라운드에서 조용히 재시도합니다.", delay)
                elif attempt % 10 == 0:
                    logger.info("MQTT 브로커에 아직 연결되지 않았습니다 (%d회 시도, 다음 재시도 %d초 후).", attempt, delay)
                else:
                    logger.debug("MQTT 연결 실패(%d회차): %s. %d초 후 재시도.", attempt, exc, delay)
                time.sleep(delay)
                delay = min(delay * 2, settings.mqtt_reconnect_max_delay)

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties=None):
        if reason_code == 0:
            self._connected.set()
            logger.info("MQTT 브로커 연결 성공")
            client.subscribe([(DATA_TOPIC, 1), (STATUS_TOPIC, 1)])
            logger.info("토픽 구독 완료: %s, %s", DATA_TOPIC, STATUS_TOPIC)
        else:
            self._connected.clear()
            logger.error("MQTT 연결 실패 (reason_code=%s)", reason_code)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        self._connected.clear()
        if reason_code != 0 and not self._stopping:
            logger.warning(
                "MQTT 연결이 예기치 않게 끊어졌습니다 (reason_code=%s). 자동 재연결 대기 중...", reason_code
            )
        else:
            logger.info("MQTT 연결 정상 종료")

    def _on_message(self, client, userdata, msg):
        try:
            if msg.topic.endswith("/data"):
                handle_data_message(msg.topic, msg.payload)
            elif msg.topic.endswith("/status"):
                handle_status_message(msg.topic, msg.payload)
            else:
                logger.debug("처리 대상이 아닌 토픽 수신: %s", msg.topic)
        except Exception:
            logger.exception("메시지 처리 중 예외 발생 (topic=%s)", msg.topic)


mqtt_service = MQTTService()
