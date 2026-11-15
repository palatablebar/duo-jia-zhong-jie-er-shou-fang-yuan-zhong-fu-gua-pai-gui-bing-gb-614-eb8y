from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import INTAKE_WRITE_ROLES
from src.controllers import intake_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.intake import BatchIn, BatchRetryIn, CorrectionIn, WithdrawIn
from src.utils.pagination import page_params

router = APIRouter(tags=["intake"])


@router.get("/intake/batches")
def list_batches(
    agency_code: str | None = None,
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return intake_controller.list_batches(db, agency_code=agency_code, limit=page.limit, offset=page.offset)


@router.get("/intake/batches/{batch_no}")
def get_batch(
    batch_no: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return intake_controller.get_batch(db, batch_no)


@router.post("/intake/batches", status_code=201)
def ingest_batch(
    payload: BatchIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*INTAKE_WRITE_ROLES)),
) -> dict:
    return intake_controller.ingest_batch(db, payload.model_dump(), principal, request)


@router.post("/intake/batches/{batch_no}/retry")
def retry_batch(
    batch_no: str,
    payload: BatchRetryIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*INTAKE_WRITE_ROLES)),
) -> dict:
    return intake_controller.retry_batch(db, batch_no, payload.model_dump(exclude_unset=True), principal, request)


@router.get("/listings/{listing_id}/corrections")
def list_corrections(
    listing_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return intake_controller.list_corrections(db, listing_id)


@router.post("/listings/{listing_id}/corrections")
def correct_listing(
    listing_id: int,
    payload: CorrectionIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*INTAKE_WRITE_ROLES)),
) -> dict:
    return intake_controller.correct_listing(db, listing_id, payload.model_dump(), principal, request)


@router.post("/listings/{listing_id}/withdraw")
def withdraw_listing(
    listing_id: int,
    payload: WithdrawIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*INTAKE_WRITE_ROLES)),
) -> dict:
    return intake_controller.withdraw_listing(db, listing_id, payload.model_dump(), principal, request)
