"""渠道 × 房型下钻（room_type_drilldown）单测：对账恒等式 / 异常结构 / 停售 / 确定性。"""
from __future__ import annotations

from src.services import room_type_drilldown as drill

MIX_A = {"大床房": 0.55, "双床房": 0.33, "套房": 0.12}
MIX_B = {"大床房": 0.50, "双床房": 0.36, "套房": 0.14}

CHANNELS = [
    {"channel": "ota", "label": "OTA 渠道", "current": 200_000.0, "base": 240_000.0,
     "mix_current": MIX_A, "mix_base": MIX_B},
    {"channel": "direct", "label": "直连/官网", "current": 110_000.0, "base": 100_000.0,
     "mix_current": MIX_B, "mix_base": MIX_B},
    {"channel": "corp", "label": "协议/团队", "current": 50_000.0, "base": 60_000.0,
     "mix_current": MIX_A, "mix_base": MIX_A},
]
STORE_CURRENT = 360_000.0
STORE_BASE = 400_000.0


def build():
    return drill.build_drilldown(CHANNELS, STORE_CURRENT, STORE_BASE)


def sums(block):
    return round(sum(row["delta"] for row in block["room_types"]), 2)


def test_room_types_reconcile_to_each_channel():
    result = build()
    for block in result["channels"]:
        # 渠道Δ = Σ房型Δ + 未分配差额（恒等式逐渠道成立）
        unallocated = block["unallocated"]["delta"] if block["unallocated"] else 0.0
        assert abs(sums(block) + unallocated - block["delta"]) < 0.02
        assert block["status"] == "ok"
        assert block["unallocated"] is None
        # 占比归一化后合计恰为 1
        assert abs(sum(row["share_current"] for row in block["room_types"]) - 1.0) < 1e-6


def test_channels_reconcile_to_store_total():
    result = build()
    recon = result["reconciliation"]
    channel_sum = sum(block["delta"] for block in result["channels"])
    assert abs(recon["channel_delta_sum"] - channel_sum) < 0.02
    # 门店Δ = Σ渠道Δ + 口径差异（差额单列，恒等式成立）
    assert abs(recon["channel_delta_sum"] + recon["difference"] - recon["store_delta"]) < 0.02
    assert recon["store_delta"] == -40_000.0
    assert recon["current_difference"] == 0.0
    assert recon["balanced"] is True


def test_missing_structure_lists_unallocated_with_reason():
    channels = [dict(CHANNELS[0], mix_current={})]
    result = drill.build_drilldown(channels, 200_000.0, 240_000.0)
    block = result["channels"][0]
    assert block["status"] == "missing_structure"
    assert block["room_types"] == []
    # 整个渠道变动进入未分配差额，并说明原因
    assert block["unallocated"]["delta"] == block["delta"] == -40_000.0
    assert "未上报房型结构" in block["unallocated"]["reason"]
    assert result["reconciliation"]["unallocated_total"] == -40_000.0


def test_abnormal_share_lists_unallocated_with_reason():
    bad = {"大床房": 0.9, "双床房": 0.55, "套房": 0.2}  # 合计 1.65
    channels = [dict(CHANNELS[0], mix_current=bad)]
    result = drill.build_drilldown(channels, 200_000.0, 240_000.0)
    block = result["channels"][0]
    assert block["status"] == "abnormal_share"
    assert block["room_types"] == []
    assert "占比合计" in block["unallocated"]["reason"]
    negative = dict(CHANNELS[0], mix_current={"大床房": 1.2, "双床房": -0.2})
    block2 = drill.build_drilldown([negative], 200_000.0, 240_000.0)["channels"][0]
    assert block2["status"] == "abnormal_share"
    assert "占比非法" in block2["unallocated"]["reason"]


def test_suite_suspension_is_real_drag_not_phantom():
    # 当期套房停售（占比 0）：套房行如实显示真实拖累（当期 0 vs 基线 > 0）
    suspended = {"大床房": 0.62, "双床房": 0.38, "套房": 0.0}
    channels = [dict(CHANNELS[0], mix_current=suspended)]
    result = drill.build_drilldown(channels, 200_000.0, 240_000.0)
    block = result["channels"][0]
    suite = next(row for row in block["room_types"] if row["key"] == "套房")
    assert suite["current"] == 0.0
    assert suite["base"] > 0
    assert suite["delta"] < 0
    # 从无套房的物业：两期结构都无套房 → 不出现套房行（不凭空生成拖累）
    suiteless = {"大床房": 0.62, "双床房": 0.38}
    both = [dict(CHANNELS[0], mix_current=suiteless, mix_base=suiteless)]
    keys = [row["key"] for row in drill.build_drilldown(both, 200_000.0, 240_000.0)["channels"][0]["room_types"]]
    assert "套房" not in keys


def test_unnormalized_mix_within_tolerance_is_normalized():
    loose = {"大床房": 0.5505, "双床房": 0.3305, "套房": 0.1205}  # 合计 1.0015
    channels = [dict(CHANNELS[0], mix_current=loose, mix_base=loose)]
    block = drill.build_drilldown(channels, 200_000.0, 240_000.0)["channels"][0]
    assert block["status"] == "ok"
    assert abs(sum(row["share_current"] for row in block["room_types"]) - 1.0) < 1e-6


def test_zero_store_delta_gives_null_contributions():
    channels = [dict(CHANNELS[0], current=240_000.0, base=240_000.0)]
    result = drill.build_drilldown(channels, 240_000.0, 240_000.0)
    block = result["channels"][0]
    assert block["contribution"] is None
    assert all(row["contribution"] is None for row in block["room_types"])


def test_mean_mix_skips_invalid_days():
    mixes = [MIX_A, {}, {"大床房": 1.5}, MIX_B]
    status, merged, _ = drill.mean_mix(mixes)
    assert status == "ok"
    assert abs(sum(merged.values()) - 1.0) < 1e-9
    # MIX_A / MIX_B 合计均为 1，均值为两项算术平均
    assert abs(merged["大床房"] - (0.55 + 0.50) / 2) < 1e-9
    status, merged, reason = drill.mean_mix([{}, None])
    assert status == "missing_structure"
    assert merged == {}
    assert "无有效" in reason


def test_aggregate_room_types_includes_unallocated_row():
    channels = [dict(CHANNELS[0], mix_current={}), CHANNELS[1]]
    result = drill.build_drilldown(channels, 310_000.0, 340_000.0)
    groups = drill.aggregate_room_types(result)
    keys = [group["key"] for group in groups]
    assert "未分配差额（结构缺失/占比异常）" in keys
    # 门店级房型行 + 未分配行 与渠道层对平
    total = sum(round(group["value"] - group["base"], 2) for group in groups)
    assert abs(total - result["reconciliation"]["channel_delta_sum"]) < 0.05


def test_deterministic_output():
    assert build() == build()
    assert drill.mean_mix([MIX_A, MIX_B]) == drill.mean_mix([MIX_A, MIX_B])
