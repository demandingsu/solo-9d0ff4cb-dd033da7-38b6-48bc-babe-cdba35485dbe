"""批次发布与结果汇总服务。"""
from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    BatchStatus,
    Observation,
    PublishIssue,
    ResolvedObservation,
    SurveyBatch,
    TaxonRow,
    TaxonomyVersion,
)
from .taxonomy import build_name_index, resolve_all


class PublishError(Exception):
    """发布前置条件不满足（对应 HTTP 4xx）。"""


@dataclass
class PublishOutcome:
    success: bool
    status: BatchStatus
    taxonomy_version_id: int
    message: str
    issues: list[dict]


def publish_batch(
    db: Session, batch: SurveyBatch, taxonomy_version_id: int
) -> PublishOutcome:
    """按指定分类表版本发布批次。

    事务策略：先在内存中完成全部解析，只有整批合法时才删除旧结果并写入新结果，
    最后统一 commit。任何异常都会回滚，旧结果（如有）继续对外提供。
    """
    version = db.get(TaxonomyVersion, taxonomy_version_id)
    if version is None:
        raise PublishError(f"分类表版本 {taxonomy_version_id} 不存在")

    observations = list(db.scalars(
        select(Observation).where(Observation.batch_id == batch.id)
        .order_by(Observation.id)
    ))
    if not observations:
        raise PublishError("批次没有任何观测记录，无法发布")

    rows = list(db.scalars(select(TaxonRow).where(
        TaxonRow.taxonomy_version_id == version.id)))
    index = build_name_index(rows)
    resolutions = resolve_all([o.raw_species_name for o in observations], index)

    problems = [r for r in resolutions if r.issue_type is not None]

    # 每次发布都重置问题清单，反映最近一次尝试（直接查表，避免关系缓存陈旧）
    for old_issue in db.scalars(select(PublishIssue).where(
            PublishIssue.batch_id == batch.id)).all():
        db.delete(old_issue)

    if problems:
        for res in problems:
            db.add(PublishIssue(
                batch_id=batch.id,
                issue_type=res.issue_type,
                raw_species_name=res.raw_name,
                detail=res.detail,
                candidates_json=json.dumps(res.candidates, ensure_ascii=False),
            ))
        batch.status = BatchStatus.NEEDS_REVIEW
        batch.last_publish_error = (
            f"{len(problems)} 条记录无法解析（unknown/ambiguous/cycle），整批待核；"
            "旧结果（如有）继续可用"
        )
        db.commit()
        return PublishOutcome(
            success=False,
            status=batch.status,
            taxonomy_version_id=version.id,
            message=batch.last_publish_error,
            issues=[_issue_dict(p) for p in problems],
        )

    # 整批合法：替换旧结果（原子提交，失败则回滚保留上一份完整版本）
    for old_resolved in db.scalars(select(ResolvedObservation).where(
            ResolvedObservation.batch_id == batch.id)).all():
        db.delete(old_resolved)
    db.flush()

    for obs, res in zip(observations, resolutions):
        db.add(ResolvedObservation(
            batch_id=batch.id,
            taxonomy_version_id=version.id,
            plot_code=obs.plot_code,
            accepted_name=res.accepted_name,
            raw_species_name=obs.raw_species_name,
            observed_on=obs.observed_on,
            quantity=obs.quantity,
        ))

    batch.status = BatchStatus.PUBLISHED
    batch.published_taxonomy_version_id = version.id
    from datetime import datetime, timezone
    batch.published_at = datetime.now(timezone.utc)
    batch.last_publish_error = None
    db.commit()

    return PublishOutcome(
        success=True,
        status=batch.status,
        taxonomy_version_id=version.id,
        message=f"发布成功：{len(observations)} 条记录已按分类表 v{version.version} 解析",
        issues=[],
    )


def _issue_dict(res) -> dict:
    return {
        "issue_type": res.issue_type,
        "raw_species_name": res.raw_name,
        "detail": res.detail,
        "candidates": res.candidates,
    }


# ---------------- 结果汇总 ----------------

def aggregate_resolved(records: list[ResolvedObservation]) -> list[dict]:
    """按样地 + 接受种汇总，保留原名集合与日期集合。"""
    groups: dict[tuple[str, str], dict] = {}
    for r in records:
        key = (r.plot_code, r.accepted_name)
        g = groups.get(key)
        if g is None:
            g = {
                "plot_code": r.plot_code,
                "accepted_name": r.accepted_name,
                "total_quantity": 0,
                "record_count": 0,
                "_raw": set(),
                "_dates": set(),
            }
            groups[key] = g
        g["total_quantity"] += r.quantity
        g["record_count"] += 1
        g["_raw"].add(r.raw_species_name)
        g["_dates"].add(r.observed_on)

    out = []
    for g in groups.values():
        out.append({
            "plot_code": g["plot_code"],
            "accepted_name": g["accepted_name"],
            "total_quantity": g["total_quantity"],
            "record_count": g["record_count"],
            "raw_names": sorted(g["_raw"]),
            "observed_dates": sorted(g["_dates"]),
        })
    out.sort(key=lambda x: (x["plot_code"], x["accepted_name"]))
    return out
