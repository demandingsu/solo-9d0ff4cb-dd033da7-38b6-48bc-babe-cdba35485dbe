"""发布与查询接口。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .models import (
    BatchStatus,
    PublishIssue,
    ResolvedObservation,
    SurveyBatch,
    TaxonomyVersion,
)
from .schemas import (
    AggregatedRowOut,
    BatchResultsOut,
    GlobalResultsOut,
    PublishIssueOut,
    PublishResultOut,
    ResolvedRowOut,
)
from .services import PublishError, aggregate_resolved, publish_batch

router = APIRouter(tags=["发布与查询"])


def _latest_taxonomy_id(db: Session) -> int:
    latest = db.scalar(select(TaxonomyVersion).order_by(
        TaxonomyVersion.version.desc()).limit(1))
    if latest is None:
        raise HTTPException(409, "系统中还没有任何分类表版本，无法发布")
    return latest.id


@router.post("/batches/{batch_id}/publish",
             response_model=PublishResultOut,
             summary="发布批次（按分类表解析名称并汇总）")
def publish(batch_id: int,
            taxonomy_version_id: int | None = Query(
                None, description="指定分类表版本；不传则使用最新版本"),
            db: Session = Depends(get_db)) -> PublishResultOut:
    batch = db.get(SurveyBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"批次 {batch_id} 不存在")
    version_id = taxonomy_version_id or _latest_taxonomy_id(db)
    try:
        outcome = publish_batch(db, batch, version_id)
    except PublishError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PublishResultOut(
        batch_id=batch.id,
        success=outcome.success,
        status=outcome.status.value,
        taxonomy_version_id=outcome.taxonomy_version_id,
        message=outcome.message,
        issues=[PublishIssueOut(**i) for i in outcome.issues],
    )


@router.get("/batches/{batch_id}/issues",
            response_model=list[PublishIssueOut],
            summary="查看批次最近一次发布的核校问题")
def get_issues(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(SurveyBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"批次 {batch_id} 不存在")
    out = []
    for issue in db.scalars(select(PublishIssue).where(
            PublishIssue.batch_id == batch_id).order_by(PublishIssue.id)):
        out.append(PublishIssueOut(
            issue_type=issue.issue_type,
            raw_species_name=issue.raw_species_name,
            detail=issue.detail,
            candidates=json.loads(issue.candidates_json or "[]"),
        ))
    return out


def _resolved_to_output(records: list[ResolvedObservation]):
    aggregated = [AggregatedRowOut(**row) for row in aggregate_resolved(records)]
    resolved = [
        ResolvedRowOut(
            plot_code=r.plot_code,
            accepted_name=r.accepted_name,
            raw_species_name=r.raw_species_name,
            observed_on=r.observed_on,
            quantity=r.quantity,
            taxonomy_version_id=r.taxonomy_version_id,
        )
        for r in sorted(records, key=lambda x: (x.plot_code, x.accepted_name,
                                                x.observed_on, x.raw_species_name))
    ]
    return aggregated, resolved


@router.get("/batches/{batch_id}/results",
            response_model=BatchResultsOut,
            summary="查询批次发布结果（按样地+接受种汇总）")
def get_results(batch_id: int, db: Session = Depends(get_db)) -> BatchResultsOut:
    batch = db.get(SurveyBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"批次 {batch_id} 不存在")
    if batch.published_taxonomy_version_id is None:
        raise HTTPException(
            409, f"批次 {batch_id} 尚未成功发布过（当前状态 {batch.status.value}），"
                 "没有可查询的结果")
    records = list(db.scalars(select(ResolvedObservation).where(
        ResolvedObservation.batch_id == batch_id)))
    aggregated, resolved = _resolved_to_output(records)
    return BatchResultsOut(
        batch_id=batch.id,
        batch_name=batch.name,
        status=batch.status.value,
        stale=batch.status == BatchStatus.STALE,
        taxonomy_version_id=batch.published_taxonomy_version_id,
        published_at=batch.published_at,
        aggregated=aggregated,
        resolved_records=resolved,
    )


@router.get("/results", response_model=GlobalResultsOut,
            summary="跨批次全局查询已发布结果（可按样地/接受种过滤）")
def query_results(
    plot_code: str | None = Query(None, description="样地编号精确匹配"),
    accepted_name: str | None = Query(None, description="接受种精确匹配"),
    include_stale: bool = Query(
        True, description="是否包含 STALE 批次的上一份完整结果，默认包含"),
    db: Session = Depends(get_db),
) -> GlobalResultsOut:
    stmt = select(ResolvedObservation).join(SurveyBatch)
    if not include_stale:
        stmt = stmt.where(SurveyBatch.status != BatchStatus.STALE)
    if plot_code:
        stmt = stmt.where(ResolvedObservation.plot_code == plot_code.strip())
    if accepted_name:
        stmt = stmt.where(
            ResolvedObservation.accepted_name == accepted_name.strip())
    records = list(db.scalars(stmt.order_by(ResolvedObservation.id)))
    aggregated, resolved = _resolved_to_output(records)
    return GlobalResultsOut(
        filters={"plot_code": plot_code, "accepted_name": accepted_name,
                 "include_stale": include_stale},
        count=len(records),
        aggregated=aggregated,
        resolved_records=resolved,
    )
