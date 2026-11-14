"""归因服务：量价分解（算法 B）+ 门店/渠道/房型三维贡献 + 渠道×房型下钻 + 结论文本。

幂等与版本口径：
- 同一异常重跑不重复生成归因、不改变既有结论（自然键唯一 + 既有行直接返回）；
- 归因行带口径版本（room_type_drilldown.ATTRIBUTION_VERSION），报告与下钻引用同一版本；
- 生成中断时事务整体回滚，完整旧版保持可用，不会留下半截新版。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.constants.enums import CHANNEL_LABELS, CHANNEL_ORDER, METRIC_LABELS
from src.models.anomaly import Anomaly
from src.models.attribution import Attribution
from src.models.metric import MetricPoint
from src.models.store import Store
from src.services import decompose, metric_service, room_type_drilldown
from src.utils.errors import not_found
from src.utils.timeutil import iso_utc, parse_date


def serialize(attribution: Attribution, anomaly: Anomaly | None = None, store: Store | None = None) -> dict:
    return {
        "id": attribution.id,
        "anomaly_id": attribution.anomaly_id,
        "store_id": attribution.store_id,
        "store_code": store.code if store else None,
        "store_name": store.name if store else None,
        "metric_date": anomaly.metric_date.isoformat() if anomaly else None,
        "metric_key": anomaly.metric_key if anomaly else None,
        "metric_label": METRIC_LABELS.get(anomaly.metric_key, anomaly.metric_key) if anomaly else None,
        "version": attribution.version or 1,
        "method": attribution.method,
        "status": attribution.status,
        "base_rev": round(attribution.base_rev, 2),
        "cur_rev": round(attribution.cur_rev, 2),
        "total_delta": round(attribution.total_delta, 2),
        "parts": {
            "adr": round(attribution.adr_part, 2),
            "occ": round(attribution.occ_part, 2),
            "rooms": round(attribution.rooms_part, 2),
        },
        "shares": {
            "adr": None if attribution.adr_share is None else round(attribution.adr_share, 6),
            "occ": None if attribution.occ_share is None else round(attribution.occ_share, 6),
            "rooms": None if attribution.rooms_share is None else round(attribution.rooms_share, 6),
        },
        "residual": round(attribution.residual, 4),
        "drivers": attribution.drivers or {},
        "dimensions": attribution.dimensions or {},
        "narrative": attribution.narrative,
        "created_at": iso_utc(attribution.created_at),
    }


def _window_range(anomaly: Anomaly) -> tuple[date, date]:
    payload = anomaly.payload or {}
    start = parse_date(payload.get("window_start")) or (anomaly.metric_date - timedelta(days=56))
    end = parse_date(payload.get("window_end")) or (anomaly.metric_date - timedelta(days=1))
    return start, end


def _stratum(anomaly: Anomaly) -> bool:
    """检测基线的周末/工作日分层（与算法 A 同一口径）。"""
    return anomaly.metric_date.weekday() >= 5


def _channel_rows(db: Session, anomaly: Anomaly) -> list[dict]:
    start, end = _window_range(anomaly)
    window = metric_service.channel_window(db, anomaly.store_id, start, end, _stratum(anomaly))
    current = metric_service.channel_split(db, anomaly.store_id, anomaly.metric_date)
    groups = []
    for channel in CHANNEL_ORDER:
        base = metric_service.median(window.get(channel, [])) if window.get(channel) else 0.0
        groups.append({"key": CHANNEL_LABELS.get(channel, channel), "value": current.get(channel, 0.0), "base": base})
    return groups


def _region_store_rows(db: Session, anomaly: Anomaly) -> list[dict]:
    store = db.get(Store, anomaly.store_id)
    if store is None:
        return []
    peers = db.scalars(select(Store).where(Store.region == store.region)).all()
    peer_ids = [peer.id for peer in peers]
    start = anomaly.metric_date - timedelta(days=56)
    rows = db.execute(
        select(MetricPoint.store_id, MetricPoint.stat_date, func.sum(MetricPoint.revenue))
        .where(
            MetricPoint.store_id.in_(peer_ids),
            MetricPoint.stat_date >= start,
            MetricPoint.stat_date <= anomaly.metric_date,
        )
        .group_by(MetricPoint.store_id, MetricPoint.stat_date)
    ).all()
    bucket: dict[int, dict[date, float]] = {}
    for store_id, stat_date, revenue in rows:
        bucket.setdefault(store_id, {})[stat_date] = float(revenue or 0.0)
    groups = []
    for peer in peers:
        daily = bucket.get(peer.id, {})
        current = daily.get(anomaly.metric_date)
        if current is None:
            continue
        reference = [
            value
            for day, value in daily.items()
            if day != anomaly.metric_date and day.weekday() < 5 and anomaly.metric_date.weekday() < 5
        ]
        base = metric_service.median(reference) if reference else current
        groups.append({"key": f"{peer.name}（{peer.code}）", "value": current, "base": base})
    return groups


def _channel_room_type_block(db: Session, anomaly: Anomaly, store_base_rev: float, store_cur_rev: float) -> dict:
    """按各渠道逐日上报的房型结构构建渠道×房型下钻（v2 口径，逐层对账）。"""
    start, end = _window_range(anomaly)
    stratum = _stratum(anomaly)
    window = metric_service.channel_window(db, anomaly.store_id, start, end, stratum)
    current_rev = metric_service.channel_split(db, anomaly.store_id, anomaly.metric_date)
    current_mix = metric_service.channel_mixes(db, anomaly.store_id, anomaly.metric_date)
    window_mix = metric_service.channel_mix_window(db, anomaly.store_id, start, end, stratum)
    channels = []
    for channel in CHANNEL_ORDER:
        _, base_mix, _ = room_type_drilldown.mean_mix(window_mix.get(channel, []))
        channels.append(
            {
                "channel": channel,
                "label": CHANNEL_LABELS.get(channel, channel),
                "current": current_rev.get(channel, 0.0),
                "base": metric_service.median(window.get(channel, [])) if window.get(channel) else 0.0,
                "mix_current": current_mix.get(channel),
                "mix_base": base_mix or None,
            }
        )
    return room_type_drilldown.build_drilldown(channels, store_cur_rev, store_base_rev)


def build(db: Session, anomaly: Anomaly, store: Store | None = None) -> Attribution:
    existing = db.scalar(select(Attribution).where(Attribution.anomaly_id == anomaly.id))
    if existing is not None:
        return existing
    store = store or db.get(Store, anomaly.store_id)
    payload = anomaly.payload or {}
    base = payload.get("baseline_triple") or {}
    current = payload.get("current_triple") or {}
    triple_base = {
        "adr": float(base.get("adr") or anomaly.baseline),
        "occ": float(base.get("occ") or 0.0),
        "rooms": float(base.get("rooms") or 0.0),
        "revenue": float(anomaly.baseline),
    }
    triple_cur = {
        "adr": float(current.get("adr") or anomaly.observed),
        "occ": float(current.get("occ") or 0.0),
        "rooms": float(current.get("rooms") or 0.0),
        "revenue": float(anomaly.observed),
    }
    result = decompose.decompose(triple_base, triple_cur)

    channel_rows = _channel_rows(db, anomaly)
    channel_rank = decompose.rank_contributions(channel_rows, result["total_delta"])
    store_rank = decompose.rank_contributions(_region_store_rows(db, anomaly), result["total_delta"])
    # 门店总量口径：优先取检测载荷里的营收三元组（任何指标异常都是营收口径），与 LMDI 同源
    store_base_rev = float(base.get("revenue") or anomaly.baseline)
    store_cur_rev = float(current.get("revenue") or anomaly.observed)
    drilldown = _channel_room_type_block(db, anomaly, store_base_rev, store_cur_rev)
    room_rows = room_type_drilldown.aggregate_room_types(drilldown)
    room_rank = decompose.rank_contributions(room_rows, result["total_delta"])

    dimensions = {
        "channel": {"label": "渠道", "rows": channel_rank["rows"], "top_negative": channel_rank["top_negative"], "top_positive": channel_rank["top_positive"]},
        "store": {"label": "门店（同区域）", "rows": store_rank["rows"], "top_negative": store_rank["top_negative"], "top_positive": store_rank["top_positive"]},
        "room_type": {"label": "房型（渠道上报结构汇总）", "rows": room_rank["rows"], "top_negative": room_rank["top_negative"], "top_positive": room_rank["top_positive"]},
        "channel_room_type": drilldown,
    }
    narrative = decompose.narrative(
        result, channel_rank["rows"], store.name if store else anomaly.payload.get("store_code", ""), anomaly.metric_date.isoformat()
    )
    if room_rank["top_negative"] and room_rank["top_negative"][0].get("contribution") is not None:
        top = room_rank["top_negative"][0]
        narrative += f" 房型层面「{top['key']}」变动 {top['delta']:+,.0f} 元（占 {top['contribution'] * 100:+.1f}%），按渠道逐日上报结构分摊。"
    unallocated_total = drilldown["reconciliation"]["unallocated_total"]
    if abs(unallocated_total) > 0.01:
        narrative += f" 另有 {unallocated_total:+,.0f} 元因房型结构缺失或占比异常未分配到房型，已单列未分配差额。"
    shares = result["shares"]
    attribution = Attribution(
        anomaly_id=anomaly.id,
        store_id=anomaly.store_id,
        version=room_type_drilldown.ATTRIBUTION_VERSION,
        method=result["method"],
        status=result["status"],
        base_rev=triple_base["revenue"],
        cur_rev=triple_cur["revenue"],
        total_delta=result["total_delta"],
        adr_part=result["parts"]["adr"] or 0.0,
        occ_part=result["parts"]["occ"] or 0.0,
        rooms_part=result["parts"]["rooms"] or 0.0,
        adr_share=shares["adr"],
        occ_share=shares["occ"],
        rooms_share=shares["rooms"],
        residual=result["residual"] or 0.0,
        drivers={
            "base": result["base"],
            "current": result["current"],
            "reason": result["reason"],
            "log_mean_weight": result.get("log_mean_weight"),
        },
        dimensions=dimensions,
        narrative=narrative,
    )
    db.add(attribution)
    db.flush()
    return attribution


def get_by_anomaly(db: Session, anomaly_id: int) -> dict:
    attribution = db.scalar(select(Attribution).where(Attribution.anomaly_id == anomaly_id))
    if attribution is None:
        anomaly = db.get(Anomaly, anomaly_id)
        if anomaly is None:
            raise not_found("anomaly", anomaly_id)
        attribution = build(db, anomaly)
        try:
            db.commit()
        except IntegrityError:
            # 并发下另一请求已按自然键生成：回滚后复用既有行，不重复生成、不改变既有结论
            db.rollback()
            attribution = db.scalar(select(Attribution).where(Attribution.anomaly_id == anomaly_id))
    anomaly = db.get(Anomaly, attribution.anomaly_id)
    store = db.get(Store, attribution.store_id)
    return serialize(attribution, anomaly, store)


def list_attributions(db: Session, *, limit: int = 20, offset: int = 0) -> dict:
    total = db.scalar(select(func.count()).select_from(Attribution)) or 0
    rows = db.scalars(select(Attribution).order_by(Attribution.id.desc()).limit(limit).offset(offset)).all()
    anomalies = {row.id: row for row in db.scalars(select(Anomaly)).all()}
    stores = {row.id: row for row in db.scalars(select(Store)).all()}
    return {
        "items": [serialize(row, anomalies.get(row.anomaly_id), stores.get(row.store_id)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
