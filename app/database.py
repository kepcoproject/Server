import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import get_settings

logger = logging.getLogger("smart_energy.database")

settings = get_settings()

def _normalize_database_url(url: str) -> str:
    """
    호스팅 업체(Render, Heroku 등)는 DATABASE_URL 을 postgres:// 로 준다.
    SQLAlchemy 2.0 은 이 접두사를 더 이상 받지 않아 기동하다 죽으므로 바꿔 준다.
    """
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


database_url = _normalize_database_url(settings.database_url)
if database_url != settings.database_url:
    logger.info("DATABASE_URL 접두사를 SQLAlchemy 형식으로 변환했습니다")

connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}

engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _add_missing_columns() -> None:
    """
    이미 있는 테이블에 새로 생긴 컬럼을 채워 넣는다.

    create_all 은 없는 '테이블'만 만들고 기존 테이블의 '컬럼'은 건드리지 않는다.
    그래서 모델에 컬럼을 추가하고 배포하면, 기존 DB를 쓰던 곳에서
    "no such column" 으로 깨진다. 마이그레이션 도구(Alembic)를 넣기 전까지
    컬럼 추가만이라도 자동으로 맞춰 준다.

    컬럼 추가만 처리한다. 이름 변경·타입 변경·삭제는 다루지 않으므로,
    스키마가 더 복잡해지면 Alembic 으로 옮겨야 한다.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all 이 방금 만들었으므로 최신이다
        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            col_type = column.type.compile(engine.dialect)
            ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type}"
            # SQLite 는 NOT NULL 컬럼을 기본값 없이 추가하지 못한다.
            # 기존 행을 채울 값이 없으면 NULL 허용으로 붙인다.
            default = getattr(column.default, "arg", None)
            if not column.nullable and default is not None and not callable(default):
                literal = f"'{default}'" if isinstance(default, str) else default
                ddl += f" NOT NULL DEFAULT {literal}"
            with engine.begin() as conn:
                conn.execute(text(ddl))
            logger.info("스키마 갱신: %s.%s 컬럼을 추가했습니다", table.name, column.name)


def init_db():
    from . import models

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
