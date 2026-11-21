"""分类表名称解析核心逻辑（纯函数，便于单元测试）。

解析规则
--------
对每个原始物种名，按以下顺序处理：
1. 未知名：名称（含各表行声明的同物异名）在本版本分类表中完全找不到。
2. 同名歧义：同一个名称对应到多个不同的接受名，整批待核。
3. 环路：沿 accepted_name 链向上解析时出现重复节点（含自环），整批待核。
4. 合法：链终止在某个接受名上；若链尾指向一个表中不存在的名称（悬挂引用），
   则把链尾名称本身作为接受名，不视为错误（常见于不完整的上位分类补录）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import BatchStatus, ResolvedObservation, SurveyBatch, TaxonRow, TaxonomyVersion

UNKNOWN = "unknown"
AMBIGUOUS = "ambiguous"
CYCLE = "cycle"


@dataclass
class NameIndex:
    """一个分类表版本构建出的名称索引。"""

    # 规范化名称 -> 该名称指向的全部接受名（去重保序）
    mapping: dict[str, list[str]] = field(default_factory=dict)
    # 规范名称（分类表中实际出现过的 name）集合
    canonical: set[str] = field(default_factory=set)
    # 同名对应多个接受名的名称集合
    ambiguous_keys: set[str] = field(default_factory=set)

    def get_accepted_candidates(self, key: str) -> list[str]:
        return self.mapping.get(key, [])


def _norm(name: str) -> str:
    """名称规范化：去首尾空白并按大小写不敏感处理，展示仍用原名。"""
    return name.strip().casefold()


def build_name_index(rows: list[TaxonRow]) -> NameIndex:
    """从分类表行构建名称索引。

    每个名称（行名与同物异名）都映射到该行的接受名；
    同一名称映射到多个不同接受名时进入 ambiguous_keys。
    """
    index = NameIndex()
    for row in rows:
        accepted = row.accepted_name.strip()
        keys = [row.name.strip()]
        try:
            keys.extend(json.loads(row.synonyms_json or "[]"))
        except json.JSONDecodeError:
            keys.extend([])
        for raw_key in keys:
            key = _norm(raw_key)
            if not key:
                continue
            bucket = index.mapping.setdefault(key, [])
            if accepted not in bucket:
                bucket.append(accepted)
        index.canonical.add(_norm(row.name))

    index.ambiguous_keys = {
        key for key, accepted in index.mapping.items() if len(accepted) > 1
    }
    return index


@dataclass
class Resolution:
    raw_name: str
    accepted_name: str | None
    issue_type: str | None
    detail: str = ""
    candidates: list[str] = field(default_factory=list)
    chain: list[str] = field(default_factory=list)


def resolve_name(raw_name: str, index: NameIndex) -> Resolution:
    """解析单个原始物种名。"""
    key = _norm(raw_name)
    candidates = index.get_accepted_candidates(key)

    if not candidates:
        return Resolution(
            raw_name=raw_name,
            accepted_name=None,
            issue_type=UNKNOWN,
            detail=f"名称 '{raw_name}' 在分类表中不存在（含同物异名匹配）",
        )

    if key in index.ambiguous_keys:
        return Resolution(
            raw_name=raw_name,
            accepted_name=None,
            issue_type=AMBIGUOUS,
            detail=f"名称 '{raw_name}' 同名对应多个接受名: {', '.join(candidates)}",
            candidates=list(candidates),
        )

    # 沿 accepted_name 链解析，同时检测环路
    chain: list[str] = list(candidates)
    seen = {_norm(candidates[0])}
    current = candidates[0]
    while True:
        next_candidates = index.get_accepted_candidates(_norm(current))
        if not next_candidates:
            # 悬挂引用：链尾不在表中，以链尾名称本身作为接受名
            return Resolution(raw_name=raw_name, accepted_name=current,
                              issue_type=None, chain=chain)
        nxt = next_candidates[0]
        nxt_key = _norm(nxt)
        if nxt == current:
            # 自环：名称指向自身，属于稳定接受名
            return Resolution(raw_name=raw_name, accepted_name=current,
                              issue_type=None, chain=chain)
        if nxt_key in seen:
            chain.append(nxt)
            return Resolution(
                raw_name=raw_name,
                accepted_name=None,
                issue_type=CYCLE,
                detail=f"接受名链路存在环路: {' -> '.join(chain)}",
                chain=chain,
            )
        seen.add(nxt_key)
        chain.append(nxt)
        current = nxt


def resolve_all(raw_names: list[str], index: NameIndex) -> list[Resolution]:
    """批量解析，重复名称只解析一次并复用结果。"""
    cache: dict[str, Resolution] = {}
    results: list[Resolution] = []
    for raw_name in raw_names:
        key = _norm(raw_name)
        if key not in cache:
            cache[key] = resolve_name(raw_name, index)
        results.append(cache[key])
    return results


# ---------------- 数据库操作 ----------------

def create_taxonomy_version(
    db: Session, rows_payload: list[dict], note: str | None
) -> TaxonomyVersion:
    """创建一个不可变的分类表版本，并返回受影响而过期的批次集合。"""
    latest = db.scalar(select(TaxonomyVersion).order_by(
        TaxonomyVersion.version.desc()).limit(1))
    next_version = 1 if latest is None else latest.version + 1

    version = TaxonomyVersion(version=next_version, note=note)
    db.add(version)
    db.flush()  # 拿到 version.id

    for item in rows_payload:
        db.add(TaxonRow(
            taxonomy_version_id=version.id,
            name=item["name"].strip(),
            higher_taxon=(item.get("higher_taxon") or None),
            accepted_name=item["accepted_name"].strip(),
            synonyms_json=json.dumps(item.get("synonyms") or [],
                                     ensure_ascii=False),
        ))
    db.flush()
    return version


def mark_affected_batches_stale(
    db: Session, old_version: TaxonomyVersion | None,
    new_version: TaxonomyVersion,
) -> list[int]:
    """新分类表发布后，只让解析结果受影响的批次过期。

    判定方式：对比新旧两个版本对“批次已解析记录中出现过的原始名称”
    的最终接受名，接受名发生变化（或由可解析变为不可解析）才标记 STALE。
    """
    if old_version is None:
        return []

    old_rows = list(db.scalars(select(TaxonRow).where(
        TaxonRow.taxonomy_version_id == old_version.id)))
    new_rows = list(db.scalars(select(TaxonRow).where(
        TaxonRow.taxonomy_version_id == new_version.id)))
    old_index, new_index = build_name_index(old_rows), build_name_index(new_rows)

    affected: list[int] = []
    batches = list(db.scalars(select(SurveyBatch).where(
        SurveyBatch.published_taxonomy_version_id == old_version.id)))
    for batch in batches:
        # 直接查表而非用 batch.resolved 关系集合，避免会话缓存陈旧
        raw_names = list({
            row[0]
            for row in db.execute(
                select(ResolvedObservation.raw_species_name).where(
                    ResolvedObservation.batch_id == batch.id)
            ).all()
        })
        changed = False
        for raw_name in raw_names:
            old_res = resolve_name(raw_name, old_index)
            new_res = resolve_name(raw_name, new_index)
            if old_res.accepted_name != new_res.accepted_name:
                changed = True
                break
        if changed:
            batch.status = BatchStatus.STALE
            affected.append(batch.id)
    return affected
