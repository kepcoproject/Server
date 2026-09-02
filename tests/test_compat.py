"""프론트엔드 호환 레이어 검증."""
import json

from fastapi.testclient import TestClient

from app.api.compat_routes import ACCESS_TOKEN, make_space_id, parse_space_id
from app.database import init_db
from app.main import app
from app.mqtt_handlers import handle_data_message


def setup_module(module):
    init_db()


def _seed(building="cp-a", floor="f3", room="room-500", device_id="CP-NODE-500"):
    handle_data_message(
        f"v1/{building}/{floor}/{room}/data",
        json.dumps(
            {
                "device_id": device_id,
                "timestamp": 1790100000,
                "metrics": {"occupancy": False, "power": 300.0, "temp": 24.0},
            }
        ).encode(),
    )


def _auth():
    return {"Authorization": f"Bearer {ACCESS_TOKEN}"}


def test_space_id_round_trips():
    space_id = make_space_id("bldg-a", "f2", "room-101")
    assert parse_space_id(space_id) == ("bldg-a", "f2", "room-101")
    assert parse_space_id("깨진값") is None


def test_login_rejects_wrong_password():
    with TestClient(app) as client:
        resp = client.post("/auth/login", json={"loginId": "demo", "password": "nope"})
    assert resp.status_code == 401
    body = resp.json()
    # 프론트는 이 코드로 재시도 여부를 판단하므로 모양이 정확해야 한다
    assert body["success"] is False
    assert body["error"]["code"] == "E4010"


def test_login_returns_tokens_and_user():
    with TestClient(app) as client:
        resp = client.post("/auth/login", json={"loginId": "demo", "password": "demo1234"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["accessToken"] and data["refreshToken"]
    assert data["user"]["role"] == "ADMIN"


def test_me_requires_token():
    with TestClient(app) as client:
        assert client.get("/auth/me").status_code == 401
        resp = client.get("/auth/me", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["data"]["loginId"] == "demo"


def test_refresh_rejects_unknown_token():
    with TestClient(app) as client:
        resp = client.post("/auth/refresh", json={"refreshToken": "aaa"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "E4010"


def test_monitoring_endpoints_require_auth():
    with TestClient(app) as client:
        for path in (
            "/monitoring/occupancy-map",
            "/monitoring/realtime-power",
            "/monitoring/savings",
            "/recommendations",
            "/notifications",
        ):
            assert client.get(path).status_code == 401, path


def test_occupancy_map_shape_matches_frontend():
    _seed()
    with TestClient(app) as client:
        body = client.get("/monitoring/occupancy-map", headers=_auth()).json()

    assert body["success"] is True and body["error"] is None
    spaces = body["data"]["spaces"]
    mine = [s for s in spaces if s["spaceId"] == make_space_id("cp-a", "f3", "room-500")]
    assert len(mine) == 1
    space = mine[0]
    # 프론트가 읽는 키가 모두 있어야 한다
    assert set(space) == {"spaceId", "code", "name", "floor", "occupied", "powerW", "wasteFlag"}
    assert space["floor"] == 3
    assert space["occupied"] is False
    assert space["wasteFlag"] is True  # 공실 + 300W


def test_realtime_power_counts_empty_rooms_as_waste():
    _seed()
    with TestClient(app) as client:
        data = client.get("/monitoring/realtime-power", headers=_auth()).json()["data"]
    assert data["totalW"] >= 300.0
    assert data["wasteW"] >= 300.0


def test_envelope_is_applied_everywhere():
    _seed()
    with TestClient(app) as client:
        for path in (
            "/monitoring/occupancy-map",
            "/monitoring/realtime-power",
            "/monitoring/savings",
            "/recommendations",
            "/notifications",
        ):
            body = client.get(path, headers=_auth()).json()
            assert set(body) == {"success", "data", "error"}, path
            assert body["success"] is True, path
