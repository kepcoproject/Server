"""
배포 설정 검증.

클라우드에 올렸을 때 기동조차 못 하는 실수를 미리 잡는다.
로컬(SQLite)에서는 드러나지 않고 배포해야만 터지는 것들이라 테스트로 못박아 둔다.
"""
import io
import re

from app.database import _normalize_database_url


def test_postgres_url_prefix_is_normalized():
    """
    Render·Heroku 는 DATABASE_URL 을 postgres:// 로 준다.
    SQLAlchemy 2.0 은 이 접두사를 거부하므로 변환하지 않으면 기동하다 죽는다.
    """
    assert (
        _normalize_database_url("postgres://u:p@host/db")
        == "postgresql+psycopg2://u:p@host/db"
    )
    assert (
        _normalize_database_url("postgresql://u:p@host/db")
        == "postgresql+psycopg2://u:p@host/db"
    )


def test_already_normalized_url_is_untouched():
    for url in (
        "postgresql+psycopg2://u:p@host/db",
        "sqlite:///./smart_energy.db",
        "sqlite:///:memory:",
    ):
        assert _normalize_database_url(url) == url


def test_postgres_driver_is_installed():
    """
    render.yaml 이 PostgreSQL 을 붙이는데 드라이버가 없으면 기동 실패한다.
    requirements.txt 에서 주석 처리되지 않았는지 확인한다.
    """
    text = io.open("requirements.txt", encoding="utf-8").read()
    active = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert any(re.match(r"psycopg2", line) for line in active), (
        "psycopg2-binary 가 주석 처리되어 있으면 PostgreSQL 배포가 기동하지 못한다"
    )


def test_dockerfile_uses_single_worker():
    """
    워커를 여러 개 띄우면 MQTT 구독이 워커마다 붙어 같은 메시지를 중복 저장하고
    웹훅도 여러 번 나간다.
    """
    text = io.open("Dockerfile", encoding="utf-8").read()
    assert "--workers 1" in text
