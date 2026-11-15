from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# 采集原文允许的工作字段（与 intake_service.CORRECTABLE_FIELDS 对齐）
RAW_FIELDS = (
    "community_raw",
    "layout_raw",
    "area_raw",
    "price_raw",
    "floor_raw",
    "orientation_raw",
    "title",
)


class IngestItemIn(BaseModel):
    lid: str = Field(min_length=1, max_length=40)
    community_raw: str = Field(default="", max_length=160)
    layout_raw: str = Field(default="", max_length=60)
    area_raw: str = Field(default="", max_length=60)
    price_raw: str = Field(default="", max_length=60)
    floor_raw: str = Field(default="", max_length=60)
    orientation_raw: str = Field(default="", max_length=40)
    title: str = Field(default="", max_length=200)
    status: Literal["active", "sold", "off_market"] | None = None
    listed_days: int | None = Field(default=None, ge=0, le=36500)
    views: int | None = Field(default=None, ge=0, le=10_000_000)


class BatchIn(BaseModel):
    batch_no: str = Field(min_length=2, max_length=60, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    agency_code: str = Field(min_length=2, max_length=40)
    items: list[IngestItemIn] = Field(min_length=1, max_length=2000)
    note: str | None = Field(default=None, max_length=500)
    retry_of: str | None = Field(default=None, max_length=60)


class BatchRetryIn(BaseModel):
    items: list[IngestItemIn] | None = Field(default=None, max_length=2000)
    note: str | None = Field(default=None, max_length=500)


class CorrectionIn(BaseModel):
    changes: dict[str, str] = Field(min_length=1, max_length=8)
    reason: str | None = Field(default=None, max_length=400)
    batch_no: str | None = Field(default=None, max_length=60)
    force: bool = False


class WithdrawIn(BaseModel):
    reason: str | None = Field(default=None, max_length=400)
