"""分类表接口：版本化上传、列表、详情。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .csv_io import CSVParseError, parse_taxonomy_csv
from .database import get_db
from .models import TaxonomyVersion
from .schemas import TaxonRowOut, TaxonomyUploadIn, TaxonomyVersionOut
from .taxonomy import create_taxonomy_version, mark_affected_batches_stale

router = APIRouter(prefix="/taxonomy", tags=["分类表"])


def _persist_version(db: Session, rows: list[dict], note: str | None) -> dict:
    old = db.scalar(select(TaxonomyVersion).order_by(
        TaxonomyVersion.version.desc()).limit(1))
    version = create_taxonomy_version(db, rows, note)
    affected = mark_affected_batches_stale(db, old, version)
    db.commit()
    db.refresh(version)
    return {
        "id": version.id,
        "version": version.version,
        "note": version.note,
        "created_at": version.created_at,
        "row_count": len(rows),
        "stale_batch_ids": affected,
    }


@router.post("/versions", response_model=TaxonomyVersionOut, status_code=201,
             summary="上传新版分类表（JSON）")
def upload_taxonomy(payload: TaxonomyUploadIn,
                    db: Session = Depends(get_db)) -> dict:
    rows = [r.model_dump() for r in payload.rows]
    return _persist_version(db, rows, payload.note)


@router.post("/versions/csv", response_model=TaxonomyVersionOut, status_code=201,
             summary="上传新版分类表（CSV 文件）")
async def upload_taxonomy_csv(file: UploadFile = File(...),
                              note: str | None = None,
                              db: Session = Depends(get_db)) -> dict:
    content = await file.read()
    try:
        rows = parse_taxonomy_csv(content)
    except CSVParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = _persist_version(db, rows, note)
    return result


@router.get("/versions", response_model=list[TaxonomyVersionOut],
            summary="分类表版本列表")
def list_versions(db: Session = Depends(get_db)) -> list[dict]:
    versions = list(db.scalars(
        select(TaxonomyVersion).order_by(TaxonomyVersion.version.desc())))
    return [
        {
            "id": v.id,
            "version": v.version,
            "note": v.note,
            "created_at": v.created_at,
            "row_count": len(v.rows),
            "stale_batch_ids": [],
        }
        for v in versions
    ]


@router.get("/versions/{version_id}", summary="分类表版本详情（含全部行）")
def get_version(version_id: int, db: Session = Depends(get_db)) -> dict:
    version = db.get(TaxonomyVersion, version_id)
    if version is None:
        raise HTTPException(404, f"分类表版本 {version_id} 不存在")
    rows = []
    for row in version.rows:
        rows.append(TaxonRowOut(
            name=row.name,
            higher_taxon=row.higher_taxon,
            accepted_name=row.accepted_name,
            synonyms=json.loads(row.synonyms_json or "[]"),
        ))
    return {
        "id": version.id,
        "version": version.version,
        "note": version.note,
        "created_at": version.created_at,
        "rows": rows,
    }
