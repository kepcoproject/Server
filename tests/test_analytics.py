import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import analytics
from app.database import SessionLocal, init_db
from app.main import app
from app.models import Device, SensorReading
from app.mqtt_handlers import handle_data_message


def setup_module(module):
    init_db()


def test_recommend_covers_three_states():
    assert analytics.recommend(False, None) == analytics.RECOMMEND_WARMING_UP
    # 비어 있고 원래도 잘 안 쓰는 시간대 -> 절전 제안
    assert analytics.recommend(False, 0.05) == analytics.RECOMMEND_SAVE
    # 비어 있어도 평소 자주 쓰는 시간대면 잠깐 자리 비움으로 보고 알리지 않는다
    assert analytics.recommend(False, 0.9) == analytics.RECOMMEND_NORMAL
    assert analytics.recommend(True, 0.05) == analytics.RECOMMEND_NORMAL


def test_probability_uses_cumulative_average_while_samples_are_few():
    db = SessionLocal()
    try:
        ts = datetime(2026, 9, 1, 10, 0, 0)
        for occupancy in (True, True, False, False):
            analytics.update_occupancy_probability(db, "an-a", "f1", "room-1", occupancy, ts)
        db.commit()

        cell = analytics.get_cell(db, "an-a", "f1", "room-1", ts)
        assert cell.sample_count == 4
        assert abs(cell.probability - 0.5) < 1e-9
    finally:
        db.close()


def test_probability_is_keyed_by_weekday_and_hour():
    db = SessionLocal()
    try:
        monday = datetime(2026, 8, 31, 9, 0, 0)
        tuesday = datetime(2026, 9, 1, 9, 0, 0)
        analytics.update_occupancy_probability(db, "an-b", "f1", "room-2", True, monday)
        analytics.update_occupancy_probability(db, "an-b", "f1", "room-2", False, tuesday)
        db.commit()

        assert analytics.get_probability_now(db, "an-b", "f1", "room-2", monday) == 1.0
        assert analytics.get_probability_now(db, "an-b", "f1", "room-2", tuesday) == 0.0
    finally:
        db.close()


def test_compute_savings_against_baseline():
    db = SessionLocal()
    base = 1790000000
    try:
        db.add(Device(device_id="SAVE-NODE", building="sv-a", floor="f1", room_id="room-3"))
        for i in range(3):
            db.add(
                SensorReading(
                    device_id="SAVE-NODE",
                    building="sv-a",
                    floor="f1",
                    room_id="room-3",
                    occupancy=False,
                    power=100.0,
                    temp=22.0,
                    device_timestamp=base + i * 3600,
                )
            )
        db.commit()

        start = datetime(1970, 1, 1) + timedelta(seconds=base)
        result = analytics.compute_savings(
            db, "sv-a", "f1", "room-3", start, start + timedelta(hours=2), baseline_power_w=200.0
        )
        assert result is not None
        assert result["period_hours"] == 2.0
        assert result["baseline_kwh"] == 0.4  # 200W x 2h
        assert result["actual_kwh"] == 0.2  # 100W x 2h
        assert result["saved_pct"] == 50.0
    finally:
        db.close()


def test_compute_savings_returns_none_without_data():
    db = SessionLocal()
    try:
        now = datetime(2026, 9, 1, 0, 0, 0)
        assert (
            analytics.compute_savings(db, "nope", "f0", "room-0", now, now + timedelta(hours=1))
            is None
        )
    finally:
        db.close()


def _publish(room, device_id, occupancy, power, when):
    handle_data_message(
        f"v1/{room}/data",
        json.dumps(
            {
                "device_id": device_id,
                "timestamp": int(when.timestamp()),
                "metrics": {"occupancy": occupancy, "power": power, "temp": 24.0},
            }
        ).encode(),
    )


def test_recommendation_endpoint():
    now = datetime.now(timezone.utc)
    _publish("api-an/f1/room-9", "AN-NODE-9", False, 80.0, now)

    with TestClient(app) as client:
        resp = client.get("/api/rooms/api-an/f1/room-9/recommendation")

    assert resp.status_code == 200
    body = resp.json()
    assert body["room_id"] == "room-9"
    assert body["occupancy"] is False
    assert body["sample_count"] >= 1
    # 비어 있고 이 시간대 관측이 전부 '비어있음'이므로 절전을 제안해야 한다
    assert body["recommendation"] == analytics.RECOMMEND_SAVE
    assert body["evaluated_at"].endswith("Z")


def test_savings_endpoint():
    now = datetime.now(timezone.utc)
    for i in range(3):
        _publish("api-sv/f1/room-10", "SV-NODE-10", False, 50.0, now - timedelta(minutes=30 * i))

    with TestClient(app) as client:
        resp = client.get("/api/rooms/api-sv/f1/room-10/savings", params={"hours": 24})

    assert resp.status_code == 200
    body = resp.json()
    assert body["baseline_kwh"] > 0
    assert 0 <= body["saved_pct"] <= 100
    assert body["sample_count"] == 3


def test_savings_endpoint_404_without_data():
    with TestClient(app) as client:
        assert client.get("/api/rooms/nowhere/f0/room-000/savings").status_code == 404


def test_savings_baseline_uses_measured_time_not_requested_window():
    """
    데이터가 듬성한 구간에서 절감률이 부풀려지면 안 된다.
    30일을 요청해도 실제 측정이 2시간뿐이면 기준선도 2시간치로 잡아야 한다.
    """
    db = SessionLocal()
    base = 1795000000
    try:
        db.add(Device(device_id="SPARSE-NODE", building="sp-a", floor="f1", room_id="room-4"))
        for i in range(3):
            db.add(
                SensorReading(
                    device_id="SPARSE-NODE",
                    building="sp-a",
                    floor="f1",
                    room_id="room-4",
                    occupancy=False,
                    power=100.0,
                    temp=22.0,
                    device_timestamp=base + i * 3600,
                )
            )
        db.commit()

        start = datetime(1970, 1, 1) + timedelta(seconds=base)
        result = analytics.compute_savings(
            db, "sp-a", "f1", "room-4", start, start + timedelta(days=30), baseline_power_w=200.0
        )
        assert result is not None
        assert result["period_hours"] == 720.0  # 요청한 기간
        assert result["covered_hours"] == 2.0  # 실제 측정된 시간
        assert result["baseline_kwh"] == 0.4  # 200W x 2h — 720h가 아니어야 한다
        assert result["actual_kwh"] == 0.2
        assert result["saved_pct"] == 50.0  # 99%가 아니어야 한다
    finally:
        db.close()
