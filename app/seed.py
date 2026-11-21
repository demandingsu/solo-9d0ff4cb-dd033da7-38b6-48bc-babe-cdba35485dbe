"""演示数据：启动时若库为空则写入，方便直接在 /docs 体验完整流程。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Observation,
    SurveyBatch,
    TaxonomyVersion,
)
from .taxonomy import create_taxonomy_version, mark_affected_batches_stale
from .services import publish_batch

# 分类表 v1：含异名、自环接受名
V1_ROWS = [
    {"name": "喜鹊", "higher_taxon": "雀形目", "accepted_name": "Pica pica",
     "synonyms": ["Pica serica"]},
    {"name": "Pica pica", "higher_taxon": "雀形目", "accepted_name": "Pica pica",
     "synonyms": []},
    {"name": "灰喜鹊", "higher_taxon": "雀形目", "accepted_name": "Cyanopica cyanus",
     "synonyms": ["Cyanopica cooki"]},
    {"name": "Cyanopica cyanus", "higher_taxon": "雀形目",
     "accepted_name": "Cyanopica cyanus", "synonyms": []},
    {"name": "树麻雀", "higher_taxon": "雀形目", "accepted_name": "Passer montanus",
     "synonyms": ["麻雀"]},
    {"name": "Passer montanus", "higher_taxon": "雀形目",
     "accepted_name": "Passer montanus", "synonyms": []},
    # 环路：甲指向乙，乙指回甲（v1 即存在，用于演示 cycle 待核）
    {"name": "神秘鸟甲", "higher_taxon": "待查", "accepted_name": "神秘鸟乙",
     "synonyms": []},
    {"name": "神秘鸟乙", "higher_taxon": "待查", "accepted_name": "神秘鸟甲",
     "synonyms": []},
]

# 分类表 v2：树麻雀的接受名改为新组合（影响批次1 → STALE）；
# 新增一个环路条目，可用于演示 cycle 待核
V2_ROWS = [
    {"name": "喜鹊", "higher_taxon": "雀形目", "accepted_name": "Pica pica",
     "synonyms": ["Pica serica"]},
    {"name": "Pica pica", "higher_taxon": "雀形目", "accepted_name": "Pica pica",
     "synonyms": []},
    {"name": "灰喜鹊", "higher_taxon": "雀形目", "accepted_name": "Cyanopica cyanus",
     "synonyms": ["Cyanopica cooki"]},
    {"name": "Cyanopica cyanus", "higher_taxon": "雀形目",
     "accepted_name": "Cyanopica cyanus", "synonyms": []},
    {"name": "树麻雀", "higher_taxon": "雀形目",
     "accepted_name": "Passer chinensis", "synonyms": ["麻雀"]},
    {"name": "Passer chinensis", "higher_taxon": "雀形目",
     "accepted_name": "Passer chinensis", "synonyms": []},
    # 环路条目沿用 v1
    {"name": "神秘鸟甲", "higher_taxon": "待查", "accepted_name": "神秘鸟乙",
     "synonyms": []},
    {"name": "神秘鸟乙", "higher_taxon": "待查", "accepted_name": "神秘鸟甲",
     "synonyms": []},
]

BATCH1_OBS = [
    ("P-01", "2026-11-01", "喜鹊", 3),
    ("P-01", "2026-11-01", "Pica serica", 2),       # 异名 → 同一接受种
    ("P-01", "2026-11-02", "麻雀", 5),
    ("P-02", "2026-11-01", "灰喜鹊", 4),
    ("P-02", "2026-11-01", "Cyanopica cooki", 1),   # 异名
    ("P-02", "2026-11-03", "树麻雀", 7),
]

# 批次2：含未知名与环路名称，发布会整批待核
BATCH2_OBS = [
    ("P-03", "2026-11-05", "喜鹊", 2),
    ("P-03", "2026-11-05", "火星潜鸟", 1),          # 未知名
    ("P-03", "2026-11-06", "神秘鸟甲", 2),          # 环路
]


def seed_if_empty(db: Session) -> bool:
    if _has_data(db):
        return False

    v1 = create_taxonomy_version(db, V1_ROWS, note="演示分类表 v1（初始版本）")
    db.flush()

    batch1 = SurveyBatch(name="示例批次A（正常发布）", investigator="张研究员")
    db.add(batch1)
    db.flush()
    for plot, day, raw, qty in BATCH1_OBS:
        db.add(Observation(batch_id=batch1.id, plot_code=plot, observed_on=day,
                           raw_species_name=raw, quantity=qty))
    db.flush()
    publish_batch(db, batch1, v1.id)

    batch2 = SurveyBatch(name="示例批次B（含未知名/环路，待核）", investigator="李研究员")
    db.add(batch2)
    db.flush()
    for plot, day, raw, qty in BATCH2_OBS:
        db.add(Observation(batch_id=batch2.id, plot_code=plot, observed_on=day,
                           raw_species_name=raw, quantity=qty))
    db.flush()
    publish_batch(db, batch2, v1.id)  # 预期整批待核

    batch3 = SurveyBatch(name="示例批次C（草稿，可自行上传发布）",
                         investigator="王研究员")
    db.add(batch3)

    # 发布 v2：批次A 中“树麻雀/麻雀”的接受名变化 → 自动 STALE，旧结果保留
    v2 = create_taxonomy_version(
        db, V2_ROWS, note="演示分类表 v2（麻雀属修订 + 新增环路条目）")
    mark_affected_batches_stale(db, v1, v2)
    db.commit()
    return True


def _has_data(db: Session) -> bool:
    from .models import TaxonomyVersion
    return db.scalar(select(TaxonomyVersion).limit(1)) is not None
