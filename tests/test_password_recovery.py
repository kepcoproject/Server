"""
이메일 인증과 비밀번호 재설정.

두 기능은 한 쌍이다. 비밀번호를 잊으면 메일로 되찾는데, 그 주소가 본인 것이
아니면 남이 계정을 가져간다. 그래서 재설정은 인증이 끝난 주소로만 보낸다.
"""
import logging
import re
from datetime import timedelta
from io import StringIO

from fastapi.testclient import TestClient

from app.api.compat_common import RESET_PASSWORD, VERIFY_EMAIL, issue_auth_token
from app.database import SessionLocal, init_db
from app.main import app
from app.models import AppUser, AuthToken
from app.utils import utcnow

LINK = re.compile(r"/(verify-email|reset-password)\?token=([\w\-]+)")


def setup_module(module):
    init_db()


class MailCapture:
    """SMTP 가 없으면 메일러가 내용을 로그로 남긴다. 그걸 붙잡아 링크를 꺼낸다."""

    def __enter__(self):
        self.buffer = StringIO()
        self.handler = logging.StreamHandler(self.buffer)
        self.handler.setLevel(logging.WARNING)
        logging.getLogger("smart_energy.mailer").addHandler(self.handler)
        return self

    def __exit__(self, *exc):
        logging.getLogger("smart_energy.mailer").removeHandler(self.handler)

    def tokens(self, kind):
        return [t for k, t in LINK.findall(self.buffer.getvalue()) if k == kind]


def _signup(client, login_id, email, password="password123"):
    return client.post(
        "/auth/signup",
        json={
            "loginId": login_id,
            "password": password,
            "passwordConfirm": password,
            "name": "테스터",
            "email": email,
        },
    )


def test_signup_sends_verification_and_marks_unverified():
    with MailCapture() as mail, TestClient(app) as client:
        resp = _signup(client, "verifyme", "verifyme@example.com")
        assert resp.status_code == 200
        assert resp.json()["data"]["emailVerified"] is False

        tokens = mail.tokens("verify-email")
        assert len(tokens) == 1

        done = client.post("/auth/email/verify", json={"token": tokens[0]})
        assert done.status_code == 200
        assert done.json()["data"]["verified"] is True

    db = SessionLocal()
    try:
        user = db.query(AppUser).filter_by(login_id="verifyme").first()
        assert user.email_verified is True
    finally:
        db.close()


def test_verification_link_works_only_once():
    with MailCapture() as mail, TestClient(app) as client:
        _signup(client, "onceonly", "onceonly@example.com")
        token = mail.tokens("verify-email")[-1]

        assert client.post("/auth/email/verify", json={"token": token}).status_code == 200
        again = client.post("/auth/email/verify", json={"token": token})
        assert again.status_code == 400
        assert "만료" in again.json()["error"]["message"]


def test_reset_is_refused_for_unverified_email():
    """
    아무 주소나 적어두고 그 주소로 재설정 링크를 받을 수 있으면
    이메일을 확인하는 의미가 없다.
    """
    with MailCapture() as mail, TestClient(app) as client:
        _signup(client, "unverified", "unverified@example.com")
        mail.buffer.truncate(0)
        mail.buffer.seek(0)

        resp = client.post("/auth/password/forgot", json={"email": "unverified@example.com"})
        # 응답은 성공처럼 보이지만 실제로는 보내지 않는다
        assert resp.status_code == 200
        assert mail.tokens("reset-password") == []


def test_reset_changes_password_and_burns_the_link():
    with MailCapture() as mail, TestClient(app) as client:
        _signup(client, "resetme", "resetme@example.com")
        client.post("/auth/email/verify", json={"token": mail.tokens("verify-email")[-1]})

        # 대소문자가 달라도 같은 주소로 본다
        client.post("/auth/password/forgot", json={"email": "ResetMe@Example.COM"})
        token = mail.tokens("reset-password")[-1]

        short = client.post(
            "/auth/password/reset", json={"token": token, "newPassword": "123"}
        )
        assert short.status_code == 400
        # 형식 오류로 토큰을 태우면 링크를 다시 받아야 한다. 아직 살아 있어야 한다.
        assert client.post(
            "/auth/password/reset", json={"token": token, "newPassword": "brandnew123"}
        ).status_code == 200

        # 한 번 쓴 링크는 죽는다
        assert client.post(
            "/auth/password/reset", json={"token": token, "newPassword": "another12345"}
        ).status_code == 400

        # 관리자가 승인하면 새 비밀번호로만 들어갈 수 있다
        admin = client.post(
            "/auth/login", json={"loginId": "demo", "password": "demo1234"}
        ).json()["data"]["accessToken"]
        headers = {"Authorization": f"Bearer {admin}"}
        row = next(
            u
            for u in client.get("/users", headers=headers).json()["data"]["items"]
            if u["loginId"] == "resetme"
        )
        client.patch(f"/users/{row['userId']}", headers=headers, json={"status": "ACTIVE"})

        assert client.post(
            "/auth/login", json={"loginId": "resetme", "password": "password123"}
        ).status_code == 401
        assert client.post(
            "/auth/login", json={"loginId": "resetme", "password": "brandnew123"}
        ).status_code == 200


def test_forgot_answers_the_same_for_unknown_address():
    """다르게 답하면 어떤 주소가 가입돼 있는지 알아내는 데 쓰인다."""
    with TestClient(app) as client:
        known = client.post("/auth/password/forgot", json={"email": "resetme@example.com"})
        unknown = client.post("/auth/password/forgot", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()


def test_expired_token_is_refused():
    with TestClient(app) as client:
        _signup(client, "expired", "expired@example.com")

        db = SessionLocal()
        try:
            user = db.query(AppUser).filter_by(login_id="expired").first()
            token = issue_auth_token(db, user, VERIFY_EMAIL, timedelta(hours=1))
            # 시계를 되돌리는 대신 만료 시각을 과거로 옮긴다
            row = db.query(AuthToken).filter_by(user_id=user.user_id).first()
            row.expires_at = utcnow() - timedelta(minutes=1)
            db.commit()
        finally:
            db.close()

        assert client.post("/auth/email/verify", json={"token": token}).status_code == 400


def test_token_is_not_stored_in_plain_text():
    """DB 를 볼 수 있는 사람이 남의 재설정 링크를 만들어낼 수 없어야 한다."""
    with TestClient(app) as client:
        _signup(client, "hashonly", "hashonly@example.com")

    db = SessionLocal()
    try:
        user = db.query(AppUser).filter_by(login_id="hashonly").first()
        token = issue_auth_token(db, user, RESET_PASSWORD, timedelta(minutes=30))
        rows = db.query(AuthToken).filter_by(user_id=user.user_id).all()
        stored = {r.token_hash for r in rows}
        assert token not in stored
        assert all(len(h) == 64 for h in stored)  # sha256 hex
    finally:
        db.close()


def test_reset_kills_sessions_that_were_open_before():
    """
    계정을 빼앗겼을 때 비밀번호를 되찾는 게 요점이다. 공격자가 들고 있던
    리프레시 토큰이 14일 동안 그대로 듣는다면 되찾은 게 아니다.
    """
    with MailCapture() as mail, TestClient(app) as client:
        _signup(client, "sessionkill", "sessionkill@example.com")
        client.post("/auth/email/verify", json={"token": mail.tokens("verify-email")[-1]})

        db = SessionLocal()
        try:
            user = db.query(AppUser).filter_by(login_id="sessionkill").first()
            user.status = "ACTIVE"
            db.commit()
        finally:
            db.close()

        stolen = client.post(
            "/auth/login", json={"loginId": "sessionkill", "password": "password123"}
        ).json()["data"]
        headers = {"Authorization": f"Bearer {stolen['accessToken']}"}
        assert client.get("/auth/me", headers=headers).status_code == 200

        # 주인이 비밀번호를 되찾는다
        client.post("/auth/password/forgot", json={"email": "sessionkill@example.com"})
        client.post(
            "/auth/password/reset",
            json={
                "token": mail.tokens("reset-password")[-1],
                "newPassword": "recovered1234",
            },
        )

        # 들고 있던 토큰은 그 자리에서 죽어야 한다
        assert client.get("/auth/me", headers=headers).status_code == 401
        assert client.post(
            "/auth/refresh", json={"refreshToken": stolen["refreshToken"]}
        ).status_code == 401

        # 주인은 새 비밀번호로 들어간다
        assert client.post(
            "/auth/login", json={"loginId": "sessionkill", "password": "recovered1234"}
        ).status_code == 200


def test_password_change_also_kills_old_sessions():
    """설정 화면에서 비밀번호를 바꿔도 마찬가지여야 한다."""
    with MailCapture() as mail, TestClient(app) as client:
        _signup(client, "changekill", "changekill@example.com")
        client.post("/auth/email/verify", json={"token": mail.tokens("verify-email")[-1]})

        db = SessionLocal()
        try:
            user = db.query(AppUser).filter_by(login_id="changekill").first()
            user.status = "ACTIVE"
            db.commit()
        finally:
            db.close()

        session = client.post(
            "/auth/login", json={"loginId": "changekill", "password": "password123"}
        ).json()["data"]
        headers = {"Authorization": f"Bearer {session['accessToken']}"}

        changed = client.patch(
            "/auth/password",
            headers=headers,
            json={"currentPassword": "password123", "newPassword": "changed12345"},
        )
        assert changed.status_code == 200

        # 예전 토큰은 죽는다 — 다른 기기에 열려 있던 세션이 끊긴다
        assert client.get("/auth/me", headers=headers).status_code == 401

        # 방금 스스로 바꾼 사람은 함께 받은 새 토큰으로 계속 쓸 수 있어야 한다.
        # 이게 없으면 비밀번호를 바꾼 직후 영문 모를 로그아웃이 된다.
        fresh = changed.json()["data"]
        assert client.get(
            "/auth/me", headers={"Authorization": f"Bearer {fresh['accessToken']}"}
        ).status_code == 200
        assert client.post(
            "/auth/refresh", json={"refreshToken": fresh["refreshToken"]}
        ).status_code == 200


def test_auth_endpoints_reject_non_string_values():
    """
    인증이 없는 경로들이라 아무나 500 을 만들 수 있었다. 500 에는 봉투가 없어
    화면이 사유를 읽지도 못한다.
    """
    with TestClient(app) as client:
        cases = [
            ("/auth/login", {"loginId": "demo", "password": 123}),
            ("/auth/signup", {"loginId": 123, "password": "password123"}),
            ("/auth/signup", {"loginId": "ok", "password": "password123", "name": ["x"]}),
            ("/auth/signup", {"loginId": "ok", "password": "pw12345678", "email": {"a": 1}}),
            ("/auth/email/verify", {"token": 123}),
            ("/auth/email/verify", {"token": {"a": 1}}),
            ("/auth/email/resend", {"email": 123}),
            ("/auth/password/forgot", {"email": 123}),
            ("/auth/password/forgot", {"email": ["a"]}),
            ("/auth/password/reset", {"token": "x", "newPassword": 123}),
        ]
        for path, body in cases:
            resp = client.post(path, json=body)
            assert resp.status_code == 400, f"{path} {body} -> {resp.status_code}"
            assert resp.json()["error"]["code"] == "E4000"
