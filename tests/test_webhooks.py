import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import init_db
from app.main import app
from app.webhooks import (
    EVENT_DEVICE_OFFLINE,
    EVENT_DEVICE_ONLINE,
    EVENT_ENERGY_WASTE,
    EVENT_PING,
    MAX_QUEUE_SIZE,
    WebhookDispatcher,
    mask_url,
    WebhookEvent,
    sign_payload,
)


def setup_module(module):
    init_db()


def make_dispatcher(handler, **overrides):
    kwargs = {
        "webhook_enabled": True,
        "webhook_urls": "https://example.test/hook",
        "webhook_secret": "test-secret",
        "webhook_events": "*",
        "webhook_max_retries": 2,
        "webhook_retry_backoff": 0,
    }
    kwargs.update(overrides)
    settings = Settings(**kwargs)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    dispatcher = WebhookDispatcher(client=client)
    dispatcher._settings = settings
    return dispatcher


def test_sign_payload_matches_manual_hmac():
    body = '{"event":"webhook.ping"}'
    expected = hmac.new(b"s3cr3t", f"1700000000.{body}".encode(), hashlib.sha256).hexdigest()
    assert sign_payload("s3cr3t", "1700000000", body) == f"sha256={expected}"


def test_sign_payload_changes_when_body_changes():
    a = sign_payload("s3cr3t", "1700000000", '{"a":1}')
    b = sign_payload("s3cr3t", "1700000000", '{"a":2}')
    assert a != b


def test_deliver_sends_signed_request():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200)

    dispatcher = make_dispatcher(handler)
    dispatcher._deliver(WebhookEvent(event=EVENT_DEVICE_OFFLINE, data={"device_id": "PY-NODE-101"}))

    assert captured["url"] == "https://example.test/hook"
    assert captured["headers"]["x-webhook-event"] == EVENT_DEVICE_OFFLINE

    body = json.loads(captured["body"])
    assert body["event"] == EVENT_DEVICE_OFFLINE
    assert body["data"]["device_id"] == "PY-NODE-101"

    expected_sig = sign_payload(
        "test-secret", captured["headers"]["x-webhook-timestamp"], captured["body"]
    )
    assert captured["headers"]["x-webhook-signature"] == expected_sig

    delivery = dispatcher.recent_deliveries()[0]
    assert delivery["ok"] is True
    assert delivery["attempts"] == 1


def test_deliver_retries_on_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500) if calls["n"] == 1 else httpx.Response(204)

    dispatcher = make_dispatcher(handler)
    dispatcher._deliver(WebhookEvent(event=EVENT_PING, data={}))

    assert calls["n"] == 2
    assert dispatcher.recent_deliveries()[0]["ok"] is True


def test_deliver_does_not_retry_on_4xx():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    dispatcher = make_dispatcher(handler)
    dispatcher._deliver(WebhookEvent(event=EVENT_PING, data={}))

    assert calls["n"] == 1
    delivery = dispatcher.recent_deliveries()[0]
    assert delivery["ok"] is False
    assert delivery["status_code"] == 404
    assert delivery["attempts"] == 1


def test_deliver_gives_up_after_max_retries_on_network_error():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused", request=request)

    dispatcher = make_dispatcher(handler)
    dispatcher._deliver(WebhookEvent(event=EVENT_PING, data={}))

    assert calls["n"] == 3
    delivery = dispatcher.recent_deliveries()[0]
    assert delivery["ok"] is False
    assert delivery["attempts"] == 3


def test_emit_queues_and_worker_sends():
    received = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content.decode("utf-8"))["event"])
        return httpx.Response(200)

    dispatcher = make_dispatcher(handler)
    dispatcher.start()
    try:
        assert dispatcher.emit(EVENT_PING, {"hello": "world"}) is True
        dispatcher._queue.join()
    finally:
        dispatcher.stop()

    assert received == [EVENT_PING]


def test_stop_does_not_block_when_queue_is_full():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    dispatcher = make_dispatcher(handler)
    dispatcher.start()
    try:
        for _ in range(MAX_QUEUE_SIZE + 50):
            dispatcher.emit(EVENT_PING, {})
    finally:
        dispatcher.stop()

    assert dispatcher._worker is None


def test_emit_is_noop_when_disabled():
    dispatcher = make_dispatcher(lambda r: httpx.Response(200), webhook_enabled=False)
    assert dispatcher.enabled is False
    assert dispatcher.emit(EVENT_PING, {}) is False


def test_emit_is_noop_when_no_urls_configured():
    dispatcher = make_dispatcher(lambda r: httpx.Response(200), webhook_urls="")
    assert dispatcher.enabled is False
    assert dispatcher.emit(EVENT_PING, {}) is False


def test_event_filter_blocks_unlisted_events():
    dispatcher = make_dispatcher(
        lambda r: httpx.Response(200), webhook_events="device.offline,alert.energy_waste"
    )
    assert dispatcher.emit(EVENT_DEVICE_OFFLINE, {}) is True
    assert dispatcher.emit(EVENT_PING, {}) is False
    dispatcher._queue.queue.clear()


def test_multiple_urls_all_receive():
    hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(200)

    dispatcher = make_dispatcher(
        handler, webhook_urls="https://a.test/hook, https://b.test/hook"
    )
    dispatcher._deliver(WebhookEvent(event=EVENT_PING, data={}))
    assert hits == ["https://a.test/hook", "https://b.test/hook"]


def test_mask_url_hides_path_and_query():
    assert mask_url("https://hooks.slack.com/services/T00/B00/XXXX") == "https://hooks.slack.com/***"
    assert mask_url("http://localhost:9000/webhook?token=abc") == "http://localhost:9000/***"
    assert mask_url("https://example.test") == "https://example.test"
    assert mask_url("이상한값") == "***"


def test_recent_deliveries_masks_url_by_default():
    dispatcher = make_dispatcher(
        lambda r: httpx.Response(200), webhook_urls="https://hooks.test/services/secret-token"
    )
    dispatcher._deliver(WebhookEvent(event=EVENT_PING, data={}))

    assert dispatcher.recent_deliveries()[0]["url"] == "https://hooks.test/***"
    assert dispatcher.recent_deliveries(mask=False)[0]["url"] == "https://hooks.test/services/secret-token"


@pytest.fixture
def captured_events(monkeypatch):
    from app import mqtt_handlers

    events = []

    def fake_emit(event, data):
        events.append((event, data))
        return True

    monkeypatch.setattr(mqtt_handlers.webhook_dispatcher, "emit", fake_emit)
    mqtt_handlers._last_alert_at.clear()
    return events


def test_status_change_emits_event_only_once(captured_events):
    from app.mqtt_handlers import handle_status_message

    topic = "v1/hook-b/f1/room-201/status"
    handle_status_message(topic, json.dumps({"status": "offline"}).encode())
    handle_status_message(topic, json.dumps({"status": "offline"}).encode())
    handle_status_message(topic, json.dumps({"status": "online"}).encode())

    names = [e[0] for e in captured_events]
    assert names == [EVENT_DEVICE_OFFLINE, EVENT_DEVICE_ONLINE]
    assert captured_events[0][1]["room_id"] == "room-201"
    assert captured_events[1][1]["previous_status"] == "offline"


def test_energy_waste_alert_emitted_with_cooldown(captured_events):
    from app.mqtt_handlers import handle_data_message

    topic = "v1/hook-b/f1/room-202/data"

    def payload(power, occupancy, ts):
        return json.dumps(
            {
                "device_id": "HOOK-NODE-202",
                "timestamp": ts,
                "metrics": {"occupancy": occupancy, "power": power, "temp": 25.0},
            }
        ).encode()

    handle_data_message(topic, payload(120.0, False, 1755590100))
    handle_data_message(topic, payload(130.0, False, 1755590105))
    handle_data_message(topic, payload(150.0, True, 1755590110))
    handle_data_message(topic, payload(0.5, False, 1755590115))

    assert [e[0] for e in captured_events] == [EVENT_ENERGY_WASTE]
    assert captured_events[0][1]["power"] == 120.0
    assert captured_events[0][1]["room_id"] == "room-202"


def test_webhook_config_endpoint():
    with TestClient(app) as client:
        resp = client.get("/api/webhooks")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is False
        assert EVENT_DEVICE_OFFLINE in body["known_events"]


def test_webhook_test_endpoint_conflicts_when_disabled():
    with TestClient(app) as client:
        resp = client.post("/api/webhooks/test")
        assert resp.status_code == 409


def test_webhook_deliveries_endpoint_returns_list():
    with TestClient(app) as client:
        resp = client.get("/api/webhooks/deliveries?limit=5")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
