"""演示数据集不变量：规模、头条异常数值、渠道一致性、确定性。"""
from __future__ import annotations

from datetime import date

from scripts.demo_data import HEADLINE_DATE, HEADLINE_IMPACT, build_dataset

from src.services import decompose, detection

CACHE: dict = {}


def dataset() -> dict:
    if "data" not in CACHE:
        CACHE["data"] = build_dataset()
    return CACHE["data"]


def aggregate_series(points: list[dict], store_code: str) -> list[dict]:
    days: dict = {}
    for point in points:
        if point["store_code"] != store_code:
            continue
        entry = days.setdefault(
            point["stat_date"],
            {"date": point["stat_date"].isoformat(), "rooms_available": point["rooms_available"], "rooms_sold": 0, "revenue": 0.0, "is_weekend": point["is_weekend"]},
        )
        entry["rooms_sold"] += point["rooms_sold"]
        entry["revenue"] += point["revenue"]
    series = []
    for stat_date, entry in sorted(days.items()):
        revenue = round(entry["revenue"], 2)
        series.append(
            {
                "date": entry["date"],
                "rooms_available": entry["rooms_available"],
                "rooms_sold": entry["rooms_sold"],
                "occupancy": round(entry["rooms_sold"] / entry["rooms_available"], 6),
                "adr": round(revenue / entry["rooms_sold"], 6),
                "revenue": revenue,
                "is_weekend": entry["is_weekend"],
            }
        )
    return series


def test_dataset_shape():
    data = dataset()
    assert data["meta"]["stores"] == 120
    assert data["meta"]["days"] == 90
    assert data["meta"]["points"] == 32400
    assert len(data["events"]) == 37


def test_headline_anomaly_matches_acceptance():
    data = dataset()
    series = aggregate_series(data["points"], "LB-HD-001")
    hits = detection.detect_series(series, "rev")["hits"]
    headline = [hit for hit in hits if hit["date"] == HEADLINE_DATE.isoformat()]
    assert len(headline) == 1
    hit = headline[0]
    assert round(hit["robust_z"], 1) == -4.2
    assert abs(hit["impact_amount"] - HEADLINE_IMPACT) < 2_000
    result = decompose.decompose(hit["baseline_triple"], hit["current_triple"])
    shares = result["shares"]
    assert abs(shares["adr"] - 0.62) < 0.03
    assert abs(shares["occ"] - 0.31) < 0.03
    assert abs(shares["rooms"] - 0.07) < 0.02


def test_channel_revenue_sums_to_store_revenue():
    data = dataset()
    series = aggregate_series(data["points"], "LB-HD-001")
    total = sum(point["revenue"] for point in data["points"] if point["store_code"] == "LB-HD-001")
    assert abs(total - sum(item["revenue"] for item in series)) < 1.0


def test_detection_finds_exactly_the_planned_events():
    data = dataset()
    found = set()
    for store in {point["store_code"] for point in data["points"]}:
        for hit in detection.detect_series(aggregate_series(data["points"], store), "rev")["hits"]:
            found.add((store, hit["date"]))
    planned = {(item["store_code"], item["metric_date"]) for item in data["events"]}
    assert found == planned
    assert len(found) == 37


def test_dataset_is_deterministic():
    first = build_dataset()
    second = build_dataset()
    assert first["meta"] == second["meta"]
    assert first["events"] == second["events"]
    assert first["points"][:50] == second["points"][:50]


def _mix_of(points: list[dict], store_code: str, day: date, channel: str) -> dict:
    for point in points:
        if point["store_code"] == store_code and point["stat_date"] == day and point["channel"] == channel:
            return point["room_type_mix"]
    raise AssertionError(f"缺少指标点 {store_code} {day} {channel}")


def test_room_type_mix_reported_per_channel_and_day():
    """各渠道逐日上报结构：同一门店同一天不同渠道结构不同，且合计≈1。"""
    data = dataset()
    points = data["points"]
    ota = _mix_of(points, "LB-HD-001", date(2026, 10, 20), "ota")
    corp = _mix_of(points, "LB-HD-001", date(2026, 10, 20), "corp")
    assert ota != corp
    for mix in (ota, corp):
        assert abs(sum(mix.values()) - 1.0) < 0.02


def test_headline_suite_suspension_window():
    """外滩店套房 11-02 ~ 11-08 停售：窗口内各渠道套房占比归零，窗口外恢复。"""
    data = dataset()
    points = data["points"]
    for day in (date(2026, 11, 2), HEADLINE_DATE, date(2026, 11, 8)):
        for channel in ("ota", "direct", "corp"):
            assert _mix_of(points, "LB-HD-001", day, channel)["套房"] == 0.0
    assert _mix_of(points, "LB-HD-001", date(2026, 11, 1), "ota")["套房"] > 0
    assert _mix_of(points, "LB-HD-001", date(2026, 11, 9), "ota")["套房"] > 0


def test_suiteless_brand_never_reports_suite():
    """轻居品牌门店（如 LB-HB-001）任何渠道任何日期都没有套房 → 归因不再凭空生成套房拖累。"""
    data = dataset()
    for point in data["points"]:
        if point["store_code"] == "LB-HB-001":
            assert "套房" not in point["room_type_mix"]


def test_structure_gap_scenarios_present():
    """结构缺失 / 占比异常场景落在对应门店的异常日，用于未分配差额路径。"""
    data = dataset()
    points = data["points"]
    # LB-HD-002 异常日 2026-09-30：corp 结构缺失，其余渠道正常
    assert _mix_of(points, "LB-HD-002", date(2026, 9, 30), "corp") == {}
    assert _mix_of(points, "LB-HD-002", date(2026, 9, 30), "ota") != {}
    # LB-HZ-002 异常日 2026-10-06：ota 占比合计 1.65（异常），其余渠道正常
    abnormal = _mix_of(points, "LB-HZ-002", date(2026, 10, 6), "ota")
    assert abs(sum(abnormal.values()) - 1.0) > 0.02
    assert abs(sum(_mix_of(points, "LB-HZ-002", date(2026, 10, 6), "corp").values()) - 1.0) < 0.02
