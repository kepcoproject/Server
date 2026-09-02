import json

from app.database import SessionLocal, init_db
from app.models import Device, SensorReading
from app.mqtt_handlers import handle_data_message, handle_status_message, parse_topic


def setup_module(module):
    init_db()


def test_parse_topic_valid():
    info = parse_topic("v1/bldg-a/f2/room-101/data")
    assert info is not None
    assert info.building == "bldg-a"
    assert info.floor == "f2"
    assert info.room_id == "room-101"
    assert info.message_type == "data"


def test_parse_topic_invalid_shapes():
    assert parse_topic("v1/bldg-a/f2/data") is None
    assert parse_topic("v1/bldg-a/f2/room-101/unknown") is None


def test_handle_data_message_creates_device_and_reading():
    topic = "v1/bldg-a/f2/room-101/data"
    payload = json.dumps(
        {
            "device_id": "PY-NODE-101",
            "timestamp": 1755590000,
            "metrics": {"occupancy": True, "power": 12.5, "temp": 24.5},
        }
    ).encode()

    handle_data_message(topic, payload)

    db = SessionLocal()
    try:
        device = db.get(Device, "PY-NODE-101")
        assert device is not None
        assert device.building == "bldg-a"
        assert device.floor == "f2"
        assert device.room_id == "room-101"

        reading = db.query(SensorReading).filter_by(device_id="PY-NODE-101").first()
        assert reading is not None
        assert reading.power == 12.5
        assert reading.occupancy is True
        assert reading.temp == 24.5
    finally:
        db.close()


def test_handle_data_message_ignores_malformed_payload():
    topic = "v1/bldg-a/f2/room-101/data"
    bad_payload = b"{not valid json"

    handle_data_message(topic, bad_payload)


def test_handle_status_message_updates_cache_and_db():
    from app.status_cache import status_cache

    topic = "v1/bldg-a/f2/room-101/status"
    payload = json.dumps({"status": "offline"}).encode()

    handle_status_message(topic, payload)

    db = SessionLocal()
    try:
        device = (
            db.query(Device).filter_by(building="bldg-a", floor="f2", room_id="room-101").first()
        )
        assert device is not None
        assert device.status.value == "offline"

        cached = status_cache.get_status(device.device_id)
        assert cached is not None
        assert cached["status"] == "offline"
    finally:
        db.close()


def test_handle_status_message_creates_placeholder_device_if_unseen():
    topic = "v1/bldg-b/f5/room-999/status"
    payload = json.dumps({"status": "online"}).encode()

    handle_status_message(topic, payload)

    db = SessionLocal()
    try:
        device = (
            db.query(Device).filter_by(building="bldg-b", floor="f5", room_id="room-999").first()
        )
        assert device is not None
        assert device.status.value == "online"
        assert device.device_id == "bldg-b-f5-room-999"
    finally:
        db.close()


def test_placeholder_is_absorbed_when_real_device_reports():
    """status가 data보다 먼저 오는 실제 부팅 순서에서 유령 노드가 남지 않아야 한다."""
    handle_status_message(
        "v1/absorb-a/f1/room-1/status", json.dumps({"status": "online"}).encode()
    )
    handle_data_message(
        "v1/absorb-a/f1/room-1/data",
        json.dumps(
            {
                "device_id": "ABSORB-NODE-1",
                "timestamp": 1755591000,
                "metrics": {"occupancy": True, "power": 10.0, "temp": 22.0},
            }
        ).encode(),
    )

    db = SessionLocal()
    try:
        devices = (
            db.query(Device).filter_by(building="absorb-a", floor="f1", room_id="room-1").all()
        )
        # 임시 노드는 사라지고 진짜 노드 하나만 남는다
        assert [d.device_id for d in devices] == ["ABSORB-NODE-1"]
        assert db.get(Device, "absorb-a-f1-room-1") is None
        # 임시 노드가 들고 있던 online 상태를 넘겨받는다 (unknown에 머물면 안 된다)
        assert devices[0].status.value == "online"
    finally:
        db.close()


def test_data_message_stores_lux():
    """조도는 센서 융합(밝은데 조명 켜짐 판정)에 쓰이므로 저장되어야 한다."""
    handle_data_message(
        "v1/lux-a/f1/room-10/data",
        json.dumps(
            {
                "device_id": "LUX-NODE-10",
                "timestamp": 1790300000,
                "metrics": {"occupancy": False, "power": 300.0, "lux": 812.5},
            }
        ).encode(),
    )
    db = SessionLocal()
    try:
        reading = (
            db.query(SensorReading)
            .filter_by(device_id="LUX-NODE-10")
            .order_by(SensorReading.id.desc())
            .first()
        )
        assert reading is not None and reading.lux == 812.5
    finally:
        db.close()


def test_command_ack_completes_command_and_writes_log():
    """
    MQTT 노드는 실행 결과를 ack 로 돌려준다. HTTP 폴링과 달리 '가져갔다'가 아니라
    '실행했다'를 알 수 있으므로 그대로 기록해야 한다.
    """
    from app.models import ControlCommand, ControlLog
    from app.mqtt_handlers import handle_command_ack

    db = SessionLocal()
    try:
        db.add(
            ControlCommand(
                command_id="cmd-ack-test",
                space_id="sp-ack",
                action="LIGHT",
                value="OFF",
                trigger="MANUAL",
                status="PENDING",
            )
        )
        db.commit()
    finally:
        db.close()

    handle_command_ack(
        "v1/ack-a/f1/room-1/cmd/ack",
        json.dumps({"command_id": "cmd-ack-test", "result": "COMPLETED"}).encode(),
    )

    db = SessionLocal()
    try:
        command = db.get(ControlCommand, "cmd-ack-test")
        assert command.status == "COMPLETED"
        assert command.completed_at is not None

        log = db.query(ControlLog).filter_by(space_id="sp-ack").first()
        assert log is not None and log.result == "COMPLETED"
    finally:
        db.close()


def test_command_ack_ignores_unknown_and_malformed():
    from app.mqtt_handlers import handle_command_ack

    # 예외 없이 조용히 무시되어야 한다 (수신 루프가 죽으면 안 된다)
    handle_command_ack("v1/a/f/r/cmd/ack", b"{not json")
    handle_command_ack("v1/a/f/r/cmd/ack", json.dumps({"result": "COMPLETED"}).encode())
    handle_command_ack("v1/a/f/r/cmd/ack", json.dumps({"command_id": "없는명령"}).encode())
    handle_command_ack("v1/a/f/r/wrong", json.dumps({"command_id": "x"}).encode())
