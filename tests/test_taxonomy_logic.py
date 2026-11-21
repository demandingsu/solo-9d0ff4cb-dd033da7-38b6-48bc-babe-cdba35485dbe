"""分类表解析纯函数单元测试：异名、未知名、同名歧义、环路、悬挂引用。"""
from __future__ import annotations

from types import SimpleNamespace

from app.taxonomy import (
    AMBIGUOUS,
    CYCLE,
    UNKNOWN,
    build_name_index,
    resolve_all,
    resolve_name,
)


def _row(name, accepted, synonyms=None):
    import json
    return SimpleNamespace(
        name=name,
        accepted_name=accepted,
        synonyms_json=json.dumps(synonyms or [], ensure_ascii=False),
    )


def test_synonym_and_accepted_chain_resolves_to_accepted_name():
    rows = [
        _row("喜鹊", "Pica pica", ["Pica serica"]),
        _row("Pica pica", "Pica pica"),
    ]
    idx = build_name_index(rows)
    assert resolve_name("喜鹊", idx).accepted_name == "Pica pica"
    assert resolve_name("Pica serica", idx).accepted_name == "Pica pica"
    # 大小写/空白不敏感
    assert resolve_name("  pica serica ", idx).accepted_name == "Pica pica"


def test_unknown_name_is_flagged():
    idx = build_name_index([_row("喜鹊", "Pica pica")])
    res = resolve_name("火星潜鸟", idx)
    assert res.issue_type == UNKNOWN
    assert res.accepted_name is None


def test_ambiguous_homonym_is_flagged():
    rows = [
        _row("同名鸟", "Accepted A"),
        _row("同名鸟", "Accepted B"),
    ]
    idx = build_name_index(rows)
    res = resolve_name("同名鸟", idx)
    assert res.issue_type == AMBIGUOUS
    assert set(res.candidates) == {"Accepted A", "Accepted B"}


def test_directed_cycle_is_flagged():
    rows = [
        _row("甲", "乙"),
        _row("乙", "丙"),
        _row("丙", "甲"),
    ]
    idx = build_name_index(rows)
    res = resolve_name("甲", idx)
    assert res.issue_type == CYCLE
    assert "乙 -> 丙 -> 甲 -> 乙" in res.detail


def test_self_loop_is_stable_accepted_name():
    idx = build_name_index([_row("Pica pica", "Pica pica")])
    res = resolve_name("Pica pica", idx)
    assert res.issue_type is None
    assert res.accepted_name == "Pica pica"


def test_dangling_accepted_name_uses_chain_tail():
    # 表中只有“俗名 -> 学名”，学名本身没有对应行
    idx = build_name_index([_row("俗名", "Nomen novum")])
    res = resolve_name("俗名", idx)
    assert res.issue_type is None
    assert res.accepted_name == "Nomen novum"


def test_resolve_all_caches_repeated_names():
    idx = build_name_index([_row("喜鹊", "Pica pica")])
    results = resolve_all(["喜鹊", "喜鹊", "未知"], idx)
    assert len(results) == 3
    assert results[0] is results[1]  # 同一缓存对象
    assert results[2].issue_type == UNKNOWN
