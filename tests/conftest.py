"""pytest 夹具：每个测试使用独立的临时 SQLite 文件，不触碰演示库。

不 reload 应用模块（reload 会让路由持有的 get_db 引用失效），
而是直接把 app.database 的引擎/会话工厂替换为测试库。
"""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import database as database_mod
from app.config import settings
from app.main import app


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_url = f"sqlite:///{tmp.name}"

    test_engine = create_engine(
        db_url, connect_args={"check_same_thread": False}, future=True
    )
    TestSession = sessionmaker(bind=test_engine, autoflush=False,
                               expire_on_commit=False)

    # 先释放模块级旧引擎（其连接缓存可能指向已被上一个测试删除的库文件）
    database_mod.engine.dispose()
    monkeypatch.setattr(database_mod, "engine", test_engine)
    monkeypatch.setattr(database_mod, "SessionLocal", TestSession)
    # main.py 直接引用 settings 实例；关闭演示种子
    monkeypatch.setattr(settings, "seed_demo_on_startup", False)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[database_mod.get_db] = override_get_db
    # 直接用测试引擎对共享的模型元数据建表
    from app import models  # noqa: F401  确保模型已注册到 Base.metadata
    database_mod.Base.metadata.create_all(bind=test_engine)

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()
    test_engine.dispose()
    os.unlink(tmp.name)
