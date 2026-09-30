"""
회원가입 이메일 인증(인증번호)과 비밀번호 재설정.

두 기능은 한 쌍이다. 비밀번호를 잊으면 메일로 되찾는데, 그 주소가 본인 것이
아니면 남이 계정을 가져간다. 그래서 가입할 때 메일로 보낸 인증번호를 맞혀야
하고, 재설정은 인증이 끝난 주소로만 보낸다.
"""
import hashlib
import logging
import re
from datetime import timedelta
from io import StringIO

from fastapi.testclient import TestClient

from app import mailer
from app.api import compat_routes
from app.api.compat_common import (
    EMAIL_TICKET,
    RESET_PASSWORD,
    hash_password,
    issue_auth_token,
    issue_email_ticket,
    issue_token,
)
from app.database import SessionLocal, init_db
from app.main import app
from app.models import AppUser, AuthToken, EmailCode
from app.utils import utcnow

LINK = re.compile(r"/(reset-password)\?token=([\w\-]+)")
CODE = re.compile(r"인증번호 (\d{6})")

SEND = "/auth/email/code"
CHECK = "/auth/email/code/verify"


def setup_module(module):
    init_db()


class MailCapture:
    """SMTP 가 없으면 메일러가 내용을 로그로 남긴다. 그걸 붙잡아 링크와 번호를 꺼낸다."""

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

    def codes(self):
        return CODE.findall(self.buffer.getvalue())


def _signup(client, login_id, email, password="password123"):
    """인증번호 흐름은 아래에서 따로 본다. 여기서는 증표를 바로 만들어 쓴다."""
    return client.post(
        "/auth/signup",
        json={
            "loginId": login_id,
            "password": password,
            "passwordConfirm": password,
            "name": "테스터",
            "email": email,
            "emailToken": issue_email_ticket(email),
        },
    )


def _age_codes(email, seconds):
    """재전송 대기를 기다리는 대신 보낸 시각을 과거로 옮긴다."""
    db = SessionLocal()
    try:
        for row in db.query(EmailCode).filter_by(email=email.lower()).all():
            row.created_at = row.created_at - timedelta(seconds=seconds)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 회원가입 인증번호
# ---------------------------------------------------------------------------
def test_signup_with_email_code_creates_verified_user():
    with MailCapture() as mail, TestClient(app) as client:
        sent = client.post(SEND, json={"email": "Coder@Example.com"})
        assert sent.status_code == 200
        assert sent.json()["data"]["expiresIn"] == 600
        [code] = mail.codes()

        # 메일에서 복사하면 공백이 섞여 오기도 한다. 주소의 대소문자는 가리지 않는다.
        checked = client.post(
            CHECK, json={"email": "coder@example.com", "code": f" {code[:3]} {code[3:]} "}
        )
        assert checked.status_code == 200, checked.text
        ticket = checked.json()["data"]["emailToken"]

        resp = client.post(
            "/auth/signup",
            json={
                "loginId": "coder",
                "password": "password123",
                "passwordConfirm": "password123",
                "name": "코더",
                "email": "Coder@Example.com",
                "emailToken": ticket,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["emailVerified"] is True

        # 맞힌 번호는 버린다. 같은 번호로 증표를 또 받을 수 없다.
        again = client.post(CHECK, json={"email": "coder@example.com", "code": code})
        assert again.status_code == 400

    db = SessionLocal()
    try:
        user = db.query(AppUser).filter_by(login_id="coder").first()
        assert user.email_verified is True
        # 저장은 입력한 그대로 둔다
        assert user.email == "Coder@Example.com"
    finally:
        db.close()


def test_signup_without_verified_email_is_refused():
    body = {
        "loginId": "skipper",
        "password": "password123",
        "passwordConfirm": "password123",
        "name": "건너뛰기",
        "email": "skipper@example.com",
    }
    with TestClient(app) as client:
        access = client.post(
            "/auth/login", json={"loginId": "demo", "password": "demo1234"}
        ).json()["data"]["accessToken"]

        cases = [
            (None, "인증을 안 함"),
            ("garbage", "위조"),
            (issue_email_ticket("someone-else@example.com"), "남의 주소로 받은 증표"),
            (access, "다른 종류의 토큰"),
            (
                issue_token(EMAIL_TICKET, "skipper@example.com", ttl=timedelta(seconds=-5)),
                "시간이 지난 증표",
            ),
        ]
        for token, label in cases:
            payload = dict(body) if token is None else {**body, "emailToken": token}
            resp = client.post("/auth/signup", json=payload)
            assert resp.status_code == 400, label
            assert resp.json()["error"]["code"] == "E4004", label

    db = SessionLocal()
    try:
        assert db.query(AppUser).filter_by(login_id="skipper").first() is None
    finally:
        db.close()


def test_wrong_code_is_limited(monkeypatch):
    """여섯 자리뿐이라 몇 번이고 넣어 볼 수 있으면 금방 맞힌다."""
    monkeypatch.setattr(compat_routes.secrets, "randbelow", lambda n: 123456)
    with TestClient(app) as client:
        assert client.post(SEND, json={"email": "guess@example.com"}).status_code == 200

        for left in (4, 3, 2, 1):
            resp = client.post(CHECK, json={"email": "guess@example.com", "code": "000000"})
            assert resp.status_code == 400
            assert f"남은 횟수 {left}번" in resp.json()["error"]["message"]

        last = client.post(CHECK, json={"email": "guess@example.com", "code": "000000"})
        assert "입력 횟수" in last.json()["error"]["message"]

        # 이제는 맞는 번호를 넣어도 받지 않는다
        right = client.post(CHECK, json={"email": "guess@example.com", "code": "123456"})
        assert right.status_code == 400
        assert "입력 횟수" in right.json()["error"]["message"]


def test_new_code_replaces_the_old_one(monkeypatch):
    codes = iter([111111, 222222])
    monkeypatch.setattr(compat_routes.secrets, "randbelow", lambda n: next(codes))
    with TestClient(app) as client:
        assert client.post(SEND, json={"email": "twice@example.com"}).status_code == 200
        _age_codes("twice@example.com", 61)
        assert client.post(SEND, json={"email": "twice@example.com"}).status_code == 200

        old = client.post(CHECK, json={"email": "twice@example.com", "code": "111111"})
        assert old.status_code == 400
        new = client.post(CHECK, json={"email": "twice@example.com", "code": "222222"})
        assert new.status_code == 200


def test_resend_is_throttled():
    """남의 주소로 메일을 퍼붓는 데 쓰이면 안 된다."""
    with TestClient(app) as client:
        assert client.post(SEND, json={"email": "spam@example.com"}).status_code == 200

        # 바로 다시 누르면 기다리라고 한다
        soon = client.post(SEND, json={"email": "spam@example.com"})
        assert soon.status_code == 429
        assert soon.json()["error"]["code"] == "E4290"
        assert "초 뒤에" in soon.json()["error"]["message"]

        # 간격을 지켜도 1시간에 다섯 번까지만
        for _ in range(4):
            _age_codes("spam@example.com", 61)
            assert client.post(SEND, json={"email": "spam@example.com"}).status_code == 200
        _age_codes("spam@example.com", 61)
        capped = client.post(SEND, json={"email": "spam@example.com"})
        assert capped.status_code == 429
        assert "1시간" in capped.json()["error"]["message"]


def test_code_expires():
    with MailCapture() as mail, TestClient(app) as client:
        client.post(SEND, json={"email": "late@example.com"})
        [code] = mail.codes()

        db = SessionLocal()
        try:
            row = db.query(EmailCode).filter_by(email="late@example.com").first()
            row.expires_at = utcnow() - timedelta(minutes=1)
            db.commit()
        finally:
            db.close()

        resp = client.post(CHECK, json={"email": "late@example.com", "code": code})
        assert resp.status_code == 400
        assert "만료" in resp.json()["error"]["message"]


def test_code_is_refused_for_registered_email():
    with TestClient(app) as client:
        assert _signup(client, "taken", "taken@example.com").status_code == 200
        resp = client.post(SEND, json={"email": "Taken@Example.com"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "E4090"


def test_code_is_not_stored_in_plain_text():
    """여섯 자리 숫자는 해시만 남기면 백만 번 대입해 풀린다. 서버 키를 섞어야 한다."""
    with MailCapture() as mail, TestClient(app) as client:
        client.post(SEND, json={"email": "hashcode@example.com"})
        [code] = mail.codes()

    db = SessionLocal()
    try:
        row = db.query(EmailCode).filter_by(email="hashcode@example.com").first()
        assert code not in row.code_hash
        assert row.code_hash != hashlib.sha256(code.encode()).hexdigest()
    finally:
        db.close()


def test_mail_failure_is_reported_and_does_not_block_retry(monkeypatch):
    """번호를 못 받으면 가입을 못 한다. 조용히 넘기면 사용자는 영문도 모르고 기다린다."""
    monkeypatch.setattr(mailer, "is_configured", lambda: True)
    monkeypatch.setattr(mailer, "_send", lambda *args, **kwargs: False)
    with TestClient(app) as client:
        failed = client.post(SEND, json={"email": "down@example.com"})
        assert failed.status_code == 503
        assert failed.json()["error"]["code"] == "E5030"

        # 메일 서버가 돌아오면 바로 다시 받을 수 있어야 한다 (재전송 대기 없음)
        monkeypatch.undo()
        assert client.post(SEND, json={"email": "down@example.com"}).status_code == 200


# ---------------------------------------------------------------------------
# 비밀번호 재설정
# ---------------------------------------------------------------------------
def test_reset_is_refused_for_unverified_email():
    """
    아무 주소나 적어두고 그 주소로 재설정 링크를 받을 수 있으면
    이메일을 확인하는 의미가 없다. 지금은 가입할 때 인증을 거치지만,
    그 전에 만들어진 계정은 인증되지 않은 채 남아 있을 수 있다.
    """
    db = SessionLocal()
    try:
        db.add(
            AppUser(
                user_id="u-legacy01",
                login_id="unverified",
                name="예전 계정",
                email="unverified@example.com",
                role="MEMBER",
                status="ACTIVE",
                password_hash=hash_password("password123"),
                email_verified=False,
            )
        )
        db.commit()
    finally:
        db.close()

    with MailCapture() as mail, TestClient(app) as client:
        resp = client.post("/auth/password/forgot", json={"email": "unverified@example.com"})
        # 응답은 성공처럼 보이지만 실제로는 보내지 않는다
        assert resp.status_code == 200
        assert mail.tokens("reset-password") == []


def test_reset_changes_password_and_burns_the_link():
    with MailCapture() as mail, TestClient(app) as client:
        _signup(client, "resetme", "resetme@example.com")

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


def test_expired_reset_link_is_refused():
    with TestClient(app) as client:
        _signup(client, "expired", "expired@example.com")

        db = SessionLocal()
        try:
            user = db.query(AppUser).filter_by(login_id="expired").first()
            token = issue_auth_token(db, user, RESET_PASSWORD, timedelta(minutes=30))
            # 시계를 되돌리는 대신 만료 시각을 과거로 옮긴다
            row = db.query(AuthToken).filter_by(user_id=user.user_id).first()
            row.expires_at = utcnow() - timedelta(minutes=1)
            db.commit()
        finally:
            db.close()

        resp = client.post(
            "/auth/password/reset", json={"token": token, "newPassword": "brandnew123"}
        )
        assert resp.status_code == 400


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
    with TestClient(app) as client:
        _signup(client, "changekill", "changekill@example.com")

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
            ("/auth/signup", {"loginId": "okay", "password": "pw12345678", "emailToken": 1}),
            (SEND, {"email": 123}),
            (SEND, {"email": ["a"]}),
            (CHECK, {"email": "a@example.com", "code": 123456}),
            (CHECK, {"email": {"a": 1}, "code": "123456"}),
            ("/auth/password/forgot", {"email": 123}),
            ("/auth/password/forgot", {"email": ["a"]}),
            ("/auth/password/reset", {"token": "x", "newPassword": 123}),
        ]
        for path, body in cases:
            resp = client.post(path, json=body)
            assert resp.status_code == 400, f"{path} {body} -> {resp.status_code}"
            assert resp.json()["error"]["code"] == "E4000"
