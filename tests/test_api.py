"""端到端 API 集成测试（FastAPI TestClient + 临时 SQLite）。"""
from __future__ import annotations

import io

import pytest


V1_TAXONOMY = {
    "note": "v1",
    "rows": [
        {"name": "喜鹊", "higher_taxon": "雀形目",
         "accepted_name": "Pica pica", "synonyms": ["Pica serica"]},
        {"name": "Pica pica", "higher_taxon": "雀形目",
         "accepted_name": "Pica pica", "synonyms": []},
        {"name": "树麻雀", "higher_taxon": "雀形目",
         "accepted_name": "Passer montanus", "synonyms": ["麻雀"]},
        {"name": "Passer montanus", "higher_taxon": "雀形目",
         "accepted_name": "Passer montanus", "synonyms": []},
    ],
}

V2_TAXONOMY = {
    "note": "v2 修订麻雀接受名",
    "rows": [
        {"name": "喜鹊", "higher_taxon": "雀形目",
         "accepted_name": "Pica pica", "synonyms": ["Pica serica"]},
        {"name": "Pica pica", "higher_taxon": "雀形目",
         "accepted_name": "Pica pica", "synonyms": []},
        {"name": "树麻雀", "higher_taxon": "雀形目",
         "accepted_name": "Passer chinensis", "synonyms": ["麻雀"]},
        {"name": "Passer chinensis", "higher_taxon": "雀形目",
         "accepted_name": "Passer chinensis", "synonyms": []},
    ],
}


def _create_taxonomy(client, payload):
    r = client.post("/taxonomy/versions", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _create_batch(client, name="批次1"):
    r = client.post("/batches", json={"name": name, "investigator": "测试员"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _upload_obs(client, batch_id, rows):
    r = client.post(f"/batches/{batch_id}/observations",
                    json={"observations": rows})
    assert r.status_code == 201, r.text
    return r.json()


def test_full_publish_and_aggregation_flow(client):
    vid = _create_taxonomy(client, V1_TAXONOMY)["id"]
    bid = _create_batch(client)
    res = _upload_obs(client, bid, [
        {"plot_code": "P-01", "observed_on": "2026-11-01",
         "raw_species_name": "喜鹊", "quantity": 3},
        {"plot_code": "P-01", "observed_on": "2026-11-01",
         "raw_species_name": "Pica serica", "quantity": 2},
        {"plot_code": "P-01", "observed_on": "2026-11-02",
         "raw_species_name": "麻雀", "quantity": 5},
        {"plot_code": "P-02", "observed_on": "2026-11-01",
         "raw_species_name": "树麻雀", "quantity": 7},
    ])
    assert res["inserted"] == 4

    r = client.post(f"/batches/{bid}/publish")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["status"] == "PUBLISHED"
    assert body["taxonomy_version_id"] == vid

    r = client.get(f"/batches/{bid}/results")
    results = r.json()
    # 喜鹊 + 异名在同样地同接受种汇总：3+2=5
    magpie = next(a for a in results["aggregated"]
                  if a["accepted_name"] == "Pica pica")
    assert magpie["total_quantity"] == 5
    assert set(magpie["raw_names"]) == {"喜鹊", "Pica serica"}
    # 树麻雀两条（异名“麻雀”+ 正名）按样地分两组
    sparrow_groups = [a for a in results["aggregated"]
                      if a["accepted_name"] == "Passer montanus"]
    assert {g["plot_code"] for g in sparrow_groups} == {"P-01", "P-02"}
    # 每条解析结果都带分类表版本
    assert all(rec["taxonomy_version_id"] == vid
               for rec in results["resolved_records"])


def test_duplicate_observations_are_skipped(client):
    _create_taxonomy(client, V1_TAXONOMY)
    bid = _create_batch(client)
    row = {"plot_code": "P-01", "observed_on": "2026-11-01",
           "raw_species_name": "喜鹊", "quantity": 3}
    first = _upload_obs(client, bid, [row])
    second = _upload_obs(client, bid, [row])
    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["duplicates_skipped"] == 1


def test_unknown_name_makes_batch_needs_review_without_results(client):
    _create_taxonomy(client, V1_TAXONOMY)
    bid = _create_batch(client)
    _upload_obs(client, bid, [
        {"plot_code": "P-01", "observed_on": "2026-11-01",
         "raw_species_name": "火星潜鸟", "quantity": 1},
    ])
    r = client.post(f"/batches/{bid}/publish")
    body = r.json()
    assert r.status_code == 200
    assert body["success"] is False
    assert body["status"] == "NEEDS_REVIEW"
    assert body["issues"][0]["issue_type"] == "unknown"

    r = client.get(f"/batches/{bid}/issues")
    assert r.json()[0]["raw_species_name"] == "火星潜鸟"
    # 从未成功发布 → 无结果可查
    r = client.get(f"/batches/{bid}/results")
    assert r.status_code == 409


def test_cycle_makes_batch_needs_review(client):
    payload = {
        "note": "含环路",
        "rows": V1_TAXONOMY["rows"] + [
            {"name": "甲", "higher_taxon": "待查",
             "accepted_name": "乙", "synonyms": []},
            {"name": "乙", "higher_taxon": "待查",
             "accepted_name": "甲", "synonyms": []},
        ],
    }
    _create_taxonomy(client, payload)
    bid = _create_batch(client)
    _upload_obs(client, bid, [
        {"plot_code": "P-09", "observed_on": "2026-11-01",
         "raw_species_name": "甲", "quantity": 1},
        {"plot_code": "P-09", "observed_on": "2026-11-01",
         "raw_species_name": "喜鹊", "quantity": 2},
    ])
    body = client.post(f"/batches/{bid}/publish").json()
    assert body["success"] is False
    assert body["status"] == "NEEDS_REVIEW"
    assert body["issues"][0]["issue_type"] == "cycle"


def test_ambiguous_homonym_makes_batch_needs_review(client):
    payload = {
        "note": "含同名歧义",
        "rows": V1_TAXONOMY["rows"] + [
            {"name": "同名鸟", "higher_taxon": "x",
             "accepted_name": "Accepted A", "synonyms": []},
            {"name": "同名鸟", "higher_taxon": "y",
             "accepted_name": "Accepted B", "synonyms": []},
        ],
    }
    _create_taxonomy(client, payload)
    bid = _create_batch(client)
    _upload_obs(client, bid, [
        {"plot_code": "P-09", "observed_on": "2026-11-01",
         "raw_species_name": "同名鸟", "quantity": 1},
    ])
    body = client.post(f"/batches/{bid}/publish").json()
    assert body["success"] is False
    issue = body["issues"][0]
    assert issue["issue_type"] == "ambiguous"
    assert set(issue["candidates"]) == {"Accepted A", "Accepted B"}


def test_taxonomy_revision_only_stales_affected_batch_and_keeps_old_results(client):
    v1 = _create_taxonomy(client, V1_TAXONOMY)
    # 受影响批次（含麻雀）
    bid_affected = _create_batch(client, "含麻雀批次")
    _upload_obs(client, bid_affected, [
        {"plot_code": "P-01", "observed_on": "2026-11-01",
         "raw_species_name": "麻雀", "quantity": 5},
    ])
    client.post(f"/batches/{bid_affected}/publish")

    # 不受影响批次（只含喜鹊）
    bid_clean = _create_batch(client, "只含喜鹊批次")
    _upload_obs(client, bid_clean, [
        {"plot_code": "P-02", "observed_on": "2026-11-01",
         "raw_species_name": "喜鹊", "quantity": 2},
    ])
    client.post(f"/batches/{bid_clean}/publish")

    # 上传 v2
    r = client.post("/taxonomy/versions", json=V2_TAXONOMY)
    assert r.status_code == 201

    ba = client.get(f"/batches/{bid_affected}").json()
    bc = client.get(f"/batches/{bid_clean}").json()
    assert ba["status"] == "STALE"
    assert bc["status"] == "PUBLISHED"  # 未受影响不过期

    # STALE 批次仍可查询上一份完整结果（旧接受名）
    old = client.get(f"/batches/{bid_affected}/results").json()
    assert old["stale"] is True
    assert old["aggregated"][0]["accepted_name"] == "Passer montanus"

    # 重发布成功 → 结果替换为新接受名
    repub = client.post(f"/batches/{bid_affected}/publish").json()
    assert repub["success"] is True
    new = client.get(f"/batches/{bid_affected}/results").json()
    assert new["stale"] is False
    assert new["aggregated"][0]["accepted_name"] == "Passer chinensis"


def test_failed_republish_keeps_previous_complete_results(client):
    v1 = _create_taxonomy(client, V1_TAXONOMY)
    bid = _create_batch(client)
    _upload_obs(client, bid, [
        {"plot_code": "P-01", "observed_on": "2026-11-01",
         "raw_species_name": "麻雀", "quantity": 4},
    ])
    client.post(f"/batches/{bid}/publish")
    good_results = client.get(f"/batches/{bid}/results").json()
    assert good_results["aggregated"][0]["total_quantity"] == 4

    # 分类表 v2 修改麻雀接受名 → 该批次 STALE，但旧结果保留
    client.post("/taxonomy/versions", json=V2_TAXONOMY)
    assert client.get(f"/batches/{bid}").json()["status"] == "STALE"

    # 用仍能解析的 v1 重发布成功，结果按 v1 重新生成（状态回到 PUBLISHED）
    repub = client.post(
        f"/batches/{bid}/publish",
        params={"taxonomy_version_id": v1["id"]}).json()
    assert repub["success"] is True
    same = client.get(f"/batches/{bid}/results").json()
    assert same["aggregated"][0]["accepted_name"] == "Passer montanus"

    # 再构造一个“曾发布成功 → 修订后待核 → 旧结果保留”的批次：
    # 批次2 正常发布；随后上传含歧义的 v3，用 v3 发布必然失败
    bid2 = _create_batch(client, "第二个批次")
    _upload_obs(client, bid2, [
        {"plot_code": "P-07", "observed_on": "2026-11-01",
         "raw_species_name": "喜鹊", "quantity": 2},
    ])
    client.post(f"/batches/{bid2}/publish")
    v3 = {
        "note": "v3 含同名歧义",
        "rows": V2_TAXONOMY["rows"] + [
            {"name": "喜鹊", "higher_taxon": "雀形目",
             "accepted_name": "Pica serica", "synonyms": []},
        ],
    }
    client.post("/taxonomy/versions", json=v3)
    failed = client.post(f"/batches/{bid2}/publish").json()
    assert failed["success"] is False
    assert failed["status"] == "NEEDS_REVIEW"

    # 失败后继续提供上一份完整结果
    old = client.get(f"/batches/{bid2}/results").json()
    assert old["status"] == "NEEDS_REVIEW"
    assert old["aggregated"][0]["accepted_name"] == "Pica pica"
    assert old["aggregated"][0]["total_quantity"] == 2

    # NEEDS_REVIEW 状态允许补传数据；用回 v1（无歧义）重发布成功后结果替换
    _upload_obs(client, bid2, [
        {"plot_code": "P-07", "observed_on": "2026-11-02",
         "raw_species_name": "麻雀", "quantity": 3},
    ])
    fixed = client.post(
        f"/batches/{bid2}/publish",
        params={"taxonomy_version_id": v1["id"]}).json()
    assert fixed["success"] is True
    final = client.get(f"/batches/{bid2}/results").json()
    assert final["status"] == "PUBLISHED"
    names = {a["accepted_name"] for a in final["aggregated"]}
    assert names == {"Pica pica", "Passer montanus"}


def test_global_results_filters(client):
    _create_taxonomy(client, V1_TAXONOMY)
    bid = _create_batch(client)
    _upload_obs(client, bid, [
        {"plot_code": "P-01", "observed_on": "2026-11-01",
         "raw_species_name": "喜鹊", "quantity": 3},
        {"plot_code": "P-02", "observed_on": "2026-11-01",
         "raw_species_name": "麻雀", "quantity": 6},
    ])
    client.post(f"/batches/{bid}/publish")

    r = client.get("/results", params={"plot_code": "P-01"})
    body = r.json()
    assert body["count"] == 1
    assert body["resolved_records"][0]["plot_code"] == "P-01"

    r = client.get("/results", params={"accepted_name": "Passer montanus"})
    assert r.json()["count"] == 1

    r = client.get("/results", params={"accepted_name": "不存在"})
    assert r.json()["count"] == 0


def test_csv_uploads_work(client):
    csv_tax = (
        "name,higher_taxon,accepted_name,synonyms\n"
        "喜鹊,雀形目,Pica pica,Pica serica;山喳啦\n"
        "Pica pica,雀形目,Pica pica,\n"
    )
    r = client.post(
        "/taxonomy/versions/csv",
        files={"file": ("tax.csv", io.BytesIO(csv_tax.encode()), "text/csv")},
        data={"note": "csv 版本"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["row_count"] == 2

    bid = _create_batch(client, "CSV批次")
    csv_obs = (
        "plot_code,observed_on,raw_species_name,quantity\n"
        "P-01,2026-11-01,喜鹊,3\n"
        "P-01,2026-11-01,山喳啦,2\n"
    )
    r = client.post(
        f"/batches/{bid}/observations/csv",
        files={"file": ("obs.csv", io.BytesIO(csv_obs.encode()), "text/csv")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["inserted"] == 2

    body = client.post(f"/batches/{bid}/publish").json()
    assert body["success"] is True
    results = client.get(f"/batches/{bid}/results").json()
    assert results["aggregated"][0]["total_quantity"] == 5


def test_published_batch_rejects_new_uploads(client):
    _create_taxonomy(client, V1_TAXONOMY)
    bid = _create_batch(client)
    _upload_obs(client, bid, [
        {"plot_code": "P-01", "observed_on": "2026-11-01",
         "raw_species_name": "喜鹊", "quantity": 1},
    ])
    client.post(f"/batches/{bid}/publish")
    r = client.post(f"/batches/{bid}/observations",
                    json={"observations": [
                        {"plot_code": "P-01", "observed_on": "2026-11-02",
                         "raw_species_name": "喜鹊", "quantity": 1}]})
    assert r.status_code == 409


def test_publish_without_taxonomy_or_observations_is_rejected(client):
    # 无分类表
    bid = _create_batch(client)
    r = client.post(f"/batches/{bid}/publish")
    assert r.status_code == 409

    _create_taxonomy(client, V1_TAXONOMY)
    # 有分类表但无观测
    r = client.post(f"/batches/{bid}/publish")
    assert r.status_code == 400

    # 指定不存在的版本
    r = client.post(f"/batches/{bid}/publish",
                    params={"taxonomy_version_id": 999})
    assert r.status_code == 400


# ---------------- 快速过期判定：差异名称定位批次 ----------------

def test_indirect_rename_marks_stale_and_returns_batch_ids(client):
    """间接改名：名称所在行未动，但链上接受名被改，批次必须过期。"""
    _create_taxonomy(client, {
        "note": "v1",
        "rows": [
            {"name": "斑鸠", "higher_taxon": "鸽形目",
             "accepted_name": "Streptopelia chinensis", "synonyms": []},
            {"name": "Streptopelia chinensis", "higher_taxon": "鸽形目",
             "accepted_name": "Streptopelia chinensis", "synonyms": []},
            {"name": "喜鹊", "higher_taxon": "雀形目",
             "accepted_name": "Pica pica", "synonyms": ["Pica serica"]},
            {"name": "Pica pica", "higher_taxon": "雀形目",
             "accepted_name": "Pica pica", "synonyms": []},
        ],
    })
    bid_hit = _create_batch(client, "间接改名批次")
    _upload_obs(client, bid_hit, [
        {"plot_code": "P-01", "observed_on": "2026-11-01",
         "raw_species_name": "斑鸠", "quantity": 2},
    ])
    client.post(f"/batches/{bid_hit}/publish")

    bid_clean = _create_batch(client, "不受影响批次")
    _upload_obs(client, bid_clean, [
        {"plot_code": "P-02", "observed_on": "2026-11-01",
         "raw_species_name": "喜鹊", "quantity": 1},
    ])
    client.post(f"/batches/{bid_clean}/publish")

    # v2：斑鸠的行原样保留，只改链上 Streptopelia chinensis 的接受名
    r = client.post("/taxonomy/versions", json={
        "note": "v2 链上改名",
        "rows": [
            {"name": "斑鸠", "higher_taxon": "鸽形目",
             "accepted_name": "Streptopelia chinensis", "synonyms": []},
            {"name": "Streptopelia chinensis", "higher_taxon": "鸽形目",
             "accepted_name": "Spilopelia chinensis", "synonyms": []},
            {"name": "Spilopelia chinensis", "higher_taxon": "鸽形目",
             "accepted_name": "Spilopelia chinensis", "synonyms": []},
            {"name": "喜鹊", "higher_taxon": "雀形目",
             "accepted_name": "Pica pica", "synonyms": ["Pica serica"]},
            {"name": "Pica pica", "higher_taxon": "雀形目",
             "accepted_name": "Pica pica", "synonyms": []},
        ],
    })
    assert r.status_code == 201
    assert r.json()["stale_batch_ids"] == [bid_hit]
    assert client.get(f"/batches/{bid_hit}").json()["status"] == "STALE"
    assert client.get(f"/batches/{bid_clean}").json()["status"] == "PUBLISHED"
    # 旧结果仍完整可查（旧接受名）
    old = client.get(f"/batches/{bid_hit}/results").json()
    assert old["stale"] is True
    assert old["aggregated"][0]["accepted_name"] == "Streptopelia chinensis"


def test_csv_taxonomy_upload_uses_same_stale_path(client):
    """CSV 上传走同一过期判定路径；库存原始名大小写混杂也能命中。"""
    csv_v1 = (
        "name,higher_taxon,accepted_name,synonyms\n"
        "喜鹊,雀形目,Pica pica,Pica serica\n"
        "Pica pica,雀形目,Pica pica,\n"
    )
    r = client.post(
        "/taxonomy/versions/csv",
        files={"file": ("v1.csv", io.BytesIO(csv_v1.encode()), "text/csv")},
    )
    assert r.status_code == 201, r.text

    bid = _create_batch(client, "CSV过期批次")
    _upload_obs(client, bid, [
        {"plot_code": "P-01", "observed_on": "2026-11-01",
         "raw_species_name": "PICA serica", "quantity": 2},  # 大小写混杂的异名
    ])
    assert client.post(f"/batches/{bid}/publish").json()["success"] is True

    csv_v2 = (
        "name,higher_taxon,accepted_name,synonyms\n"
        "喜鹊,雀形目,Pica nova,Pica serica\n"
        "Pica nova,雀形目,Pica nova,\n"
    )
    r = client.post(
        "/taxonomy/versions/csv",
        files={"file": ("v2.csv", io.BytesIO(csv_v2.encode()), "text/csv")},
        data={"note": "v2 csv"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["stale_batch_ids"] == [bid]
    assert client.get(f"/batches/{bid}").json()["status"] == "STALE"


def test_republish_interleaved_with_version_uploads(client):
    """重发与版本上传交错：判定以上传提交前已经生效的发布数据为准。"""
    v1 = _create_taxonomy(client, {
        "note": "v1",
        "rows": [
            {"name": "喜鹊", "higher_taxon": "雀形目",
             "accepted_name": "Pica pica", "synonyms": []},
            {"name": "Pica pica", "higher_taxon": "雀形目",
             "accepted_name": "Pica pica", "synonyms": []},
            {"name": "麻雀", "higher_taxon": "雀形目",
             "accepted_name": "Passer montanus", "synonyms": []},
            {"name": "Passer montanus", "higher_taxon": "雀形目",
             "accepted_name": "Passer montanus", "synonyms": []},
            {"name": "斑鸠", "higher_taxon": "鸽形目",
             "accepted_name": "Streptopelia chinensis", "synonyms": []},
            {"name": "Streptopelia chinensis", "higher_taxon": "鸽形目",
             "accepted_name": "Streptopelia chinensis", "synonyms": []},
        ],
    })

    def _batch_with(name, raws):
        bid = _create_batch(client, name)
        _upload_obs(client, bid, [
            {"plot_code": "P-01", "observed_on": "2026-11-01",
             "raw_species_name": raw, "quantity": 1}
            for raw in raws
        ])
        assert client.post(f"/batches/{bid}/publish").json()["success"] is True
        return bid

    bid_a = _batch_with("批次A（喜鹊+麻雀）", ["喜鹊", "麻雀"])
    bid_b = _batch_with("批次B（斑鸠）", ["斑鸠"])
    bid_c = _batch_with("批次C（麻雀）", ["麻雀"])

    # v2：麻雀改名，斑鸠整行删除（变得无法解析），喜鹊不动 → 三批全过期
    v2 = _create_taxonomy(client, {
        "note": "v2",
        "rows": [
            {"name": "喜鹊", "higher_taxon": "雀形目",
             "accepted_name": "Pica pica", "synonyms": []},
            {"name": "Pica pica", "higher_taxon": "雀形目",
             "accepted_name": "Pica pica", "synonyms": []},
            {"name": "麻雀", "higher_taxon": "雀形目",
             "accepted_name": "Passer chinensis", "synonyms": []},
            {"name": "Passer chinensis", "higher_taxon": "雀形目",
             "accepted_name": "Passer chinensis", "synonyms": []},
        ],
    })
    assert set(v2["stale_batch_ids"]) == {bid_a, bid_b, bid_c}

    # 交错重发：A、C 成功（名称关联随明细替换为 v2）；B 失败（斑鸠 unknown）
    assert client.post(f"/batches/{bid_a}/publish").json()["success"] is True
    assert client.post(f"/batches/{bid_c}/publish").json()["success"] is True
    failed = client.post(f"/batches/{bid_b}/publish").json()
    assert failed["success"] is False
    assert failed["status"] == "NEEDS_REVIEW"
    # 重发失败：仍按上次成功发布（v1）的明细提供结果
    b_old = client.get(f"/batches/{bid_b}/results").json()
    assert b_old["taxonomy_version_id"] == v1["id"]
    assert b_old["aggregated"][0]["accepted_name"] == "Streptopelia chinensis"

    # v3：喜鹊改名；麻雀保持 v2 的接受名；斑鸠依旧缺失
    v3 = _create_taxonomy(client, {
        "note": "v3",
        "rows": [
            {"name": "喜鹊", "higher_taxon": "雀形目",
             "accepted_name": "Pica nova", "synonyms": []},
            {"name": "Pica nova", "higher_taxon": "雀形目",
             "accepted_name": "Pica nova", "synonyms": []},
            {"name": "麻雀", "higher_taxon": "雀形目",
             "accepted_name": "Passer chinensis", "synonyms": []},
            {"name": "Passer chinensis", "higher_taxon": "雀形目",
             "accepted_name": "Passer chinensis", "synonyms": []},
        ],
    })
    # 只有 A 过期：C 的当前明细基于 v2（麻雀在 v3 未变），若误用其 v1 明细
    # 会误判为过期；B 重发失败后仍引用 v1，不在“仍引用上一版（v2）”范围
    assert v3["stale_batch_ids"] == [bid_a]
    assert client.get(f"/batches/{bid_a}").json()["status"] == "STALE"
    assert client.get(f"/batches/{bid_c}").json()["status"] == "PUBLISHED"
    assert client.get(f"/batches/{bid_b}").json()["status"] == "NEEDS_REVIEW"


def test_version_and_stale_marking_commit_together(client, monkeypatch):
    """过期标记失败时，新版分类表一并回滚（同一事务）。"""
    _create_taxonomy(client, V1_TAXONOMY)
    bid = _create_batch(client)
    _upload_obs(client, bid, [
        {"plot_code": "P-01", "observed_on": "2026-11-01",
         "raw_species_name": "麻雀", "quantity": 1},
    ])
    client.post(f"/batches/{bid}/publish")

    from app import routers_taxonomy

    original = routers_taxonomy.mark_affected_batches_stale

    def _boom(*args, **kwargs):
        original(*args, **kwargs)  # 先真的完成标记，再模拟提交前失败
        raise RuntimeError("模拟过期判定后、提交前失败")

    monkeypatch.setattr(routers_taxonomy, "mark_affected_batches_stale", _boom)
    with pytest.raises(RuntimeError):
        client.post("/taxonomy/versions", json=V2_TAXONOMY)
    # 只恢复本测试的补丁（不能用 monkeypatch.undo()，会误撤夹具的补丁）
    routers_taxonomy.mark_affected_batches_stale = original

    # 新版本未留下，批次状态未被改动（同一事务整体回滚）
    assert len(client.get("/taxonomy/versions").json()) == 1
    assert client.get(f"/batches/{bid}").json()["status"] == "PUBLISHED"
