"""프론트엔드 호환 레이어 검증."""
import json

from fastapi.testclient import TestClient

from app.database import SessionLocal, init_db
from app.main import app
from app.mqtt_handlers import handle_data_message

BASE_TS = 1790200000


def setup_module(module):
    init_db()


def _seed(building="cp-a", floor="f3", room="room-500", device_id="CP-NODE-500", occupancy=False, power=300.0):
    handle_data_message(
        f"v1/{building}/{floor}/{room}/data",
        json.dumps(
            {
                "device_id": device_id,
                "timestamp": BASE_TS,
                "metrics": {"occupancy": occupancy, "power": power, "temp": 24.0},
            }
        ).encode(),
    )


def _login(client):
    resp = client.post("/auth/login", json={"loginId": "demo", "password": "demo1234"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['data']['accessToken']}"}


def _first_space_id(client, headers):
    items = client.get("/spaces", headers=headers).json()["data"]["items"]
    assert items, "공간이 하나도 없습니다"
    return items[0]["spaceId"]


# ---------------------------------------------------------------------------
# 인증
# ---------------------------------------------------------------------------
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
        data = client.post(
            "/auth/login", json={"loginId": "demo", "password": "demo1234"}
        ).json()["data"]
    assert data["accessToken"] and data["refreshToken"]
    assert data["user"]["role"] == "ADMIN"


def test_me_requires_token():
    with TestClient(app) as client:
        assert client.get("/auth/me").status_code == 401
        headers = _login(client)
        assert client.get("/auth/me", headers=headers).json()["data"]["loginId"] == "demo"


def test_refresh_round_trips():
    with TestClient(app) as client:
        tokens = client.post(
            "/auth/login", json={"loginId": "demo", "password": "demo1234"}
        ).json()["data"]
        resp = client.post("/auth/refresh", json={"refreshToken": tokens["refreshToken"]})
        assert resp.status_code == 200
        assert resp.json()["data"]["accessToken"]

        bad = client.post("/auth/refresh", json={"refreshToken": "aaa"})
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "E4010"


def test_signup_creates_pending_user_and_cannot_login():
    with TestClient(app) as client:
        resp = client.post(
            "/auth/signup",
            json={
                "loginId": "newbie",
                "password": "pw12345678",
                "name": "신규",
                "email": "n@x.io",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "PENDING"

        # 승인 전에는 로그인이 막혀야 한다
        blocked = client.post("/auth/login", json={"loginId": "newbie", "password": "pw12345678"})
        assert blocked.status_code == 403
        assert blocked.json()["error"]["code"] == "E4030"

        dup = client.post(
            "/auth/signup",
            json={
                "loginId": "newbie",
                "password": "pw12345678",
                "name": "신규",
                "email": "n@x.io",
            },
        )
        assert dup.status_code == 409


def test_check_id_reports_availability():
    with TestClient(app) as client:
        assert client.get("/auth/check-id", params={"loginId": "demo"}).json()["data"][
            "available"
        ] is False
        assert client.get("/auth/check-id", params={"loginId": "totally-free"}).json()["data"][
            "available"
        ] is True


# ---------------------------------------------------------------------------
# 대시보드
# ---------------------------------------------------------------------------
def test_all_endpoints_require_auth():
    with TestClient(app) as client:
        for path in (
            "/monitoring/occupancy-map",
            "/monitoring/realtime-power",
            "/monitoring/savings",
            "/recommendations",
            "/notifications",
            "/spaces",
            "/devices",
            "/control/logs",
            "/reports/savings",
            "/users",
        ):
            assert client.get(path).status_code == 401, path


def test_occupancy_map_shape_matches_frontend():
    _seed()
    with TestClient(app) as client:
        headers = _login(client)
        body = client.get("/monitoring/occupancy-map", headers=headers).json()

    assert body["success"] is True and body["error"] is None
    spaces = body["data"]["spaces"]
    mine = [s for s in spaces if s["code"] == "ROOM-500"]
    assert len(mine) == 1
    # 프론트가 읽는 키가 모두 있어야 한다
    assert set(mine[0]) == {
        "spaceId", "code", "name", "floor", "occupied", "powerW", "wasteFlag"
    }
    assert mine[0]["floor"] == 3
    assert mine[0]["wasteFlag"] is True  # 공실 + 300W


def test_realtime_power_counts_empty_rooms_as_waste():
    _seed()
    with TestClient(app) as client:
        data = client.get("/monitoring/realtime-power", headers=_login(client)).json()["data"]
    assert data["totalW"] >= 300.0
    assert data["wasteW"] >= 300.0


def test_envelope_is_applied_everywhere():
    _seed()
    with TestClient(app) as client:
        headers = _login(client)
        for path in (
            "/monitoring/occupancy-map",
            "/monitoring/realtime-power",
            "/monitoring/savings",
            "/recommendations",
            "/notifications",
            "/spaces",
            "/devices",
            "/control/logs",
            "/reports/savings",
            "/users",
        ):
            body = client.get(path, headers=headers).json()
            assert set(body) == {"success", "data", "error"}, path
            assert body["success"] is True, path


# ---------------------------------------------------------------------------
# 공간
# ---------------------------------------------------------------------------
def test_spaces_are_created_from_sensor_data():
    _seed(building="auto-b", floor="f7", room="room-700", device_id="AUTO-NODE-700")
    with TestClient(app) as client:
        items = client.get("/spaces", headers=_login(client)).json()["data"]["items"]
    codes = {s["code"] for s in items}
    assert "ROOM-700" in codes, "측정값이 들어온 공간이 자동 등록되어야 한다"


def test_space_crud_round_trip():
    with TestClient(app) as client:
        headers = _login(client)
        created = client.post(
            "/spaces",
            headers=headers,
            json={"code": "Z101", "name": "테스트실", "building": "Z동", "floor": 1,
                  "ratedPowerW": 500},
        ).json()["data"]
        assert created["code"] == "Z101"
        space_id = created["spaceId"]

        updated = client.patch(
            f"/spaces/{space_id}", headers=headers, json={"name": "이름변경", "ratedPowerW": 900}
        ).json()["data"]
        assert updated["name"] == "이름변경" and updated["ratedPowerW"] == 900

        assert client.get(f"/spaces/{space_id}", headers=headers).status_code == 200
        assert client.delete(f"/spaces/{space_id}", headers=headers).status_code == 200
        assert client.get(f"/spaces/{space_id}", headers=headers).status_code == 404


def test_space_with_nodes_cannot_be_deleted():
    _seed(building="keep-c", floor="f2", room="room-800", device_id="KEEP-NODE-800")
    with TestClient(app) as client:
        headers = _login(client)
        items = client.get("/spaces", headers=headers).json()["data"]["items"]
        target = next(s for s in items if s["code"] == "ROOM-800")
        resp = client.delete(f"/spaces/{target['spaceId']}", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "E4091"


# ---------------------------------------------------------------------------
# 디바이스 · 제어 · 리포트
# ---------------------------------------------------------------------------
def test_device_registration_returns_api_key_once():
    _seed(building="dev-d", floor="f1", room="room-900", device_id="DEV-NODE-900")
    with TestClient(app) as client:
        headers = _login(client)
        space_id = next(
            s["spaceId"]
            for s in client.get("/spaces", headers=headers).json()["data"]["items"]
            if s["code"] == "ROOM-900"
        )
        created = client.post(
            "/devices",
            headers=headers,
            json={"deviceId": "NEW-NODE-1", "spaceId": space_id,
                  "sensors": ["PIR", "CURRENT"], "actuators": ["LIGHT"]},
        ).json()["data"]
        assert created["deviceApiKey"].startswith("dvk_")
        assert created["sensors"] == ["PIR", "CURRENT"]

        # 이후 조회에는 절대 나오면 안 된다
        detail = client.get("/devices/NEW-NODE-1", headers=headers).json()["data"]
    assert "deviceApiKey" not in detail


def test_control_requires_an_online_node():
    """노드가 붙어 있는 공간은 접수(202), 노드가 없는 공간은 거절(503)이어야 한다."""
    _seed(building="ctl-e", floor="f1", room="room-950", device_id="CTL-NODE-950")
    with TestClient(app) as client:
        headers = _login(client)

        # 방금 데이터를 보낸 노드가 있는 공간 -> 접수만 하고 202
        with_node = next(
            s["spaceId"]
            for s in client.get("/spaces", headers=headers).json()["data"]["items"]
            if s["code"] == "ROOM-950"
        )
        accepted = client.post(
            "/control/commands",
            headers=headers,
            json={"spaceId": with_node, "action": "LIGHT", "value": "OFF"},
        )
        assert accepted.status_code == 202
        body = accepted.json()["data"]
        # 202는 '접수했다'이지 '실행했다'가 아니다
        assert body["status"] == "PENDING" and body["commandId"]

        # 폴링하면 아직 PENDING (노드가 가져가야 완료된다)
        polled = client.get(f"/control/commands/{body['commandId']}", headers=headers)
        assert polled.json()["data"]["status"] == "PENDING"

        # 노드가 하나도 없는 공간 -> 503
        empty = client.post(
            "/spaces",
            headers=headers,
            json={"code": "EMPTY1", "name": "노드없음", "building": "E동", "floor": 9},
        ).json()["data"]
        refused = client.post(
            "/control/commands",
            headers=headers,
            json={"spaceId": empty["spaceId"], "action": "LIGHT", "value": "OFF"},
        )
    assert refused.status_code == 503
    assert refused.json()["error"]["code"] == "E5030"


def test_power_history_downsamples_and_marks_gaps():
    _seed()
    with TestClient(app) as client:
        headers = _login(client)
        space_id = next(
            s["spaceId"]
            for s in client.get("/spaces", headers=headers).json()["data"]["items"]
            if s["code"] == "ROOM-500"
        )
        body = client.get(
            f"/telemetry/power-history/{space_id}",
            headers=headers,
            params={"hours": 1, "interval": "10m"},
        ).json()["data"]

    assert "points" in body and "occupiedPeriods" in body
    assert all(set(p) == {"t", "powerW"} for p in body["points"])
    # 값이 없는 구간은 null 이어야 프론트가 선을 끊어 그린다
    assert any(p["powerW"] is None for p in body["points"])


def test_report_has_summary_and_buckets():
    _seed()
    with TestClient(app) as client:
        body = client.get(
            "/reports/savings", headers=_login(client), params={"period": "day"}
        ).json()["data"]
    assert set(body["summary"]) == {
        "totalSavingWh", "savingCost", "co2ReductionKg", "savingRate"
    }
    assert len(body["items"]) == 14
    assert all(set(i) == {"date", "usageWh", "baselineWh"} for i in body["items"])


def test_user_list_and_approval():
    with TestClient(app) as client:
        headers = _login(client)
        client.post(
            "/auth/signup",
            json={
                "loginId": "pendinguser",
                "password": "pw12345678",
                "name": "대기",
                "email": "pending@example.com",
            },
        )

        items = client.get("/users", headers=headers).json()["data"]["items"]
        target = next(u for u in items if u["loginId"] == "pendinguser")
        assert target["status"] == "PENDING"

        updated = client.patch(
            f"/users/{target['userId']}", headers=headers, json={"status": "ACTIVE"}
        ).json()["data"]
        assert updated["status"] == "ACTIVE"

        # 승인 후에는 로그인이 된다
        assert client.post(
            "/auth/login", json={"loginId": "pendinguser", "password": "pw12345678"}
        ).status_code == 200


def test_admin_cannot_demote_self():
    with TestClient(app) as client:
        headers = _login(client)
        me = client.get("/auth/me", headers=headers).json()["data"]
        resp = client.patch(f"/users/{me['id']}", headers=headers, json={"role": "MEMBER"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "E4003"


def test_signup_requires_every_field():
    """
    화면에서도 막지만 서버가 다시 봐야 한다. API 를 직접 부르면 화면 검증을 건너뛴다.
    프론트(Signup.jsx)가 보내는 다섯 필드를 모두 필수로 본다.
    """
    ok_body = {
        "loginId": "validuser",
        "password": "password123",
        "passwordConfirm": "password123",
        "name": "홍길동",
        "email": "hong@example.com",
    }

    cases = [
        ({"loginId": ""}, "아이디 없음"),
        ({"loginId": "ab"}, "아이디가 너무 짧음"),
        ({"loginId": "한글아이디"}, "아이디에 허용되지 않는 문자"),
        ({"name": ""}, "이름 없음"),
        ({"email": ""}, "이메일 없음"),
        ({"email": "not-an-email"}, "이메일 형식 오류"),
        ({"password": ""}, "비밀번호 없음"),
        ({"password": "short1", "passwordConfirm": "short1"}, "비밀번호가 너무 짧음"),
        ({"passwordConfirm": "different123"}, "비밀번호 확인 불일치"),
    ]

    with TestClient(app) as client:
        for override, label in cases:
            body = {**ok_body, **override}
            resp = client.post("/auth/signup", json=body)
            assert resp.status_code == 400, f"{label}: 통과되면 안 된다"
            assert resp.json()["error"]["code"] == "E4000", label

        # 다 채우면 통과한다
        resp = client.post("/auth/signup", json=ok_body)
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "PENDING"


def test_signup_stores_name_and_email_as_given():
    """예전에는 이름이 비면 아이디로 대신 채웠다. 이제는 받은 값을 그대로 쓴다."""
    with TestClient(app) as client:
        client.post(
            "/auth/signup",
            json={
                "loginId": "storeduser",
                "password": "password123",
                "passwordConfirm": "password123",
                "name": "김철수",
                "email": "chulsoo@example.com",
            },
        )
        headers = _login(client)
        items = client.get("/users", headers=headers).json()["data"]["items"]

    target = next(u for u in items if u["loginId"] == "storeduser")
    assert target["name"] == "김철수"
    assert target["email"] == "chulsoo@example.com"
