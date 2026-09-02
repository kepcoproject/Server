import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.database import init_db
from app.main import app
from app.mqtt_handlers import handle_data_message, handle_status_message
from app.schemas import to_utc_z


def setup_module(module):
    init_db()


def _seed_room(building="tz-a", floor="f1", room="room-900", device_id="TZ-NODE-900"):
    now = datetime.now(timezone.utc)
    for offset in range(3):
        moment = now - timedelta(minutes=offset)
        handle_data_message(
            f"v1/{building}/{floor}/{room}/data",
            json.dumps(
                {
                    "device_id": device_id,
                    "timestamp": int(moment.timestamp()),
                    "metrics": {"occupancy": True, "power": 30.0, "temp": 23.0},
                }
            ).encode(),
        )
    handle_status_message(
        f"v1/{building}/{floor}/{room}/status", json.dumps({"status": "online"}).encode()
    )


def test_health_check():
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "mqtt_connected" in body
        assert "mqtt_enabled" in body


def test_list_devices_returns_list():
    with TestClient(app) as client:
        resp = client.get("/api/devices")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


def test_get_unknown_device_returns_404():
    with TestClient(app) as client:
        resp = client.get("/api/devices/NOT-A-REAL-DEVICE")
        assert resp.status_code == 404


def test_room_latest_without_data_returns_404():
    with TestClient(app) as client:
        resp = client.get("/api/rooms/nowhere/f0/room-000/latest")
        assert resp.status_code == 404


def test_to_utc_z_marks_utc():
    naive = datetime(2026, 8, 20, 8, 57, 7)
    assert to_utc_z(naive) == "2026-08-20T08:57:07Z"
    aware_kst = datetime(2026, 8, 20, 17, 57, 7, tzinfo=timezone(timedelta(hours=9)))
    assert to_utc_z(aware_kst) == "2026-08-20T08:57:07Z"
    assert to_utc_z(None) is None


def test_datetime_fields_are_marked_utc():
    _seed_room()
    with TestClient(app) as client:
        device = client.get("/api/devices/TZ-NODE-900").json()
        assert device["last_seen"].endswith("Z")
        assert device["last_status_change"].endswith("Z")

        reading = client.get("/api/rooms/tz-a/f1/room-900/latest").json()
        assert reading["received_at"].endswith("Z")


def test_since_accepts_offset_and_normalizes_to_utc():
    _seed_room()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)
    as_utc = cutoff.isoformat().replace("+00:00", "Z")
    as_kst = cutoff.astimezone(timezone(timedelta(hours=9))).isoformat()

    with TestClient(app) as client:
        utc_rows = client.get(
            "/api/rooms/tz-a/f1/room-900/history", params={"since": as_utc}
        ).json()
        kst_rows = client.get(
            "/api/rooms/tz-a/f1/room-900/history", params={"since": as_kst}
        ).json()

    assert len(utc_rows) > 0
    assert len(kst_rows) == len(utc_rows)


def test_since_in_future_returns_empty():
    _seed_room()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    with TestClient(app) as client:
        rows = client.get(
            "/api/rooms/tz-a/f1/room-900/history", params={"since": future}
        ).json()
    assert rows == []


def test_data_latest_returns_one_row_per_room():
    """한 공간에 노드가 둘이어도 /api/data/latest는 공간당 1건만 반환해야 한다."""
    for device_id in ("DUP-NODE-A", "DUP-NODE-B"):
        handle_data_message(
            "v1/dup-a/f1/room-7/data",
            json.dumps(
                {
                    "device_id": device_id,
                    "timestamp": 1755592000,
                    "metrics": {"occupancy": True, "power": 5.0, "temp": 21.0},
                }
            ).encode(),
        )

    with TestClient(app) as client:
        rows = client.get("/api/data/latest").json()

    keys = [(r["building"], r["floor"], r["room_id"]) for r in rows]
    assert keys.count(("dup-a", "f1", "room-7")) == 1
    assert len(keys) == len(set(keys))


def test_history_limit_rejects_out_of_range():
    """음수 limit은 SQLite에서 '무제한'이 되므로 반드시 막혀야 한다."""
    with TestClient(app) as client:
        for bad in (-1, 0, 1001):
            resp = client.get(
                "/api/rooms/tz-a/f1/room-900/history", params={"limit": bad}
            )
            assert resp.status_code == 422, f"limit={bad}이 통과됨"
