"""采集分诊控制器：批次入库、补正、撤回，全部写领域审计；失败不丢记录。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, intake_service, listing_service
from src.utils.errors import AppError, not_found


def list_batches(db: Session, *, agency_code: str | None, limit: int, offset: int) -> dict:
    return intake_service.list_batches(db, agency_code=agency_code, limit=limit, offset=offset)


def get_batch(db: Session, batch_no: str) -> dict:
    return intake_service.serialize_batch(intake_service.get_batch(db, batch_no))


def ingest_batch(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    batch = intake_service.ingest_items(
        db,
        batch_no=payload["batch_no"],
        agency_code=payload["agency_code"],
        items=payload["items"],
        submitted_by=principal["email"],
        note=payload.get("note"),
        retry_of=payload.get("retry_of"),
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="intake.ingest",
        entity="ingest_batch",
        entity_id=batch.batch_no,
        detail={
            "received": batch.received_count,
            "complete": batch.complete_count,
            "incomplete": batch.incomplete_count,
            "invalid": batch.invalid_count,
            "created": batch.created_count,
            "updated": batch.updated_count,
            "unchanged": batch.unchanged_count,
            "retry_of": batch.retry_of,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return intake_service.serialize_batch(batch)


def retry_batch(db: Session, batch_no: str, payload: dict, principal: dict, request: Request) -> dict:
    """整批重试：默认复用原批次记录（同批重试不新增），也可用新 items 覆盖后重处理。"""
    previous = intake_service.get_batch(db, batch_no)
    if payload.get("items"):
        items = payload["items"]
    else:
        from src.models.listing import Listing
        from sqlalchemy import select

        rows = db.scalars(
            select(Listing).where(Listing.ingest_batch == batch_no).order_by(Listing.id)
        ).all()
        if not rows:
            raise AppError("VALIDATION", 422, {"batch_no": batch_no, "reason": "原批次没有可重试的记录"})
        items = [
            {
                "lid": row.lid,
                "community_raw": row.community_raw,
                "layout_raw": row.layout_raw,
                "area_raw": row.area_raw,
                "price_raw": row.price_raw,
                "floor_raw": row.floor_raw,
                "orientation_raw": row.orientation_raw,
                "title": row.title,
                "status": row.status,
                "listed_days": row.listed_days,
                "views": row.views,
            }
            for row in rows
        ]
    batch = intake_service.ingest_items(
        db,
        batch_no=batch_no,
        agency_code=previous.agency_code,
        items=items,
        submitted_by=principal["email"],
        note=payload.get("note") or previous.note,
        retry_of=previous.retry_of or batch_no,
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="intake.retry",
        entity="ingest_batch",
        entity_id=batch.batch_no,
        detail={"complete": batch.complete_count, "incomplete": batch.incomplete_count, "invalid": batch.invalid_count},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return intake_service.serialize_batch(batch)


def _get_listing(db: Session, listing_id: int):
    from src.models.listing import Listing

    listing = db.get(Listing, listing_id)
    if listing is None:
        raise not_found("listing", listing_id)
    return listing


def correct_listing(db: Session, listing_id: int, payload: dict, principal: dict, request: Request) -> dict:
    listing = _get_listing(db, listing_id)
    changes = payload["changes"]
    reason = payload.get("reason")
    batch_no = payload.get("batch_no") or listing.ingest_batch

    # 先试算：补正后仍是待补/不合规则驳回，原记录不动，只留 rejected 痕迹（记录不丢失）
    preview = intake_service.preview_correction(listing, changes)
    if preview["stage"] != "complete" and not payload.get("force"):
        result = intake_service.reject_correction(
            db,
            listing,
            changes,
            submitted_by=principal["email"],
            batch_no=batch_no,
            reason=reason or f"补正后仍为{preview['stage']}：{','.join(preview['defect_flags'])}",
        )
        audit_service.record(
            db,
            actor_email=principal["email"],
            actor_role=principal["role"],
            action="intake.correct",
            entity="listing",
            entity_id=listing.id,
            detail={"result": "rejected", "defect_flags": preview["defect_flags"]},
            ip=request.client.host if request.client else None,
        )
        db.commit()
        return result

    result = intake_service.apply_correction(
        db,
        listing,
        changes,
        submitted_by=principal["email"],
        batch_no=batch_no,
        reason=reason,
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="intake.correct",
        entity="listing",
        entity_id=listing.id,
        detail={"result": "applied", "fields": list(changes.keys()), "stage": result["stage"]},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    result["listing"] = listing_service.get_listing(db, listing_id)
    return result


def list_corrections(db: Session, listing_id: int) -> dict:
    _get_listing(db, listing_id)
    return {"items": intake_service.list_corrections(db, listing_id)}


def withdraw_listing(db: Session, listing_id: int, payload: dict, principal: dict, request: Request) -> dict:
    listing = _get_listing(db, listing_id)
    result = intake_service.withdraw(
        db, listing, submitted_by=principal["email"], reason=payload.get("reason")
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="intake.withdraw",
        entity="listing",
        entity_id=listing.id,
        detail={"reason": payload.get("reason")},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    result["listing"] = listing_service.get_listing(db, listing_id)
    return result
