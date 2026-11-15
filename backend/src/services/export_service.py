"""导出服务：归并簇 / 挂牌明细 / 复核单 / 银行清单 CSV（含两个算法的结果列）。

银行清单（bank-book）是对外口径：只包含在售、完整、已入簇记录支撑的房源簇，
文件头标注覆盖范围（中介数、挂牌/簇区间、生成时间）与待补数量，绝不出现
待补（incomplete）、不合规（invalid）或已撤回（off_market / withdrawn）房源。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import (
    INGEST_STAGE_LABELS,
    LISTING_STATUS_LABELS,
    REVIEW_REASON_LABELS,
    REVIEW_STATUS_LABELS,
    RISK_LEVEL_LABELS,
    RISK_TAG_LABELS,
)
from src.models.agency import Agency
from src.models.listing import Listing
from src.models.property import Property
from src.models.review import Review
from src.utils.csv_export import to_csv, to_csv_document
from src.utils.timeutil import local_stamp, utcnow


def _tags(tags: list[str] | None) -> str:
    return " / ".join(RISK_TAG_LABELS.get(tag, tag) for tag in (tags or []))


def _stage_coverage(db: Session) -> dict:
    """采集分诊与撤回的全量计数（银行清单口径与总览共用）。"""
    agencies = list(db.scalars(select(Agency).order_by(Agency.id)).all())
    listings = list(db.scalars(select(Listing)).all())
    covered_codes = sorted({agencies_map_get(agencies, listing.agency_id) for listing in listings})
    covered_codes = [code for code in covered_codes if code]
    pending = sum(1 for listing in listings if listing.ingest_stage == "incomplete")
    invalid = sum(1 for listing in listings if listing.ingest_stage == "invalid")
    withdrawn = sum(1 for listing in listings if listing.withdrawn_at is not None or listing.status == "off_market")
    eligible = sum(
        1
        for listing in listings
        if listing.ingest_stage == "complete" and listing.status == "active" and listing.withdrawn_at is None
    )
    return {
        "agency_count": len(covered_codes),
        "agency_codes": covered_codes,
        "listing_total": len(listings),
        "eligible": eligible,
        "incomplete": pending,
        "invalid": invalid,
        "withdrawn": withdrawn,
    }


def agencies_map_get(agencies: list[Agency], agency_id: int | None) -> str:
    for agency in agencies:
        if agency.id == agency_id:
            return agency.code
    return ""


def _bank_eligible_properties(db: Session) -> list[Property]:
    """银行清单可选簇：簇内至少有一条在售、完整、未撤回的成员。"""
    props = list(db.scalars(select(Property).order_by(Property.id)).all())
    eligible: list[Property] = []
    for prop in props:
        member = db.scalar(
            select(Listing).where(
                Listing.property_id == prop.id,
                Listing.ingest_stage == "complete",
                Listing.status == "active",
                Listing.withdrawn_at.is_(None),
            ).limit(1)
        )
        if member is not None:
            eligible.append(prop)
    return eligible


def bank_book_csv(db: Session) -> bytes:
    """对外银行清单：元数据头 + 仅合规在售房源簇，覆盖范围与待补数量显式标注。"""
    coverage = _stage_coverage(db)
    props = _bank_eligible_properties(db)
    agencies = {agency.id: agency for agency in db.scalars(select(Agency)).all()}
    stamp = local_stamp(utcnow())
    header_lines = [
        ["# 银行抵押估价参考清单（对外）"],
        [f"# 生成时间：{stamp}"],
        [
            "# 覆盖范围：",
            f"{coverage['agency_count']} 家中介（{'/'.join(coverage['agency_codes'])}）",
            f"原始挂牌 {coverage['listing_total']} 条",
            f"合规在售 {coverage['eligible']} 条",
            f"房源簇 {len(props)} 个",
        ],
        [
            "# 待补与排除：",
            f"待补 {coverage['incomplete']} 条",
            f"不合规 {coverage['invalid']} 条",
            f"已撤回/下架 {coverage['withdrawn']} 条（均不在本清单内）",
        ],
        ["# 口径：仅收录完整、在售、未撤回且已通过归并的房源；价格为簇内中位、面积为众数。"],
        [],
    ]
    headers = [
        "清单编号",
        "小区",
        "户型",
        "面积众数(㎡)",
        "价格中位(元)",
        "单价(元/㎡)",
        "成员挂牌数",
        "在售成员数",
        "中介数",
        "价格极差(万元)",
        "离散度",
        "风险等级",
        "风险标签",
        "更新时间",
    ]
    rows = []
    for prop in props:
        active_members = db.scalars(
            select(Listing).where(
                Listing.property_id == prop.id,
                Listing.ingest_stage == "complete",
                Listing.status == "active",
                Listing.withdrawn_at.is_(None),
            )
        ).all()
        agency_codes = sorted({agencies[m.agency_id].code for m in active_members if m.agency_id in agencies})
        unit_price = round(prop.price_median / prop.area_mode) if prop.area_mode and prop.price_median else ""
        rows.append(
            [
                prop.id,
                prop.community,
                prop.layout,
                f"{prop.area_mode:g}",
                prop.price_median,
                unit_price,
                prop.member_count,
                len(active_members),
                f"{prop.agency_count}（{'/'.join(agency_codes)}）",
                f"{prop.spread / 10000:.1f}",
                f"{prop.dispersion:.4f}",
                RISK_LEVEL_LABELS.get(prop.risk_level, prop.risk_level),
                _tags(prop.risk_tags) or "-",
                local_stamp(prop.updated_at),
            ]
        )
    return to_csv_document([*header_lines, headers, *rows])


def clusters_csv(db: Session) -> bytes:
    """归并簇导出：仅合规在售簇，附覆盖范围与待补数量口径说明行。"""
    coverage = _stage_coverage(db)
    props = _bank_eligible_properties(db)
    stamp = local_stamp(utcnow())
    meta = [
        ["# 归并簇清单（内部口径）"],
        [f"# 生成时间：{stamp}"],
        [
            "# 覆盖范围：",
            f"{coverage['agency_count']} 家中介（{'/'.join(coverage['agency_codes'])}）",
            f"原始挂牌 {coverage['listing_total']} 条",
            f"合规在售 {coverage['eligible']} 条",
            f"房源簇 {len(props)} 个",
        ],
        [
            "# 待补数量：",
            f"待补 {coverage['incomplete']} 条",
            f"不合规 {coverage['invalid']} 条",
            f"已撤回/下架 {coverage['withdrawn']} 条（不入簇、不在本清单）",
        ],
        [],
    ]
    headers = [
        "簇编号",
        "小区",
        "户型",
        "面积众数(㎡)",
        "价格中位(元)",
        "簇置信",
        "成员挂牌数",
        "中介数",
        "价格极差(万元)",
        "离散度",
        "风险分",
        "风险等级",
        "风险标签",
        "钓鱼价挂牌",
        "僵尸挂牌",
        "更新时间",
    ]
    rows = []
    for prop in props:
        detail = prop.risk_detail or {}
        rows.append(
            [
                prop.id,
                prop.community,
                prop.layout,
                f"{prop.area_mode:g}",
                prop.price_median,
                f"{prop.confidence:.3f}",
                prop.member_count,
                prop.agency_count,
                f"{prop.spread / 10000:.1f}",
                f"{prop.dispersion:.4f}",
                prop.risk_score,
                RISK_LEVEL_LABELS.get(prop.risk_level, prop.risk_level),
                _tags(prop.risk_tags),
                " / ".join(detail.get("bait_lids") or []) or "-",
                " / ".join(detail.get("stale_lids") or []) or "-",
                local_stamp(prop.updated_at),
            ]
        )
    return to_csv_document([*meta, headers, *rows])


def listings_csv(db: Session, agency_id: int | None = None) -> bytes:
    """挂牌明细全量导出（内部核对）：原文留档 + 分诊/缺项/批次/撤回列。"""
    stmt = select(Listing)
    if agency_id:
        stmt = stmt.where(Listing.agency_id == agency_id)
    listings = db.scalars(stmt.order_by(Listing.id)).all()
    agencies = {agency.id: agency for agency in db.scalars(select(Agency)).all()}
    props = {prop.id: prop for prop in db.scalars(select(Property)).all()}
    headers = [
        "挂牌编号",
        "中介",
        "采集分诊",
        "阻断缺项",
        "采集批次",
        "小区(原文)",
        "小区(归一)",
        "面积(原文)",
        "面积(㎡)",
        "价格(原文)",
        "价格(元)",
        "楼层(原文)",
        "楼层",
        "朝向(归一)",
        "户型(归一)",
        "状态",
        "撤回时间",
        "挂牌天数",
        "带看次数",
        "所属簇",
        "簇风险分",
        "风险标签",
        "归一标记",
    ]
    rows = []
    for listing in listings:
        prop = props.get(listing.property_id)
        rows.append(
            [
                listing.lid,
                agencies.get(listing.agency_id).name if agencies.get(listing.agency_id) else "",
                INGEST_STAGE_LABELS.get(listing.ingest_stage, listing.ingest_stage),
                " / ".join(listing.defect_flags or []) or "-",
                listing.ingest_batch or "-",
                listing.community_raw,
                listing.community_norm,
                listing.area_raw,
                f"{listing.area_num:g}" if listing.area_num is not None else "",
                listing.price_raw,
                listing.price_num or "",
                listing.floor_raw,
                listing.floor_num or "",
                listing.orientation_norm,
                listing.layout_norm,
                LISTING_STATUS_LABELS.get(listing.status, listing.status),
                local_stamp(listing.withdrawn_at) if listing.withdrawn_at else "",
                listing.listed_days,
                listing.views,
                prop.id if prop else "",
                prop.risk_score if prop else "",
                _tags(listing.risk_tags),
                " / ".join(listing.parse_flags or []),
            ]
        )
    return to_csv(headers, rows)


def reviews_csv(db: Session, status: str | None = None) -> bytes:
    stmt = select(Review)
    if status:
        stmt = stmt.where(Review.status == status)
    reviews = db.scalars(stmt.order_by(Review.id)).all()
    props = {prop.id: prop for prop in db.scalars(select(Property)).all()}
    headers = ["复核号", "小区", "户型", "风险分", "风险等级", "复核原因", "状态", "复核人", "结论备注", "创建时间"]
    rows = []
    for review in reviews:
        prop = props.get(review.property_id)
        rows.append(
            [
                review.id,
                prop.community if prop else "",
                prop.layout if prop else "",
                review.risk_score,
                RISK_LEVEL_LABELS.get(review.severity, review.severity),
                REVIEW_REASON_LABELS.get(review.reason, review.reason),
                REVIEW_STATUS_LABELS.get(review.status, review.status),
                review.reviewer or "",
                review.note or "",
                local_stamp(review.created_at),
            ]
        )
    return to_csv(headers, rows)
