import hashlib
import hmac
import json
import logging
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

import httpx

from .config import get_settings
from .utils import utcnow

logger = logging.getLogger("smart_energy.webhooks")

EVENT_DEVICE_ONLINE = "device.online"
EVENT_DEVICE_OFFLINE = "device.offline"
EVENT_ENERGY_WASTE = "alert.energy_waste"
EVENT_PING = "webhook.ping"

KNOWN_EVENTS = (
    EVENT_DEVICE_ONLINE,
    EVENT_DEVICE_OFFLINE,
    EVENT_ENERGY_WASTE,
    EVENT_PING,
)

MAX_QUEUE_SIZE = 1000
MAX_DELIVERY_LOG = 50


@dataclass
class WebhookEvent:
    event: str
    data: Dict[str, Any]
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    occurred_at: str = field(default_factory=lambda: utcnow().isoformat() + "Z")

    def to_body(self) -> Dict[str, Any]:
        return {
            "id": self.event_id,
            "event": self.event,
            "occurred_at": self.occurred_at,
            "data": self.data,
        }


def sign_payload(secret: str, timestamp: str, body: str) -> str:
    message = f"{timestamp}.{body}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def mask_url(url: str) -> str:
    try:
        scheme, _, rest = url.partition("://")
        if not rest:
            return "***"
        host, slash, tail = rest.partition("/")
        if not slash or not tail:
            return f"{scheme}://{host}"
        return f"{scheme}://{host}/***"
    except Exception:
        return "***"


class WebhookDispatcher:
    def __init__(self, client: Optional[httpx.Client] = None):
        self._settings = get_settings()
        self._queue: "queue.Queue[Optional[WebhookEvent]]" = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self._worker: Optional[threading.Thread] = None
        self._client = client
        self._owns_client = client is None
        self._deliveries: Deque[Dict[str, Any]] = deque(maxlen=MAX_DELIVERY_LOG)
        self._log_lock = threading.Lock()
        self._started = False
        self._stopping = threading.Event()

    @property
    def urls(self) -> List[str]:
        raw = self._settings.webhook_urls or ""
        return [u.strip() for u in raw.split(",") if u.strip()]

    @property
    def enabled(self) -> bool:
        return bool(self._settings.webhook_enabled and self.urls)

    @property
    def event_filter(self) -> Optional[List[str]]:
        raw = (self._settings.webhook_events or "").strip()
        if not raw or raw == "*":
            return None
        return [e.strip() for e in raw.split(",") if e.strip()]

    def _should_send(self, event_name: str) -> bool:
        allowed = self.event_filter
        return allowed is None or event_name in allowed

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stopping.clear()
        if not self.enabled:
            logger.info("웹훅 비활성화 상태 (WEBHOOK_ENABLED=false 이거나 WEBHOOK_URLS 비어 있음)")
            return
        self._worker = threading.Thread(target=self._run, name="webhook-dispatcher", daemon=True)
        self._worker.start()
        logger.info("웹훅 디스패처 시작: 엔드포인트 %d개", len(self.urls))

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self._stopping.set()
        if self._worker is not None:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                logger.warning("웹훅 큐가 가득 차 종료 신호를 넣지 못했습니다. 플래그로 종료합니다.")
            self._worker.join(timeout=5)
            self._worker = None
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def emit(self, event: str, data: Dict[str, Any]) -> bool:
        try:
            if not self.enabled or not self._should_send(event):
                return False
            self._queue.put_nowait(WebhookEvent(event=event, data=data))
            return True
        except queue.Full:
            logger.warning("웹훅 큐가 가득 찼습니다. 이벤트 폐기: %s", event)
            return False
        except Exception:
            logger.exception("웹훅 emit 중 예외 (event=%s)", event)
            return False

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._settings.webhook_timeout)
            self._owns_client = True
        return self._client

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None or self._stopping.is_set():
                    return
                self._deliver(item)
            except Exception:
                logger.exception("웹훅 전송 워커에서 예외 발생")
            finally:
                self._queue.task_done()

    def _build_headers(self, event: WebhookEvent, body: str) -> Dict[str, str]:
        """
        전송 시도마다 새로 만든다. 최초 타임스탬프와 서명을 재시도에 재사용하면,
        수신자가 타임스탬프 허용 시간을 검사할 때 재시도가 리플레이로 거부된다.
        """
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "smart-energy-backend-webhook/1.0",
            "X-Webhook-Event": event.event,
            "X-Webhook-Delivery": event.event_id,
            "X-Webhook-Timestamp": timestamp,
        }
        if self._settings.webhook_secret:
            headers["X-Webhook-Signature"] = sign_payload(
                self._settings.webhook_secret, timestamp, body
            )
        return headers

    def _deliver(self, event: WebhookEvent) -> None:
        body = json.dumps(event.to_body(), ensure_ascii=False, separators=(",", ":"))
        for url in self.urls:
            self._post_with_retry(url, body, event)

    def _post_with_retry(self, url: str, body: str, event: WebhookEvent) -> None:
        max_attempts = max(1, self._settings.webhook_max_retries + 1)
        delay = self._settings.webhook_retry_backoff
        last_error: Optional[str] = None
        status_code: Optional[int] = None
        attempts_made = 0

        for attempt in range(1, max_attempts + 1):
            attempts_made = attempt
            try:
                headers = self._build_headers(event, body)
                resp = self._get_client().post(url, content=body.encode("utf-8"), headers=headers)
                status_code = resp.status_code
                if 200 <= resp.status_code < 300:
                    self._record(event, url, attempt, True, status_code, None)
                    logger.debug("웹훅 전송 성공: %s -> %s (%s)", event.event, url, status_code)
                    return
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    last_error = f"HTTP {resp.status_code} (재시도하지 않음)"
                    break
                last_error = f"HTTP {resp.status_code}"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"

            if attempt < max_attempts:
                if self._stopping.is_set():
                    last_error = f"{last_error} (종료로 중단)"
                    break
                logger.warning(
                    "웹훅 전송 실패(%s, %d/%d회): %s. %d초 후 재시도",
                    url,
                    attempt,
                    max_attempts,
                    last_error,
                    delay,
                )
                # time.sleep이면 종료 시 최대 60초까지 붙잡혀, join(timeout=5)이 끝난 뒤
                # httpx 클라이언트가 사용 중에 닫힐 수 있다. 종료 신호를 기다리도록 바꾼다.
                if self._stopping.wait(delay):
                    last_error = f"{last_error} (종료로 중단)"
                    break
                delay = min(delay * 2, 60)

        self._record(event, url, attempts_made, False, status_code, last_error)
        logger.error("웹훅 전송 최종 실패: %s -> %s (%s)", event.event, url, last_error)

    def _record(
        self,
        event: WebhookEvent,
        url: str,
        attempts: int,
        ok: bool,
        status_code: Optional[int],
        error: Optional[str],
    ) -> None:
        with self._log_lock:
            self._deliveries.appendleft(
                {
                    "delivery_id": event.event_id,
                    "event": event.event,
                    "url": url,
                    "ok": ok,
                    "status_code": status_code,
                    "attempts": attempts,
                    "error": error,
                    "sent_at": utcnow().isoformat() + "Z",
                }
            )

    def recent_deliveries(self, limit: int = MAX_DELIVERY_LOG, mask: bool = True) -> List[Dict[str, Any]]:
        with self._log_lock:
            rows = list(self._deliveries)[:limit]
        if not mask:
            return rows
        return [{**row, "url": mask_url(row["url"])} for row in rows]


webhook_dispatcher = WebhookDispatcher()
