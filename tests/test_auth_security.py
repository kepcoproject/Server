"""
인증과 권한 분리 검증.

공개 배포를 전제로 강화한 부분이라, 약해지면 바로 드러나도록 못박아 둔다.
"""
import base64
import json
import time

from fastapi.testclient import TestClient

from app.api.compat_common import (
    decode_token,
    hash_password,
    issue_token,
    needs_rehash,
    verify_password,
)
from app.database import SessionLocal, init_db
from app.main import app
from app.models import AppUser


def setup_module(module):
    init_db()


def _login(client, login_id="demo", password="demo1234"):
    resp = client.post("/auth/login", json={"loginId": login_id, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ---------------------------------------------------------------------------
# 비밀번호
# ---------------------------------------------------------------------------
def test_password_hash_is_salted():
    """같은 비밀번호라도 저장값이 달라야 한다. 하나가 깨져도 나머지가 안전하다."""
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b
    assert a.startswith("pbkdf2$")
    assert verify_password("same-password", a)
    assert verify_password("same-password", b)
    assert not verify_password("wrong", a)


def test_legacy_hash_still_verifies_and_is_flagged():
    """기존 DB의 옛 해시로도 로그인은 되어야 하고, 갈아끼울 대상으로 표시되어야 한다."""
    import hashlib

    legacy = hashlib.sha256(b"smart-energy:oldpw").hexdigest()
    assert verify_password("oldpw", legacy)
    assert not verify_password("nope", legacy)
    assert needs_rehash(legacy) is True
    assert needs_rehash(hash_password("x")) is False


def test_login_upgrades_legacy_hash():
    db = SessionLocal()
    try:
        import hashlib

        db.add(
            AppUser(
                user_id="u-legacy",
                login_id="legacyuser",
                name="옛계정",
                role="MEMBER",
                status="ACTIVE",
                password_hash=hashlib.sha256(b"smart-energy:legacypw1").hexdigest(),
            )
        )
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        assert client.post(
            "/auth/login", json={"loginId": "legacyuser", "password": "legacypw1"}
        ).status_code == 200

    db = SessionLocal()
    try:
        user = db.get(AppUser, "u-legacy")
        # 로그인 성공 시 새 형식으로 바뀌어 있어야 한다
        assert user.password_hash.startswith("pbkdf2$")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 토큰
# ---------------------------------------------------------------------------
def test_token_round_trips_and_rejects_tampering():
    token = issue_token("access", "u-1")
    assert decode_token(token, "access") == "u-1"

    # 서명이 안 맞으면 거부
    body, sig = token.split(".")
    assert decode_token(f"{body}.{sig[:-2]}xx", "access") is None
    # 종류가 다르면 거부 (리프레시 토큰으로 API를 호출할 수 없다)
    assert decode_token(issue_token("refresh", "u-1"), "access") is None
    # 형식이 깨져도 예외 없이 None
    assert decode_token("쓰레기값", "access") is None
    assert decode_token("", "access") is None


def test_expired_token_is_rejected():
    """만료 시각을 과거로 만든 토큰은 서명이 맞아도 거부되어야 한다."""
    import hashlib
    import hmac as hmac_mod

    from app.api.compat_common import _TOKEN_KEY, _b64

    payload = {"u": "u-1", "k": "access", "exp": int(time.time()) - 10}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac_mod.new(_TOKEN_KEY, body.encode(), hashlib.sha256).digest())
    assert decode_token(f"{body}.{sig}", "access") is None


def test_forged_token_without_signature_is_rejected():
    """서명 키를 모르면 payload 를 지어내도 통과하지 못한다."""
    payload = {"u": "u-1", "k": "access", "exp": int(time.time()) + 3600}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    with TestClient(app) as client:
        resp = client.get("/auth/me", headers={"Authorization": f"Bearer {body}.aaaa"})
    assert resp.status_code == 401


def test_refresh_token_cannot_be_used_as_access_token():
    with TestClient(app) as client:
        tokens = _login(client)
        resp = client.get(
            "/auth/me", headers={"Authorization": f"Bearer {tokens['refreshToken']}"}
        )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 권한 분리
# ---------------------------------------------------------------------------
def _member_headers(client):
    """승인된 일반 사용자를 만들어 토큰을 받는다."""
    client.post(
        "/auth/signup",
        json={
            "loginId": "memberx",
            "password": "memberpw123",
            "name": "일반",
            "email": "member@example.com",
        },
    )
    db = SessionLocal()
    try:
        user = db.query(AppUser).filter_by(login_id="memberx").first()
        user.status = "ACTIVE"
        user.role = "MEMBER"
        db.commit()
    finally:
        db.close()
    tokens = _login(client, "memberx", "memberpw123")
    return {"Authorization": f"Bearer {tokens['accessToken']}"}


def test_member_can_read_but_not_write():
    with TestClient(app) as client:
        headers = _member_headers(client)

        # 조회는 된다
        for path in ("/spaces", "/devices", "/monitoring/occupancy-map", "/reports/savings"):
            assert client.get(path, headers=headers).status_code == 200, path

        # 쓰기는 전부 막힌다
        writes = [
            ("post", "/spaces", {"code": "X1", "name": "x", "building": "X", "floor": 1}),
            ("patch", "/spaces/sp-1", {"name": "바꿔보기"}),
            ("delete", "/spaces/sp-1", None),
            ("post", "/devices", {"deviceId": "HACK-1", "spaceId": "sp-1"}),
            ("post", "/control/commands", {"spaceId": "sp-1", "action": "LIGHT", "value": "OFF"}),
            ("post", "/recommendations/rec-1/apply", None),
            ("post", "/recommendations/rec-1/reject", {"comment": "x"}),
        ]
        for method, path, body in writes:
            call = getattr(client, method)
            resp = call(path, headers=headers, json=body) if body is not None else call(
                path, headers=headers
            )
            assert resp.status_code == 403, f"{method.upper()} {path} 가 막히지 않음"
            assert resp.json()["error"]["code"] == "E4030"


def test_member_cannot_see_user_list():
    with TestClient(app) as client:
        headers = _member_headers(client)
        assert client.get("/users", headers=headers).status_code == 403


def test_admin_can_write():
    with TestClient(app) as client:
        tokens = _login(client)
        headers = {"Authorization": f"Bearer {tokens['accessToken']}"}
        created = client.post(
            "/spaces",
            headers=headers,
            json={"code": "ADM1", "name": "관리자생성", "building": "A동", "floor": 2},
        )
        assert created.status_code == 200
        space_id = created.json()["data"]["spaceId"]
        assert client.delete(f"/spaces/{space_id}", headers=headers).status_code == 200
