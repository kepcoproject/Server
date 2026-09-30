"""
메일 발송.

회원가입 인증번호와 비밀번호 재설정 링크를 보낸다.

SMTP 설정이 비어 있으면 보내지 않고 내용을 로그에 남긴다. 학교 서버처럼 바깥
메일 포트가 막혀 있거나 계정이 아직 없는 환경에서도 흐름을 확인할 수 있어야
하기 때문이다. 공개 배포에서는 반드시 SMTP_HOST 부터 채울 것.
"""
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from .config import get_settings

logger = logging.getLogger("smart_energy.mailer")
settings = get_settings()


def is_configured() -> bool:
    return bool(settings.smtp_host)


def _send(to_address: str, subject: str, body: str) -> bool:
    """
    한 통 보낸다. 보냈으면 True.

    메일 서버가 죽어 있다고 회원가입 자체가 실패하면 안 되므로 예외를 삼키고
    False 를 돌려준다. 호출하는 쪽은 성공 여부와 무관하게 같은 응답을 준다.
    """
    if not is_configured():
        # 개발·시연용. 진짜로 보내지 않고 로그에 남긴다.
        logger.warning(
            "SMTP 미설정 — 메일을 보내지 않고 내용만 남깁니다.\n"
            "  받는 사람: %s\n  제목: %s\n%s",
            to_address,
            subject,
            body,
        )
        return False

    message = EmailMessage()
    message["Subject"] = subject
    sender = settings.smtp_from or settings.smtp_username or "no-reply@smart-energy.local"
    message["From"] = formataddr((settings.smtp_from_name, sender))
    message["To"] = to_address
    message.set_content(body)

    try:
        # 465 는 처음부터 SSL, 587 은 평문으로 붙은 뒤 STARTTLS 로 올린다.
        if settings.smtp_port == 465:
            with smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout,
                context=ssl.create_default_context(),
            ) as client:
                _login_and_send(client, message)
        else:
            with smtplib.SMTP(
                settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout
            ) as client:
                if settings.smtp_use_tls:
                    client.starttls(context=ssl.create_default_context())
                _login_and_send(client, message)
        # 제목은 남기지 않는다. 인증번호가 들어 있다.
        logger.info("메일 발송: %s", to_address)
        return True
    except Exception:
        # 주소를 남기되 본문은 남기지 않는다 (인증번호·재설정 토큰이 들어 있다).
        logger.exception("메일 발송 실패: %s", to_address)
        return False


def _login_and_send(client: smtplib.SMTP, message: EmailMessage) -> None:
    if settings.smtp_username:
        password = settings.smtp_password or ""
        # Gmail 앱 비밀번호는 "abcd efgh ijkl mnop" 처럼 띄어서 보여 준다.
        # 그대로 붙여 넣는 경우가 많은데 공백이 있으면 로그인이 거절된다.
        if (settings.smtp_host or "").endswith("gmail.com"):
            password = password.replace(" ", "")
        client.login(settings.smtp_username, password)
    client.send_message(message)


def send_signup_code(to_address: str, code: str) -> bool:
    minutes = settings.email_code_ttl_minutes
    return _send(
        to_address,
        # 제목에도 번호를 넣는다. 휴대폰 알림만 보고 바로 입력할 수 있다.
        f"[스마트 에너지 절약 시스템] 인증번호 {code}",
        f"회원가입 화면에 아래 인증번호를 입력하세요.\n\n"
        f"    {code}\n\n"
        f"이 번호는 {minutes}분 동안만 쓸 수 있습니다.\n"
        f"가입하려던 적이 없다면 이 메일은 무시하셔도 됩니다.\n",
    )


def send_password_reset(to_address: str, name: str, link: str) -> bool:
    minutes = settings.password_reset_ttl_minutes
    return _send(
        to_address,
        "[스마트 에너지 절약 시스템] 비밀번호 재설정",
        f"{name}님, 안녕하세요.\n\n"
        f"아래 주소에서 새 비밀번호를 정할 수 있습니다.\n\n"
        f"{link}\n\n"
        f"이 링크는 {minutes}분 동안만 쓸 수 있고, 한 번 쓰면 사라집니다.\n"
        f"요청한 적이 없다면 이 메일은 무시하셔도 됩니다. 비밀번호는 그대로입니다.\n",
    )
