"""挂牌补正留痕：每次字段级补正一行，applied / rejected 都不丢记录。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class ListingCorrection(Base):
    __tablename__ = "listing_corrections"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    batch_no: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    field: Mapped[str] = mapped_column(String(40))
    before: Mapped[str | None] = mapped_column(String(400), nullable=True)
    after: Mapped[str | None] = mapped_column(String(400), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="applied", index=True)
    reason: Mapped[str | None] = mapped_column(String(400), nullable=True)
    submitted_by: Mapped[str] = mapped_column(String(160), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
