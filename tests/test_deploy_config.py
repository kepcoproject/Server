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


def test_schema_compiles_for_postgresql():
    """
    로컬은 SQLite, 배포는 PostgreSQL 이다. 방언 차이로 테이블이 안 만들어지면
    배포 후 첫 기동에서야 드러나므로 미리 DDL 을 생성해 본다.
    접속은 하지 않고 문법만 확인한다.
    """
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    from app import models  # noqa: F401  모든 테이블을 메타데이터에 등록
    from app.database import Base

    pg = postgresql.dialect()
    assert Base.metadata.sorted_tables, "등록된 테이블이 없다"
    for table in Base.metadata.sorted_tables:
        CreateTable(table).compile(dialect=pg)
        for index in table.indexes:
            CreateIndex(index).compile(dialect=pg)


def test_compat_prefix_moves_routes_off_screen_paths():
    """
    화면과 같은 주소에서 서빙할 때 /spaces, /devices, /users 는 화면 경로다.
    API 를 접두사 아래로 내리지 않으면 화면 대신 JSON 이 나온다.
    """
    from fastapi import FastAPI

    from app.api.compat_admin_routes import router as admin_router
    from app.api.compat_routes import router as compat_router

    app = FastAPI()
    app.include_router(compat_router, prefix="/client-api")
    app.include_router(admin_router, prefix="/client-api")
    # 이 FastAPI 버전은 include_router 를 지연 처리해 app.routes 로는 안 보인다.
    paths = set(app.openapi()["paths"])

    assert "/client-api/auth/login" in paths
    assert "/client-api/spaces" in paths
    # 접두사 없는 경로는 화면 몫으로 비어 있어야 한다
    assert "/spaces" not in paths
    assert "/devices" not in paths
    assert "/users" not in paths
