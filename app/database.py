"""SQLAlchemy 引擎与会话管理。"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


# check_same_thread=False：SQLite 下 FastAPI 的请求线程与后台任务可共用引擎
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False}
    if settings.database_url.startswith("sqlite")
    else {},
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """建表（幂等）。"""
    from . import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(bind=engine)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每请求一个会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
