"""ORM 模型。

设计要点：
- 分类表（TaxonomyVersion / TaxonRow）是不可变的版本快照，修订即新增版本。
- 批次（SurveyBatch）有 DRAFT / PUBLISHED / STALE / NEEDS_REVIEW 四种状态。
- 观测记录（Observation）是研究员上传的原始数据。
- 解析结果（ResolvedObservation）只在整批发布成功时才写入，
  失败时旧结果保持不动，因此接口始终能返回上一份完整版本。
- 发布问题（PublishIssue）记录最近一次发布尝试的全部核校问题。
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BatchStatus(str, enum.Enum):
    DRAFT = "DRAFT"                      # 草稿，从未成功发布
    PUBLISHED = "PUBLISHED"              # 发布成功，结果有效
    STALE = "STALE"                      # 分类表修订后旧结果已过期，仍可查询
    NEEDS_REVIEW = "NEEDS_REVIEW"        # 最近一次发布失败，等待人工核校


class TaxonomyVersion(Base):
    __tablename__ = "taxonomy_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    rows: Mapped[list["TaxonRow"]] = relationship(
        back_populates="taxonomy",
        cascade="all, delete-orphan",
    )


class TaxonRow(Base):
    """分类表中的一行：名称、上位类群、接受名、同物异名（JSON 数组字符串）。"""

    __tablename__ = "taxon_rows"

    id: Mapped[int] = mapped_column(primary_key=True)
    taxonomy_version_id: Mapped[int] = mapped_column(
        ForeignKey("taxonomy_versions.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255), index=True)
    higher_taxon: Mapped[str | None] = mapped_column(String(255), nullable=True)
    accepted_name: Mapped[str] = mapped_column(String(255), index=True)
    synonyms_json: Mapped[str] = mapped_column(Text, default="[]")

    taxonomy: Mapped[TaxonomyVersion] = relationship(back_populates="rows")


class SurveyBatch(Base):
    __tablename__ = "survey_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    investigator: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[BatchStatus] = mapped_column(
        Enum(BatchStatus), default=BatchStatus.DRAFT, index=True
    )
    # 最近一次发布成功所使用的分类表版本；从未成功发布则为 None
    published_taxonomy_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("taxonomy_versions.id"), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_publish_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    observations: Mapped[list["Observation"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )
    resolved: Mapped[list["ResolvedObservation"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )
    issues: Mapped[list["PublishIssue"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class Observation(Base):
    """研究员上传的原始观测记录。"""

    __tablename__ = "observations"
    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "plot_code",
            "observed_on",
            "raw_species_name",
            name="uq_observation_raw",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("survey_batches.id", ondelete="CASCADE"), index=True
    )
    plot_code: Mapped[str] = mapped_column(String(100), index=True)
    observed_on: Mapped[str] = mapped_column(String(10))  # ISO 日期，UTC
    raw_species_name: Mapped[str] = mapped_column(String(255), index=True)
    quantity: Mapped[int] = mapped_column(Integer)

    batch: Mapped[SurveyBatch] = relationship(back_populates="observations")


class ResolvedObservation(Base):
    """发布成功后落库的解析结果，保留原名与分类表版本。"""

    __tablename__ = "resolved_observations"
    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "plot_code",
            "accepted_name",
            "raw_species_name",
            "observed_on",
            name="uq_resolved",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("survey_batches.id", ondelete="CASCADE"), index=True
    )
    taxonomy_version_id: Mapped[int] = mapped_column(
        ForeignKey("taxonomy_versions.id")
    )
    plot_code: Mapped[str] = mapped_column(String(100), index=True)
    accepted_name: Mapped[str] = mapped_column(String(255), index=True)
    raw_species_name: Mapped[str] = mapped_column(String(255))
    observed_on: Mapped[str] = mapped_column(String(10))
    quantity: Mapped[int] = mapped_column(Integer)

    batch: Mapped[SurveyBatch] = relationship(back_populates="resolved")


class PublishIssue(Base):
    """最近一次发布尝试产生的核校问题（整批待核的明细）。"""

    __tablename__ = "publish_issues"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("survey_batches.id", ondelete="CASCADE"), index=True
    )
    issue_type: Mapped[str] = mapped_column(String(32))  # cycle/unknown/ambiguous
    raw_species_name: Mapped[str] = mapped_column(String(255))
    detail: Mapped[str] = mapped_column(Text)
    candidates_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    batch: Mapped[SurveyBatch] = relationship(back_populates="issues")
