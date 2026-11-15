"""采集分诊与补正服务（业务模块 ① 的质量闸口）。

三类分诊（纯函数，确定性）：
  * complete   完整：关键字段（小区/户型/面积/价格）全部可解析且在合理区间，可进入房源簇；
  * incomplete 待补：房源可定位，但面积或价格缺失（如「价格面议」「面积待核实」），补齐后可重入；
  * invalid    不合规：小区/户型无法解析，或面积越界，无法定位具体房源，退回来源渠道整改。

原文全部留档：``Listing.raw_payload`` 保存采集时全量快照，补正只改工作字段并写
``listing_corrections``，永不覆盖原文。批次按自然键 ``batch_no`` 幂等，同批重试不新增记录。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    BATCH_STATUS_LABELS,
    CORRECTION_STATUS_LABELS,
    INGEST_STAGE_LABELS,
    IngestStage,
)
from src.models.agency import Agency
from src.models.ingest_batch import IngestBatch
from src.models.listing import Listing
from src.models.listing_correction import ListingCorrection
from src.services import normalizer
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_utc, utcnow

# 阻断入簇的缺项：按「不合规 / 待补」两档归类
INVALID_FLAGS = frozenset(
    {
        "community_missing",
        "layout_missing",
        "layout_unparsed",
        "area_out_of_range",
    }
)
INCOMPLETE_FLAGS = frozenset({"area_missing", "price_missing"})

# 允许补正的工作字段（原文留档列之外的归一输入）
CORRECTABLE_FIELDS = (
    "community_raw",
    "layout_raw",
    "area_raw",
    "price_raw",
    "floor_raw",
    "orientation_raw",
    "title",
)

_RAW_SNAPSHOT_FIELDS = (
    "community_raw",
    "layout_raw",
    "area_raw",
    "price_raw",
    "floor_raw",
    "orientation_raw",
    "title",
)


def classify(norm: dict) -> tuple[str, list[str]]:
    """根据归一结果给出分诊档位与阻断缺项（纯函数）。"""
    flags = list(norm.get("parse_flags") or [])
    defects = [flag for flag in flags if flag in INVALID_FLAGS or flag in INCOMPLETE_FLAGS]
    if any(flag in INVALID_FLAGS for flag in defects):
        return IngestStage.INVALID.value, defects
    if any(flag in INCOMPLETE_FLAGS for flag in defects):
        return IngestStage.INCOMPLETE.value, defects
    return IngestStage.COMPLETE.value, []


def can_merge(listing: Listing) -> bool:
    """只有完整、未撤回/下架的挂牌才足以支持归并（sold 已成交未下架仍参与，供僵尸识别）。"""
    return (
        listing.ingest_stage == IngestStage.COMPLETE.value
        and listing.status != "off_market"
        and listing.withdrawn_at is None
    )


def raw_snapshot(raw: dict) -> dict:
    """采集原文全量快照（深拷贝为可 JSON 化的普通字典）。"""
    return {field: raw.get(field) for field in _RAW_SNAPSHOT_FIELDS}


def reclassify(listing: Listing) -> dict:
    """对一条挂牌重新归一 + 分诊，返回归一字典（不落库）。"""
    norm = normalizer.normalize_listing(
        {field: getattr(listing, field) for field in _RAW_SNAPSHOT_FIELDS}
    )
    stage, defects = classify(norm)
    listing.ingest_stage = stage
    listing.defect_flags = defects
    return norm


def apply_normalized(listing: Listing, norm: dict) -> None:
    listing.community_norm = norm["community_norm"]
    listing.layout_norm = norm["layout_norm"]
    listing.area_num = norm["area_num"]
    listing.price_num = norm["price_num"]
    listing.floor_num = norm["floor_num"]
    listing.floor_total = norm["floor_total"]
    listing.floor_band = norm["floor_band"]
    listing.orientation_norm = norm["orientation_norm"]
    listing.title_tokens = norm["title_tokens"]
    listing.parse_flags = norm["parse_flags"]


# --------------------------------------------------------------------------- #
# 批次台账
# --------------------------------------------------------------------------- #


def serialize_batch(batch: IngestBatch) -> dict:
    return {
        "id": batch.id,
        "batch_no": batch.batch_no,
        "agency_id": batch.agency_id,
        "agency_code": batch.agency_code,
        "status": batch.status,
        "status_label": BATCH_STATUS_LABELS.get(batch.status, batch.status),
        "received_count": batch.received_count,
        "complete_count": batch.complete_count,
        "incomplete_count": batch.incomplete_count,
        "invalid_count": batch.invalid_count,
        "created_count": batch.created_count,
        "updated_count": batch.updated_count,
        "unchanged_count": batch.unchanged_count,
        "retry_of": batch.retry_of,
        "submitted_by": batch.submitted_by,
        "note": batch.note,
        "last_error": batch.last_error,
        "processed_at": iso_utc(batch.processed_at),
        "created_at": iso_utc(batch.created_at),
        "updated_at": iso_utc(batch.updated_at),
    }


def list_batches(db: Session, *, agency_code: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    stmt = select(IngestBatch)
    count_stmt = select(func.count()).select_from(IngestBatch)
    if agency_code:
        stmt = stmt.where(IngestBatch.agency_code == agency_code)
        count_stmt = count_stmt.where(IngestBatch.agency_code == agency_code)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(IngestBatch.id.desc()).limit(limit).offset(offset)).all()
    return {"items": [serialize_batch(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def get_batch(db: Session, batch_no: str) -> IngestBatch:
    batch = db.scalar(select(IngestBatch).where(IngestBatch.batch_no == batch_no))
    if batch is None:
        raise not_found("ingest_batch", batch_no)
    return batch


def _agency_lookup(db: Session, agency_code: str) -> Agency:
    agency = db.scalar(select(Agency).where(Agency.code == agency_code))
    if agency is None:
        raise AppError("NOT_FOUND", 404, {"entity": "agency", "code": agency_code})
    return agency


def ingest_items(
    db: Session,
    *,
    batch_no: str,
    agency_code: str,
    items: list[dict],
    submitted_by: str,
    note: str | None = None,
    retry_of: str | None = None,
) -> IngestBatch:
    """按 (agency_id, lid) 幂等写入一批采集记录并完成分诊。

    同批重试：已存在的 lid 不新增；内容未变记 unchanged，有变化记 updated；
    新 lid 记 created。任何一条失败都抛异常，由外层事务回滚，已写记录不丢失（仍在库中）。
    """
    if not items:
        raise AppError("VALIDATION", 422, {"items": "批次至少包含一条挂牌"})
    seen_lids: set[str] = set()
    for item in items:
        lid = str(item.get("lid") or "").strip()
        if not lid:
            raise AppError("VALIDATION", 422, {"lid": "挂牌编号 lid 必填"})
        if lid in seen_lids:
            raise AppError("CONFLICT", 409, {"lid": lid, "reason": "同一批次内 lid 重复"})
        seen_lids.add(lid)

    agency = _agency_lookup(db, agency_code)
    existing = {
        lid: listing
        for lid, listing in db.execute(
            select(Listing.lid, Listing).where(
                Listing.agency_id == agency.id, Listing.lid.in_(seen_lids)
            )
        ).all()
    }

    batch = db.scalar(select(IngestBatch).where(IngestBatch.batch_no == batch_no))
    if batch is None:
        batch = IngestBatch(batch_no=batch_no, retry_of=retry_of)
        db.add(batch)
    batch.agency_id = agency.id
    batch.agency_code = agency.code
    batch.submitted_by = submitted_by
    batch.note = note
    batch.last_error = None
    batch.received_count = len(items)

    created = updated = unchanged = 0
    stage_counter = {IngestStage.COMPLETE.value: 0, IngestStage.INCOMPLETE.value: 0, IngestStage.INVALID.value: 0}

    for item in items:
        lid = str(item["lid"]).strip()
        raw = {field: item.get(field) for field in _RAW_SNAPSHOT_FIELDS}
        norm = normalizer.normalize_listing(raw)
        stage, defects = classify(norm)
        stage_counter[stage] += 1
        listing = existing.get(lid)
        if listing is None:
            listing = Listing(
                agency_id=agency.id,
                lid=lid,
                status=str(item.get("status") or "active"),
                listed_days=int(item.get("listed_days") or 0),
                views=int(item.get("views") or 0),
                ingest_batch=batch_no,
                raw_payload=raw_snapshot(item),
            )
            db.add(listing)
            created += 1
        else:
            if _working_fields_changed(listing, raw):
                updated += 1
            else:
                unchanged += 1
        for field, value in raw.items():
            setattr(listing, field, value if value is not None else getattr(listing, field))
        if item.get("status"):
            listing.status = str(item["status"])
        if item.get("listed_days") is not None:
            listing.listed_days = int(item["listed_days"])
        if item.get("views") is not None:
            listing.views = int(item["views"])
        listing.ingest_batch = batch_no
        if not listing.raw_payload:
            listing.raw_payload = raw_snapshot(item)
        apply_normalized(listing, norm)
        listing.ingest_stage = stage
        listing.defect_flags = defects
        # 待补/不合规/撤回记录不允许挂在任何簇上，留待补正后重算
        if not can_merge(listing):
            listing.property_id = None
            listing.risk_tags = []

    batch.created_count = created
    batch.updated_count = updated
    batch.unchanged_count = unchanged
    batch.complete_count = stage_counter[IngestStage.COMPLETE.value]
    batch.incomplete_count = stage_counter[IngestStage.INCOMPLETE.value]
    batch.invalid_count = stage_counter[IngestStage.INVALID.value]
    batch.status = "processed"
    batch.processed_at = utcnow()
    db.flush()
    return batch


def _working_fields_changed(listing: Listing, raw: dict) -> bool:
    return any(str(getattr(listing, field) or "") != str(raw.get(field) or "") for field in _RAW_SNAPSHOT_FIELDS)


# --------------------------------------------------------------------------- #
# 补正与撤回
# --------------------------------------------------------------------------- #


def serialize_correction(row: ListingCorrection) -> dict:
    return {
        "id": row.id,
        "listing_id": row.listing_id,
        "batch_no": row.batch_no,
        "field": row.field,
        "before": row.before,
        "after": row.after,
        "status": row.status,
        "status_label": CORRECTION_STATUS_LABELS.get(row.status, row.status),
        "reason": row.reason,
        "submitted_by": row.submitted_by,
        "created_at": iso_utc(row.created_at),
    }


def list_corrections(db: Session, listing_id: int, *, limit: int = 50) -> list[dict]:
    rows = db.scalars(
        select(ListingCorrection)
        .where(ListingCorrection.listing_id == listing_id)
        .order_by(ListingCorrection.id.desc())
        .limit(limit)
    ).all()
    return [serialize_correction(row) for row in rows]


def _validate_correction(field: str, value: str | None) -> None:
    if field not in CORRECTABLE_FIELDS:
        raise AppError("VALIDATION", 422, {"field": field, "allowed": list(CORRECTABLE_FIELDS)})
    if value is None or not str(value).strip():
        raise AppError("VALIDATION", 422, {"field": field, "reason": "补正值不能为空"})


def preview_correction(listing: Listing, changes: dict[str, str]) -> dict:
    """试算补正后的归一与分诊，不落库（用于驳回前判定）。"""
    probe = Listing()
    for fld in _RAW_SNAPSHOT_FIELDS:
        setattr(probe, fld, getattr(listing, fld))
    for field, value in changes.items():
        setattr(probe, field, value)
    norm = normalizer.normalize_listing({fld: getattr(probe, fld) for fld in _RAW_SNAPSHOT_FIELDS})
    stage, defects = classify(norm)
    return {"norm": norm, "stage": stage, "defect_flags": defects}


def apply_correction(
    db: Session,
    listing: Listing,
    changes: dict[str, str],
    *,
    submitted_by: str,
    batch_no: str | None,
    reason: str | None,
) -> dict:
    """字段级补正：先试算，校验失败只留 rejected 痕迹、不动原记录；成功才应用并重分诊。"""
    for field, value in changes.items():
        _validate_correction(field, value)
    preview = preview_correction(listing, changes)
    records: list[ListingCorrection] = []
    for field, value in changes.items():
        before = getattr(listing, field)
        if str(before or "") == str(value):
            continue
        row = ListingCorrection(
            listing_id=listing.id,
            batch_no=batch_no,
            field=field,
            before=str(before) if before is not None else None,
            after=str(value),
            status="applied",
            reason=reason,
            submitted_by=submitted_by,
        )
        db.add(row)
        records.append(row)
        setattr(listing, field, value)
    apply_normalized(listing, preview["norm"])
    listing.ingest_stage = preview["stage"]
    listing.defect_flags = preview["defect_flags"]
    if not can_merge(listing):
        listing.property_id = None
        listing.risk_tags = []
    db.flush()
    return {
        "listing_id": listing.id,
        "stage": listing.ingest_stage,
        "stage_label": INGEST_STAGE_LABELS.get(listing.ingest_stage, listing.ingest_stage),
        "defect_flags": listing.defect_flags,
        "can_merge": can_merge(listing),
        "corrections": [serialize_correction(row) for row in records],
    }


def reject_correction(
    db: Session,
    listing: Listing,
    changes: dict[str, str],
    *,
    submitted_by: str,
    batch_no: str | None,
    reason: str | None,
) -> dict:
    """补正失败：原记录保持不变，仅写 rejected 留痕，保证记录不丢失、过程可追溯。"""
    records = []
    for field, value in changes.items():
        row = ListingCorrection(
            listing_id=listing.id,
            batch_no=batch_no,
            field=field,
            before=str(getattr(listing, field)) if getattr(listing, field) is not None else None,
            after=str(value) if value is not None else None,
            status="rejected",
            reason=reason,
            submitted_by=submitted_by,
        )
        db.add(row)
        records.append(row)
    db.flush()
    return {
        "listing_id": listing.id,
        "stage": listing.ingest_stage,
        "stage_label": INGEST_STAGE_LABELS.get(listing.ingest_stage, listing.ingest_stage),
        "defect_flags": listing.defect_flags,
        "can_merge": can_merge(listing),
        "corrections": [serialize_correction(row) for row in records],
    }


def withdraw(db: Session, listing: Listing, *, submitted_by: str, reason: str | None) -> dict:
    """撤回挂牌：置 withdrawn_at 并转 off_market，下次重算自动出簇；记录本身保留。"""
    if listing.withdrawn_at is None:
        listing.withdrawn_at = utcnow()
    listing.status = "off_market"
    listing.property_id = None
    listing.risk_tags = []
    db.add(
        ListingCorrection(
            listing_id=listing.id,
            batch_no=listing.ingest_batch,
            field="status",
            before="active",
            after="off_market",
            status="applied",
            reason=reason or "人工撤回",
            submitted_by=submitted_by,
        )
    )
    db.flush()
    return {"listing_id": listing.id, "withdrawn_at": iso_utc(listing.withdrawn_at), "status": listing.status}
