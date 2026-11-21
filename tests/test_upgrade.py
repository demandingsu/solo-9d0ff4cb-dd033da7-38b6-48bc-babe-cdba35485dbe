"""旧 SQLite 数据卷升级：上一版留下的库文件无需迁移，

新代码启动（init_db 幂等）后，上传新分类表时旧批次仍被正确标记 STALE。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import database as database_mod
from app.config import settings
from app.main import app

# 旧版应用写入的演示数据（裸 SQL 模拟上一版运行时的落库结果）
_V1_ROWS = [
    (1, 1, "喜鹊", "雀形目", "Pica pica", "[]"),
    (2, 1, "Pica pica", "雀形目", "Pica pica", "[]"),
    (3, 1, "麻雀", "雀形目", "Passer montanus", "[]"),
    (4, 1, "Passer montanus", "雀形目", "Passer montanus", "[]"),
]
_TS = "2026-11-01 08:00:00.000000"


def _write_old_volume(db_path: str) -> None:
    """生成一个“上一版应用”留下的数据卷。

    库结构与新代码一致（无 schema 变更，init_db 幂等），
    数据用裸 SQL 写入，模拟旧应用已经提交的业务数据。
    """
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    database_mod.Base.metadata.create_all(bind=engine)
    engine.dispose()

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO taxonomy_versions (id, version, note, created_at)"
            " VALUES (1, 1, '旧版分类表 v1', ?)", (_TS,))
        cur.executemany(
            "INSERT INTO taxon_rows"
            " (id, taxonomy_version_id, name, higher_taxon, accepted_name,"
            "  synonyms_json) VALUES (?, ?, ?, ?, ?, ?)",
            _V1_ROWS,
        )
        # 批次1：含麻雀（将被 v2 影响）；批次2：只含喜鹊（不受影响）
        cur.executemany(
            "INSERT INTO survey_batches"
            " (id, name, investigator, status, published_taxonomy_version_id,"
            "  published_at, last_publish_error, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            [
                (1, "旧批次A", "张研究员", "PUBLISHED", 1, _TS, _TS, _TS),
                (2, "旧批次B", "李研究员", "PUBLISHED", 1, _TS, _TS, _TS),
            ],
        )
        cur.executemany(
            "INSERT INTO observations"
            " (id, batch_id, plot_code, observed_on, raw_species_name, quantity)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, 1, "P-01", "2026-11-01", "喜鹊", 3),
                (2, 1, "P-01", "2026-11-02", "麻雀", 5),
                (3, 2, "P-02", "2026-11-01", "喜鹊", 2),
            ],
        )
        cur.executemany(
            "INSERT INTO resolved_observations"
            " (id, batch_id, taxonomy_version_id, plot_code, accepted_name,"
            "  raw_species_name, observed_on, quantity)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 1, 1, "P-01", "Pica pica", "喜鹊", "2026-11-01", 3),
                (2, 1, 1, "P-01", "Passer montanus", "麻雀", "2026-11-02", 5),
                (3, 2, 1, "P-02", "Pica pica", "喜鹊", "2026-11-01", 2),
            ],
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def upgraded_client(monkeypatch, tmp_path):
    """把应用指向一个预先存在的旧数据卷（模拟升级后重启）。"""
    db_file = tmp_path / "old_volume.db"
    _write_old_volume(str(db_file))

    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False}, future=True,
    )
    TestSession = sessionmaker(bind=engine, autoflush=False,
                               expire_on_commit=False)
    database_mod.engine.dispose()
    monkeypatch.setattr(database_mod, "engine", engine)
    monkeypatch.setattr(database_mod, "SessionLocal", TestSession)
    monkeypatch.setattr(settings, "seed_demo_on_startup", False)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[database_mod.get_db] = override_get_db
    # TestClient 启动即“升级”：init_db 在旧库上幂等执行，不得破坏数据
    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()
    engine.dispose()


def test_old_volume_survives_upgrade_and_marks_stale(upgraded_client):
    # 升级后旧数据完整可读
    versions = upgraded_client.get("/taxonomy/versions").json()
    assert [v["version"] for v in versions] == [1]
    assert upgraded_client.get("/batches/1").json()["status"] == "PUBLISHED"

    # 上传 v2（麻雀改名）→ 旧批次按新逻辑正确标记
    r = upgraded_client.post("/taxonomy/versions", json={
        "note": "v2 麻雀改名",
        "rows": [
            {"name": "喜鹊", "higher_taxon": "雀形目",
             "accepted_name": "Pica pica", "synonyms": []},
            {"name": "Pica pica", "higher_taxon": "雀形目",
             "accepted_name": "Pica pica", "synonyms": []},
            {"name": "麻雀", "higher_taxon": "雀形目",
             "accepted_name": "Passer chinensis", "synonyms": []},
            {"name": "Passer chinensis", "higher_taxon": "雀形目",
             "accepted_name": "Passer chinensis", "synonyms": []},
        ],
    })
    assert r.status_code == 201, r.text
    assert r.json()["stale_batch_ids"] == [1]

    assert upgraded_client.get("/batches/1").json()["status"] == "STALE"
    assert upgraded_client.get("/batches/2").json()["status"] == "PUBLISHED"

    # 旧批次的上一份完整结果仍可查（旧接受名保留）
    old = upgraded_client.get("/batches/1/results").json()
    assert old["stale"] is True
    assert {a["accepted_name"] for a in old["aggregated"]} == {
        "Pica pica", "Passer montanus"}

    # 旧批次重发成功 → 明细与版本引用一并替换
    repub = upgraded_client.post("/batches/1/publish").json()
    assert repub["success"] is True
    new = upgraded_client.get("/batches/1/results").json()
    assert {a["accepted_name"] for a in new["aggregated"]} == {
        "Pica pica", "Passer chinensis"}
