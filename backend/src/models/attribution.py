"""归因（与异常一对一）：LMDI 量价分解 + 三维贡献 + 渠道×房型下钻 + 结论文本。

version 记录口径版本（v1 固定分摊 / v2 渠道逐日上报结构）；既有归因行永不改写，
重跑同一异常幂等返回旧行，报告与下钻引用同一版本。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class Attribution(Base):
    __tablename__ = "attributions"
    __table_args__ = (UniqueConstraint("anomaly_id", name="uq_attribution_anomaly"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    anomaly_id: Mapped[int] = mapped_column(ForeignKey("anomalies.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    method: Mapped[str] = mapped_column(String(60), default="")
    status: Mapped[str] = mapped_column(String(16), default="ok")
    base_rev: Mapped[float] = mapped_column(Float, default=0.0)
    cur_rev: Mapped[float] = mapped_column(Float, default=0.0)
    total_delta: Mapped[float] = mapped_column(Float, default=0.0)
    adr_part: Mapped[float] = mapped_column(Float, default=0.0)
    occ_part: Mapped[float] = mapped_column(Float, default=0.0)
    rooms_part: Mapped[float] = mapped_column(Float, default=0.0)
    adr_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    occ_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    rooms_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    residual: Mapped[float] = mapped_column(Float, default=0.0)
    drivers: Mapped[dict] = mapped_column(JSON, default=dict)
    dimensions: Mapped[dict] = mapped_column(JSON, default=dict)
    narrative: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
