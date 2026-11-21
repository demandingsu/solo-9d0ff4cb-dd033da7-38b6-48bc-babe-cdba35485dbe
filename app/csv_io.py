"""CSV 上传解析辅助。

分类表 CSV 列：name,higher_taxon,accepted_name,synonyms
（synonyms 用分号分隔多个异名，留空表示无）
观测记录 CSV 列：plot_code,observed_on,raw_species_name,quantity
"""
from __future__ import annotations

import csv
import io


class CSVParseError(ValueError):
    pass


def _decode(content: bytes) -> str:
    return content.decode("utf-8-sig")


def parse_taxonomy_csv(content: bytes) -> list[dict]:
    text = _decode(content)
    reader = csv.DictReader(io.StringIO(text))
    required = {"name", "higher_taxon", "accepted_name", "synonyms"}
    if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
        raise CSVParseError(
            f"分类表 CSV 必须包含列: {sorted(required)}，实际: {reader.fieldnames}"
        )
    rows: list[dict] = []
    for i, raw in enumerate(reader, start=2):  # 表头是第 1 行
        name = (raw.get("name") or "").strip()
        accepted = (raw.get("accepted_name") or "").strip()
        if not name or not accepted:
            raise CSVParseError(f"第 {i} 行 name/accepted_name 不能为空")
        synonyms = [
            s.strip() for s in (raw.get("synonyms") or "").split(";") if s.strip()
        ]
        rows.append({
            "name": name,
            "higher_taxon": (raw.get("higher_taxon") or "").strip() or None,
            "accepted_name": accepted,
            "synonyms": synonyms,
        })
    if not rows:
        raise CSVParseError("CSV 没有数据行")
    return rows


def parse_observation_csv(content: bytes) -> list[dict]:
    text = _decode(content)
    reader = csv.DictReader(io.StringIO(text))
    required = {"plot_code", "observed_on", "raw_species_name", "quantity"}
    if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
        raise CSVParseError(
            f"观测记录 CSV 必须包含列: {sorted(required)}，实际: {reader.fieldnames}"
        )
    rows: list[dict] = []
    for i, raw in enumerate(reader, start=2):
        try:
            quantity = int((raw.get("quantity") or "").strip())
        except ValueError as exc:
            raise CSVParseError(f"第 {i} 行 quantity 必须是整数") from exc
        rows.append({
            "plot_code": (raw.get("plot_code") or "").strip(),
            "observed_on": (raw.get("observed_on") or "").strip(),
            "raw_species_name": (raw.get("raw_species_name") or "").strip(),
            "quantity": quantity,
        })
    if not rows:
        raise CSVParseError("CSV 没有数据行")
    return rows
