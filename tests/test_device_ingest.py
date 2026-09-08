"""
ESP32 노드 HTTP 연동 검증.

펌웨어(esp32_sensor_node.ino)가 실제로 보내는 모양 그대로 요청해서,
저장·환산·제어 폴링이 맞는지 본다.
"""
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.main import app
from app.models import ControlCommand, ControlLog, Device, SensorReading, Space

settings = get_settings()


def setup_module(module):
    init_db()


def _firmware_payload(node_key, space_id, occupancy=True, lux=512.0, amp=1.5):
    """펌웨어의 postSensorData() 가 만드는 것과 동일한 본문."""
    return {
        "node_key": node_key,
        "space_id": space_id,
        "occupancy": occupancy,
        "light_lux": lux,
        "current_amp": amp,
        "source": "real",
    }


def _login(client):
    resp = client.post("/auth/login", json={"loginId": "demo", "password": "demo1234"})
    return {"Authorization": f"Bearer {resp.json()['data']['accessToken']}"}


def test_ingest_creates_space_node_and_reading():
    with TestClient(app) as client:
        resp = client.post("/api/sensors/data", json=_firmware_payload("node-fw-1", 4100))
    assert resp.status_code == 200
    body = resp.json()
    assert body["stored"] is True

    db = SessionLocal()
    try:
        # SPACE_ID 에 맞는 공간이 없으면 자동 등록되어 데이터가 버려지지 않는다
        space = db.get(Space, "sp-4100")
        assert space is not None

        device = db.get(Device, "node-fw-1")
        assert device is not None
        assert device.status.value == "online"
        assert device.last_seen is not None

        reading = (
            db.query(SensorReading)
            .filter_by(device_id="node-fw-1")
            .order_by(SensorReading.id.desc())
            .first()
        )
        assert reading is not None
        # 전류(A) x 전압 = 전력(W)
        assert reading.power == round(1.5 * settings.sensor_line_voltage, 2)
        assert reading.lux == 512.0
        assert reading.occupancy is True
        # 노드에 온도 센서가 없다
        assert reading.temp is None
        # 펌웨어가 시각을 안 보내므로 서버가 수신 시각을 채운다
        assert reading.device_timestamp > 0
    finally:
        db.close()


def test_ingest_accepts_both_id_forms():
    """펌웨어는 정수(1), 화면은 문자열("sp-1")을 쓴다. 둘 다 같은 공간으로 가야 한다."""
    with TestClient(app) as client:
        client.post("/api/sensors/data", json=_firmware_payload("node-fw-2", 4200))
        resp = client.post("/api/sensors/data", json=_firmware_payload("node-fw-2", "sp-4200"))
    assert resp.status_code == 200
    assert resp.json()["spaceId"] == "sp-4200"


def test_ingest_rejects_missing_fields():
    with TestClient(app) as client:
        assert client.post("/api/sensors/data", json={"space_id": 1}).status_code == 400
        assert client.post("/api/sensors/data", json={"node_key": "x"}).status_code == 400


def test_actuator_poll_is_empty_without_command():
    with TestClient(app) as client:
        client.post("/api/sensors/data", json=_firmware_payload("node-fw-3", 4300))
        resp = client.get("/api/spaces/4300/actuator/latest", params={"actuator_type": "light"})
    # 펌웨어는 200일 때만 릴레이를 건드린다. 명령이 없으면 그대로 둬야 한다.
    assert resp.status_code == 204


def test_actuator_poll_delivers_command_without_envelope():
    with TestClient(app) as client:
        client.post("/api/sensors/data", json=_firmware_payload("node-fw-4", 4400))
        headers = _login(client)

        accepted = client.post(
            "/control/commands",
            headers=headers,
            json={"spaceId": "sp-4400", "action": "LIGHT", "value": "OFF"},
        )
        assert accepted.status_code == 202
        command_id = accepted.json()["data"]["commandId"]

        resp = client.get("/api/spaces/4400/actuator/latest", params={"actuator_type": "light"})
        assert resp.status_code == 200
        body = resp.json()

        # 펌웨어는 doc["action"] 을 그대로 읽는다. 봉투를 씌우면 안 된다.
        assert set(body) >= {"action", "source"}
        assert "success" not in body and "data" not in body
        assert body["action"] == "off"
        assert body["source"] == "manual"

        # 가져간 뒤에는 완료로 바뀌고 이력이 남는다
        status = client.get(f"/control/commands/{command_id}", headers=headers).json()["data"]
        assert status["status"] == "COMPLETED"

    db = SessionLocal()
    try:
        log = (
            db.query(ControlLog)
            .filter_by(space_id="sp-4400")
            .order_by(ControlLog.log_id.desc())
            .first()
        )
        assert log is not None and log.result == "COMPLETED"
    finally:
        db.close()


def test_ingest_makes_node_visible_to_dashboard():
    """수집된 노드가 프론트 화면(재실 맵)에 그대로 나타나야 한다."""
    with TestClient(app) as client:
        client.post(
            "/api/sensors/data",
            json=_firmware_payload("node-fw-5", 4500, occupancy=False, amp=2.0),
        )
        headers = _login(client)
        spaces = client.get("/monitoring/occupancy-map", headers=headers).json()["data"]["spaces"]

    mine = [s for s in spaces if s["spaceId"] == "sp-4500"]
    assert len(mine) == 1
    assert mine[0]["occupied"] is False
    # 2.0A x 220V = 440W -> 공실이므로 낭비로 잡혀야 한다
    assert mine[0]["powerW"] == round(2.0 * settings.sensor_line_voltage, 1)
    assert mine[0]["wasteFlag"] is True


def test_ingest_timestamp_is_utc_not_local():
    """
    utcnow() 는 tzinfo 를 뗀 UTC라, 그냥 .timestamp() 하면 로컬 시간대로 해석되어
    어긋난다(KST면 9시간). 재실 확률이 엉뚱한 시간대에 쌓이게 되는 문제라 못박아 둔다.
    """
    before = int(datetime.now(timezone.utc).timestamp())
    with TestClient(app) as client:
        client.post("/api/sensors/data", json=_firmware_payload("node-fw-tz", 4600))
    after = int(datetime.now(timezone.utc).timestamp())

    db = SessionLocal()
    try:
        reading = (
            db.query(SensorReading)
            .filter_by(device_id="node-fw-tz")
            .order_by(SensorReading.id.desc())
            .first()
        )
        # 실제 UTC epoch 범위 안에 있어야 한다. 시간대가 틀리면 몇 시간씩 벗어난다.
        assert before <= reading.device_timestamp <= after, (
            f"저장된 시각이 UTC 범위를 벗어남: {reading.device_timestamp} "
            f"(기대 {before}~{after})"
        )
    finally:
        db.close()


def test_stale_pending_command_expires_instead_of_firing_late():
    """
    화면이 15초 만에 포기한 명령이 몇 시간 뒤 노드가 붙는 순간 실행되면 안 된다.
    사용자가 손대지 않았는데 조명이 저절로 바뀌는 상황이라 못박아 둔다.
    """
    from datetime import timedelta

    from app.utils import utcnow

    with TestClient(app) as client:
        client.post("/api/sensors/data", json=_firmware_payload("node-fw-6", 4700))
        headers = _login(client)
        accepted = client.post(
            "/control/commands",
            headers=headers,
            json={"spaceId": "sp-4700", "action": "LIGHT", "value": "OFF"},
        )
        assert accepted.status_code == 202
        command_id = accepted.json()["data"]["commandId"]

        # 명령이 오래 방치된 상황을 만든다
        db = SessionLocal()
        try:
            command = db.get(ControlCommand, command_id)
            command.created_at = utcnow() - timedelta(hours=3)
            db.commit()
        finally:
            db.close()

        # 뒤늦게 노드가 폴링해도 아무것도 안 내려와야 한다
        resp = client.get("/api/spaces/4700/actuator/latest", params={"actuator_type": "light"})
        assert resp.status_code == 204

        status = client.get(f"/control/commands/{command_id}", headers=headers).json()["data"]
        assert status["status"] == "FAILED"


def test_control_rejects_node_that_is_only_online_by_stale_data():
    """
    노드 목록은 '데이터 기준 시각'으로 온라인을 판정한다. 데모 데이터처럼 전체가
    과거에 멈춰 있으면 그 시점 기준으로는 모두 온라인이다. 목록에서는 맞지만
    제어는 지금 명령을 가져갈 노드가 있어야 성립한다. 화면이 '온라인'이라 해놓고
    15초 뒤 '응답 없음'만 뜨던 원인이라 여기서 막는다.
    """
    from datetime import timedelta

    from app.utils import utcnow

    with TestClient(app) as client:
        client.post("/api/sensors/data", json=_firmware_payload("node-fw-7", 4800))
        headers = _login(client)

        db = SessionLocal()
        try:
            device = db.get(Device, "node-fw-7")
            device.last_seen = utcnow() - timedelta(days=3)
            db.commit()
        finally:
            db.close()

        resp = client.post(
            "/control/commands",
            headers=headers,
            json={"spaceId": "sp-4800", "action": "LIGHT", "value": "ON"},
        )

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "E5030"
