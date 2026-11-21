"""端到端 API 集成测试（FastAPI TestClient + 临时 SQLite）。"""
from __future__ import annotations

import io


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
