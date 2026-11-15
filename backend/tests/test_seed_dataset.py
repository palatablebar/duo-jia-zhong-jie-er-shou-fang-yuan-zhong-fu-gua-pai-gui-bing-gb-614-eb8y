"""业务验收：seed 数据集经采集分诊 + 算法 A/B 之后的口径必须与设计一致。

分诊闸口（intake_service.can_merge）：只有 complete 且未撤回/下架的记录进入房源簇；
待补（价格面议、面积待核实）与不合规（面积越界、小区缺失）记录原文留档但不入簇。
"""
from __future__ import annotations

from collections import Counter

from scripts.demo_config import AGENCIES, build_listings
from src.services import entity_resolution, intake_service, normalizer, risk_rules


def _resolve():
    clusters, listings = build_listings()
    true_cluster = {}
    for cluster in clusters:
        for member in cluster["members"]:
            true_cluster[member["lid"]] = cluster["index"]
    norms = [normalizer.normalize_listing(row) for row in listings]
    stages = [intake_service.classify(norm) for norm in norms]
    eligible = [
        index
        for index, row in enumerate(listings)
        if stages[index][0] == "complete" and row["status"] != "off_market"
    ]
    agency_ids = [
        AGENCIES.index(next(a for a in AGENCIES if a["code"] == listings[index]["agency_code"]))
        for index in eligible
    ]
    result = entity_resolution.resolve([norms[index] for index in eligible], agency_ids)
    return clusters, listings, norms, stages, eligible, result, true_cluster


def test_dataset_shape():
    _clusters, listings, _norms, _stages, _eligible, _result, _truth = _resolve()
    assert len(listings) == 540
    assert sorted(Counter(row["agency_code"] for row in listings).values()) == [60] * 9


def test_ingest_stage_split():
    _clusters, listings, norms, stages, eligible, _result, _truth = _resolve()
    stage_counter = Counter(stage for stage, _ in stages)
    assert stage_counter["complete"] == 536
    assert stage_counter["incomplete"] == 2  # 价格面议 + 面积待核实
    assert stage_counter["invalid"] == 2  # 面积越界 + 小区缺失
    # 待补缺项
    pending = {listings[index]["lid"]: stages[index][1] for index in range(len(listings)) if stages[index][0] == "incomplete"}
    assert all("price_missing" in flags or "area_missing" in flags for flags in pending.values())
    # 不合规缺项
    invalid_flags = [flags for stage, flags in stages if stage == "invalid"]
    assert any("area_out_of_range" in flags for flags in invalid_flags)
    assert any("community_missing" in flags for flags in invalid_flags)
    # 只有完整记录参与归并
    assert len(eligible) == 536
    assert all(stages[index][0] == "complete" for index in eligible)


def test_seed_dataset_clusters():
    _clusters, listings, _norms, _stages, eligible, result, truth = _resolve()
    assert result["metrics"]["listings"] == 536
    assert result["metrics"]["clusters"] == 382
    assert result["metrics"]["multi_clusters"] == 41
    # 每个预测簇都只含同一套真值房源（缺项没有把相邻户型串成一套）
    for cluster in result["clusters"]:
        assert len({truth[listings[eligible[index]]["lid"]] for index in cluster["members"]}) == 1
    # 4 条被拦记录没有对应任何簇
    assert len(eligible) + 4 == len(listings)


def test_excluded_listings_never_block_into_clusters():
    _clusters, listings, _norms, stages, eligible, result, _truth = _resolve()
    blocked_lids = {listings[index]["lid"] for index in range(len(listings)) if index not in eligible}
    assert len(blocked_lids) == 4
    clustered_lids = {
        listings[eligible[index]]["lid"]
        for cluster in result["clusters"]
        for index in cluster["members"]
    }
    assert blocked_lids.isdisjoint(clustered_lids)


def test_special_cluster_cross_agency():
    _clusters, _listings, _norms, _stages, _eligible, result, _truth = _resolve()
    matched = [
        cluster
        for cluster in result["clusters"]
        if cluster["golden"]["layout"] == "3-2-1" and abs(cluster["golden"]["area_mode"] - 89.5) < 1e-9
    ]
    assert matched, "应有 89.5㎡ 的 3-2-1 簇"
    golden = matched[0]["golden"]
    assert golden["community"] == "翠湖名邸"
    assert golden["member_count"] == 4
    assert golden["agency_count"] == 4
    assert golden["spread"] == 120_000
    assert golden["price_median"] == 6_040_000


def test_seed_dataset_risk_profile():
    _clusters, listings, norms, _stages, eligible, result, _truth = _resolve()
    risky = []
    baits = stales = 0
    for cluster in result["clusters"]:
        members = [
            {
                "id": position,
                "lid": listings[eligible[position]]["lid"],
                "price_num": norms[eligible[position]]["price_num"],
                "listed_days": listings[eligible[position]]["listed_days"],
                "views": listings[eligible[position]]["views"],
                "status": listings[eligible[position]]["status"],
            }
            for position in cluster["members"]
        ]
        risk = risk_rules.scan_cluster(members, cluster["golden"])
        if risk["risk_level"] in {"high", "medium"}:
            risky.append(risk)
        baits += len(risk["risk_detail"]["bait_lids"])
        stales += len(risk["risk_detail"]["stale_lids"])
    assert len(risky) == 6, len(risky)
    assert sum(1 for risk in risky if "bait" in risk["risk_tags"]) == 1
    assert baits == 2
    assert stales == 5
    assert max(risk["risk_score"] for risk in risky) == 65
