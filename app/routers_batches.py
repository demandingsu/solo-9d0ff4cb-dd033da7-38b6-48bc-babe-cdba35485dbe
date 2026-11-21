"""调查批次与观测记录接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .csv_io import CSVParseError, parse_observation_csv
from .database import get_db
from .models import BatchStatus, Observation, SurveyBatch
from .schemas import (
    BatchCreateIn,
    BatchOut,
    ObservationBulkIn,
    ObservationIn,
    ObservationOut,
    UploadResultOut,
)

router = APIRouter(tags=["批次与观测"])

# 只有这些状态允许继续上传/修改原始观测
_UPLOADABLE = {BatchStatus.DRAFT, BatchStatus.NEEDS_REVIEW}


@router.post("/batches", response_model=BatchOut, status_code=201,
             summary="创建调查批次")
def create_batch(payload: BatchCreateIn,
                 db: Session = Depends(get_db)) -> SurveyBatch:
    batch = SurveyBatch(name=payload.name.strip(),
                        investigator=(payload.investigator or None))
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


@router.get("/batches", response_model=list[BatchOut], summary="批次列表")
def list_batches(db: Session = Depends(get_db)) -> list[SurveyBatch]:
    return list(db.scalars(select(SurveyBatch).order_by(SurveyBatch.id)))


@router.get("/batches/{batch_id}", response_model=BatchOut, summary="批次详情")
def get_batch(batch_id: int, db: Session = Depends(get_db)) -> SurveyBatch:
    batch = db.get(SurveyBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"批次 {batch_id} 不存在")
    return batch


def _check_uploadable(batch: SurveyBatch) -> None:
    if batch.status not in _UPLOADABLE:
        raise HTTPException(
            409,
            f"批次状态为 {batch.status.value}，不允许再上传观测记录；"
            "如需修订请基于新批次重新开始",
        )


def _insert_observations(db: Session, batch_id: int,
                         items: list[dict]) -> UploadResultOut:
    # 预查已有记录，避免依赖 IntegrityError 回滚（回滚会丢掉本请求已插入的数据）
    existing = {
        (r.plot_code, r.observed_on, r.raw_species_name)
        for r in db.scalars(select(Observation).where(
            Observation.batch_id == batch_id))
    }
    seen_in_request: set[tuple[str, str, str]] = set()
    inserted = duplicates = 0
    for item in items:
        key = (
            item["plot_code"].strip(),
            item["observed_on"].isoformat()
            if hasattr(item["observed_on"], "isoformat")
            else str(item["observed_on"]),
            item["raw_species_name"].strip(),
        )
        if key in existing or key in seen_in_request:
            duplicates += 1
            continue
        seen_in_request.add(key)
        db.add(Observation(
            batch_id=batch_id,
            plot_code=key[0],
            observed_on=key[1],
            raw_species_name=key[2],
            quantity=item["quantity"],
        ))
        inserted += 1
    db.commit()
    return UploadResultOut(
        received=len(items), inserted=inserted, duplicates_skipped=duplicates
    )


@router.post("/batches/{batch_id}/observations",
             response_model=UploadResultOut, status_code=201,
             summary="上传观测记录（JSON 批量）")
def upload_observations(batch_id: int, payload: ObservationBulkIn,
                        db: Session = Depends(get_db)) -> UploadResultOut:
    batch = db.get(SurveyBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"批次 {batch_id} 不存在")
    _check_uploadable(batch)
    return _insert_observations(
        db, batch_id, [o.model_dump() for o in payload.observations])


@router.post("/batches/{batch_id}/observations/csv",
             response_model=UploadResultOut, status_code=201,
             summary="上传观测记录（CSV 文件）")
async def upload_observations_csv(batch_id: int,
                                  file: UploadFile = File(...),
                                  db: Session = Depends(get_db)) -> UploadResultOut:
    batch = db.get(SurveyBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"批次 {batch_id} 不存在")
    _check_uploadable(batch)
    try:
        items = parse_observation_csv(await file.read())
    except CSVParseError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    # 走 Pydantic 做日期与正整数校验
    try:
        validated = ObservationBulkIn(
            observations=[ObservationIn(**it) for it in items]
        )
    except Exception as exc:  # ValidationError
        raise HTTPException(422, detail=f"CSV 数据校验失败: {exc}") from exc
    return _insert_observations(
        db, batch_id, [o.model_dump() for o in validated.observations])


@router.get("/batches/{batch_id}/observations",
            response_model=list[ObservationOut], summary="查看批次原始观测")
def list_observations(batch_id: int,
                      db: Session = Depends(get_db)) -> list[Observation]:
    batch = db.get(SurveyBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"批次 {batch_id} 不存在")
    return list(db.scalars(
        select(Observation).where(Observation.batch_id == batch_id)
        .order_by(Observation.id)))
