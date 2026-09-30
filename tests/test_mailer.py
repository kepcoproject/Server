"""
메일 발송 (SMTP 설정이 있을 때).

다른 테스트는 SMTP 가 없어 로그로 떨어지는 경로만 지나간다. 실제 Gmail 로
보낼 때만 타는 부분 — 로그인, 보내는 주소, 로그에 남는 내용 — 을 여기서 본다.
"""
import logging

from app import mailer


class FakeSMTP:
    """smtplib.SMTP 대역. 무엇을 했는지만 적어 둔다."""

    last = None

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.calls = []
        self.message = None
        FakeSMTP.last = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, message):
        self.message = message


def _use_gmail(monkeypatch):
    monkeypatch.setattr(mailer.settings, "smtp_host", "smtp.gmail.com")
    monkeypatch.setattr(mailer.settings, "smtp_port", 587)
    monkeypatch.setattr(mailer.settings, "smtp_use_tls", True)
    monkeypatch.setattr(mailer.settings, "smtp_username", "team@gmail.com")
    # 구글 화면에 보이는 그대로 붙여 넣은 앱 비밀번호
    monkeypatch.setattr(mailer.settings, "smtp_password", "abcd efgh ijkl mnop")
    monkeypatch.setattr(mailer.settings, "smtp_from", None)
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)


def test_gmail_login_and_sender(monkeypatch):
    _use_gmail(monkeypatch)

    assert mailer.send_signup_code("user@example.com", "123456") is True

    smtp = FakeSMTP.last
    assert (smtp.host, smtp.port) == ("smtp.gmail.com", 587)
    # 띄어 쓴 앱 비밀번호도 로그인된다
    assert smtp.calls == ["starttls", ("login", "team@gmail.com", "abcdefghijklmnop")]
    # Gmail 은 로그인한 주소로만 보낼 수 있다. SMTP_FROM 을 비우면 그 주소로 보낸다.
    assert "team@gmail.com" in smtp.message["From"]
    assert smtp.message["To"] == "user@example.com"
    assert "123456" in smtp.message["Subject"]
    assert "123456" in smtp.message.get_content()


def test_sent_code_is_not_written_to_the_log(monkeypatch, caplog):
    """서버 로그를 볼 수 있는 사람이 남의 인증번호를 알아내면 안 된다."""
    _use_gmail(monkeypatch)

    with caplog.at_level(logging.INFO, logger="smart_energy.mailer"):
        assert mailer.send_signup_code("user@example.com", "987654") is True

    assert "user@example.com" in caplog.text
    assert "987654" not in caplog.text


def test_smtp_failure_returns_false(monkeypatch):
    """메일 서버가 죽었다고 요청 전체가 500 이 되면 안 된다. 호출한 쪽이 판단한다."""
    _use_gmail(monkeypatch)

    class Refused(FakeSMTP):
        def login(self, user, password):
            raise mailer.smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(mailer.smtplib, "SMTP", Refused)
    assert mailer.send_signup_code("user@example.com", "123456") is False
