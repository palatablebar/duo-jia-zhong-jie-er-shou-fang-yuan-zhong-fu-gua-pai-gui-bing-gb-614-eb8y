"""中介渠道控制器：写操作记领域审计。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import agency_service, audit_service


def list_agencies(db: Session, **filters) -> dict:
    return agency_service.list_agencies(db, **filters)


def get_agency(db: Session, agency_id: int) -> dict:
    agency = agency_service.get_agency(db, agency_id)
    stats = agency_service.listing_stats(db).get(agency.id, {})
    return agency_service.serialize(
        agency,
        stats.get("count", 0),
        stats.get("last_at"),
        complete_count=stats.get("complete", 0),
        incomplete_count=stats.get("incomplete", 0),
        invalid_count=stats.get("invalid", 0),
        withdrawn_count=stats.get("withdrawn", 0),
    )


def create_agency(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    agency = agency_service.create_agency(db, payload)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="agency.create",
        entity="agency",
        entity_id=agency.id,
        detail={"code": agency.code, "name": agency.name},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return agency_service.serialize(agency)


def update_agency(db: Session, agency_id: int, payload: dict, principal: dict, request: Request) -> dict:
    agency = agency_service.get_agency(db, agency_id)
    agency_service.update_agency(db, agency, payload)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="agency.update",
        entity="agency",
        entity_id=agency.id,
        detail={"fields": list(payload.keys())},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    stats = agency_service.listing_stats(db).get(agency.id, {})
    return agency_service.serialize(
        agency,
        stats.get("count", 0),
        stats.get("last_at"),
        complete_count=stats.get("complete", 0),
        incomplete_count=stats.get("incomplete", 0),
        invalid_count=stats.get("invalid", 0),
        withdrawn_count=stats.get("withdrawn", 0),
    )
