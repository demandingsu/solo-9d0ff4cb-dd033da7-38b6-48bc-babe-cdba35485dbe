"""Pydantic 请求/响应模型（同时作为 /docs 的示例来源）。"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator


# ---------- 分类表 ----------

class TaxonRowIn(BaseModel):
    name: str = Field(..., min_length=1, description="分类名称（接受名或异名所在行的名称）")
    higher_taxon: str | None = Field(None, description="上位类群，例如 雀形目")
    accepted_name: str = Field(..., min_length=1, description="接受名（权威名称）")
    synonyms: list[str] = Field(
        default_factory=list, description="同物异名列表，可选"
    )

    @field_validator("name", "accepted_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("名称不能为空字符串")
        return v

    @field_validator("synonyms")
    @classmethod
    def _strip_synonyms(cls, v: list[str]) -> list[str]:
        return [s.strip() for s in v if s and s.strip()]


class TaxonomyUploadIn(BaseModel):
    note: str | None = Field(None, description="版本说明，可选")
    rows: list[TaxonRowIn] = Field(..., min_length=1)


class TaxonomyVersionOut(BaseModel):
    id: int
    version: int
    note: str | None
    created_at: datetime
    row_count: int
    # 上传该版本后被判定过期（STALE）的批次 id 列表；列表查询时为空
    stale_batch_ids: list[int] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class TaxonRowOut(BaseModel):
    name: str
    higher_taxon: str | None
    accepted_name: str
    synonyms: list[str]


# ---------- 批次与观测 ----------

class BatchCreateIn(BaseModel):
    name: str = Field(..., min_length=1)
    investigator: str | None = None


class BatchOut(BaseModel):
    id: int
    name: str
    investigator: str | None
    status: str
    published_taxonomy_version_id: int | None
    published_at: datetime | None
    last_publish_error: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ObservationIn(BaseModel):
    plot_code: str = Field(..., min_length=1, description="样地编号，例如 P-01")
    observed_on: date = Field(..., description="观测日期（UTC），ISO 8601")
    raw_species_name: str = Field(..., min_length=1, description="原始物种名")
    quantity: int = Field(..., gt=0, description="数量，必须为正整数")

    @field_validator("plot_code", "raw_species_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class ObservationBulkIn(BaseModel):
    observations: list[ObservationIn] = Field(..., min_length=1)


class ObservationOut(BaseModel):
    id: int
    plot_code: str
    observed_on: str
    raw_species_name: str
    quantity: int

    model_config = {"from_attributes": True}


class UploadResultOut(BaseModel):
    received: int
    inserted: int
    duplicates_skipped: int


# ---------- 发布与查询 ----------

class PublishIssueOut(BaseModel):
    issue_type: str
    raw_species_name: str
    detail: str
    candidates: list[str] = Field(default_factory=list)


class PublishResultOut(BaseModel):
    batch_id: int
    success: bool
    status: str
    taxonomy_version_id: int
    message: str
    issues: list[PublishIssueOut] = Field(default_factory=list)


class AggregatedRowOut(BaseModel):
    plot_code: str
    accepted_name: str
    total_quantity: int
    record_count: int
    raw_names: list[str]
    observed_dates: list[str]


class ResolvedRowOut(BaseModel):
    plot_code: str
    accepted_name: str
    raw_species_name: str
    observed_on: str
    quantity: int
    taxonomy_version_id: int


class BatchResultsOut(BaseModel):
    batch_id: int
    batch_name: str
    status: str
    stale: bool
    taxonomy_version_id: int | None
    published_at: datetime | None
    aggregated: list[AggregatedRowOut]
    resolved_records: list[ResolvedRowOut]


class GlobalResultsOut(BaseModel):
    filters: dict[str, str | bool | None]
    count: int
    aggregated: list[AggregatedRowOut]
    resolved_records: list[ResolvedRowOut]
