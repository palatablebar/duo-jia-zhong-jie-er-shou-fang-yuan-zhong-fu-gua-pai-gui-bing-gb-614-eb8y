"""归并流水线：归一（②）→ 采集分诊 → 实体解析（③④，仅完整在售记录）→ 风险识别（⑤）→ 落库。

纯后端计算，确定性；同一批原始挂牌两次执行得到完全相同的簇集合与 golden record。
待补（incomplete）、不合规（invalid）与已撤回（off_market / withdrawn_at）记录不参与
blocking / 相似度 / 并查集，原文与归一结果仍全量留档；补正为完整后下次重算自动重新入列。
"""
from __future__ import annotations

import hashlib
import time

from sqlalchemy import func, select

from src.constants.enums import IngestStage
from src.constants.limits import SIMILARITY_THRESHOLD
from src.models.agency import Agency
from src.models.listing import Listing
from src.models.match_run import MatchRun
from src.models.property import Property
from src.models.review import Review
from src.services import entity_resolution, intake_service, normalizer, risk_rules
from src.utils.timeutil import iso_utc, utcnow

LISTING_FIELDS = (
    "community_raw",
    "layout_raw",
    "area_raw",
    "price_raw",
    "floor_raw",
    "orientation_raw",
    "title",
)


def cluster_key_of(agency_code: str, lid: str) -> str:
    return f"{agency_code}:{lid}"


def run_pipeline(db, run: MatchRun, threshold: float = SIMILARITY_THRESHOLD) -> dict:
    started = time.perf_counter()
    listings = list(db.scalars(select(Listing).order_by(Listing.id)).all())
    agencies = {agency.id: agency for agency in db.scalars(select(Agency)).all()}
    if not listings:
        return {
            "metrics": {"listings": 0, "clusters": 0, "multi_clusters": 0, "pairs_scored": 0, "pairs_linked": 0},
            "log": "没有可归并的挂牌：请先采集入库。",
            "status": "partial",
        }

    # 1) 全量刷新归一结果与采集分诊（原文不动，待补/不合规同样留档）
    normalized: list[dict] = []
    for listing in listings:
        raw = {field: getattr(listing, field) for field in LISTING_FIELDS}
        norm = normalizer.normalize_listing(raw)
        intake_service.apply_normalized(listing, norm)
        stage, defects = intake_service.classify(norm)
        listing.ingest_stage = stage
        listing.defect_flags = defects
        normalized.append(norm)

    eligible_indexes = [index for index, listing in enumerate(listings) if intake_service.can_merge(listing)]
    eligible_set = set(eligible_indexes)
    excluded_indexes = [index for index in range(len(listings)) if index not in eligible_set]

    stage_counts = {IngestStage.COMPLETE.value: 0, IngestStage.INCOMPLETE.value: 0, IngestStage.INVALID.value: 0}
    for listing in listings:
        stage_counts[listing.ingest_stage] = stage_counts.get(listing.ingest_stage, 0) + 1
    withdrawn_count = sum(1 for listing in listings if listing.withdrawn_at is not None)

    # 2) 被拦记录一律脱簇、清风险标签，等待补正后重算；
    #    记录本轮开始前已在库且没有簇的挂牌（补正后重新入列的候选）
    previously_unclustered = {listing.id for listing in listings if listing.property_id is None}
    for index in excluded_indexes:
        listings[index].property_id = None
        listings[index].risk_tags = []

    # 3) 只有足以支持归并的记录进入房源簇
    eligible_norms = [normalized[index] for index in eligible_indexes]
    eligible_agency_ids = [listings[index].agency_id for index in eligible_indexes]
    result = entity_resolution.resolve(eligible_norms, eligible_agency_ids, threshold=threshold)

    # 算法输出的成员索引是 eligible_norms 序列内的下标，映射回 listings 全量下标
    index_map = eligible_indexes

    existing = {prop.cluster_key: prop for prop in db.scalars(select(Property)).all()}
    seen_keys: set[str] = set()
    baits = stales = risk_high = risk_medium = 0
    readmitted = 0

    for cluster in result["clusters"]:
        members = [index_map[position] for position in cluster["members"]]
        member_lids = sorted(
            cluster_key_of(agencies[listings[index].agency_id].code, listings[index].lid) for index in members
        )
        key = hashlib.sha256("|".join(member_lids).encode("utf-8")).hexdigest()[:40]
        seen_keys.add(key)
        golden = cluster["golden"]
        member_rows = [
            {
                "id": listings[index].id,
                "lid": listings[index].lid,
                "price_num": normalized[index]["price_num"],
                "listed_days": listings[index].listed_days,
                "views": listings[index].views,
                "status": listings[index].status,
            }
            for index in members
        ]
        risk = risk_rules.scan_cluster(member_rows, golden)

        prop = existing.get(key)
        if prop is None:
            prop = Property(cluster_key=key)
            db.add(prop)
        # 重新入列：本轮簇里有上一轮被拦脱簇的挂牌（补正后重新处理）
        if any(listings[index].id in previously_unclustered for index in members):
            readmitted += 1
        prop.community = golden["community"]
        prop.layout = golden["layout"]
        prop.area_mode = golden["area_mode"]
        prop.area_min = golden["area_min"]
        prop.area_max = golden["area_max"]
        prop.price_median = golden["price_median"]
        prop.price_min = golden["price_min"]
        prop.price_max = golden["price_max"]
        prop.price_p10 = golden["price_p10"]
        prop.price_p90 = golden["price_p90"]
        prop.spread = golden["spread"]
        prop.dispersion = golden["dispersion"]
        prop.agency_count = golden["agency_count"]
        prop.member_count = golden["member_count"]
        prop.confidence = cluster["confidence"]
        prop.risk_score = risk["risk_score"]
        prop.risk_level = risk["risk_level"]
        prop.risk_tags = risk["risk_tags"]
        prop.risk_detail = risk["risk_detail"]
        prop.run_id = run.id
        prop.updated_at = utcnow()
        db.flush()

        bait_lids = set(risk["risk_detail"]["bait_lids"])
        stale_lids = set(risk["risk_detail"]["stale_lids"])
        for index in members:
            listing = listings[index]
            tags: list[str] = []
            if listing.lid in bait_lids:
                tags.append("bait")
            if listing.lid in stale_lids:
                tags.append("stale")
            listing.property_id = prop.id
            listing.risk_tags = tags
        baits += len(bait_lids)
        stales += len(stale_lids)
        if prop.risk_level == "high":
            risk_high += 1
        elif prop.risk_level == "medium":
            risk_medium += 1

    # 先落稳本轮全部簇与成员归属，再剪旧簇：autoflush=False 下避免剪枝查询读到过期 property_id
    db.flush()
    pruned = _prune_orphans(db, seen_keys)

    algo_metrics = dict(result["metrics"])
    metrics = {
        "listings": len(listings),
        "eligible": len(eligible_indexes),
        "excluded": len(excluded_indexes),
        "complete": stage_counts.get(IngestStage.COMPLETE.value, 0),
        "incomplete": stage_counts.get(IngestStage.INCOMPLETE.value, 0),
        "invalid": stage_counts.get(IngestStage.INVALID.value, 0),
        "withdrawn": withdrawn_count,
        "readmitted": readmitted,
        "clusters": algo_metrics["clusters"],
        "multi_clusters": algo_metrics["multi_clusters"],
        "pairs_scored": algo_metrics["pairs_scored"],
        "pairs_linked": algo_metrics["pairs_linked"],
        "threshold": threshold,
        "dedup_rate": round(1 - algo_metrics["clusters"] / len(eligible_indexes), 4) if eligible_indexes else 0.0,
        "baits": baits,
        "stales": stales,
        "risk_high": risk_high,
        "risk_medium": risk_medium,
        "risk_total": risk_high + risk_medium,
        "pruned_clusters": pruned,
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }
    log_lines = [
        f"采集挂牌 {metrics['listings']} 条：完整 {metrics['complete']} / 待补 {metrics['incomplete']} / 不合规 {metrics['invalid']}，撤回 {withdrawn_count} 条",
        f"分诊闸口拦截 {metrics['excluded']} 条（不入簇、原文留档），参与归并 {metrics['eligible']} 条，补正后重新入列 {readmitted} 条",
        f"blocking 候选对 {metrics['pairs_scored']} 组，阈值 {threshold:.2f} 连边 {metrics['pairs_linked']} 条",
        f"并查集 + 传递闭包得到 {metrics['clusters']} 个簇，其中多成员簇 {metrics['multi_clusters']} 个，去重率 {metrics['dedup_rate']:.2%}",
        f"算法 B：钓鱼价 {baits} 条、僵尸挂牌 {stales} 条，高风险簇 {risk_high} 个、中风险簇 {risk_medium} 个",
        f"淘汰旧簇 {pruned} 个；耗时 {metrics['duration_ms']} ms",
    ]
    return {"metrics": metrics, "log": "\n".join(log_lines), "status": "success"}


def _prune_orphans(db, seen_keys: set[str]) -> int:
    """删掉本次重算不再产生、且没有复核单的旧簇（有复核单的保留，避免丢历史结论）。"""
    removed = 0
    for prop in db.scalars(select(Property)).all():
        if prop.cluster_key in seen_keys:
            continue
        reviews = db.scalar(select(func.count()).select_from(Review).where(Review.property_id == prop.id)) or 0
        if reviews:
            continue
        for listing in db.scalars(select(Listing).where(Listing.property_id == prop.id)).all():
            listing.property_id = None
        db.delete(prop)
        removed += 1
    db.flush()
    return removed


def last_run_stamp(db) -> str | None:
    run = db.scalar(select(MatchRun).order_by(MatchRun.id.desc()).limit(1))
    return iso_utc(run.finished_at) if run else None
