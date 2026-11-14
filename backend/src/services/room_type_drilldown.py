"""渠道 × 房型下钻：按各渠道逐日上报的房型结构分摊，房型→渠道→门店逐层对账。

口径（README §10 同步）：
- 当期房型结构来自 `MetricPoint.room_type_mix`（各渠道逐日上报）；基线结构 = 检测窗口内
  该渠道有效上报占比的算术平均（`mean_mix`），不再使用固定 52/36/12 分摊；
- 占比校验：空/非字典 → `missing_structure`；含非数值或负值、合计偏离 1 超过 ±2% → `abnormal_share`；
  合法占比归一化到合计恰为 1，保证房型层与渠道层严格对平；
- 对账恒等式（每层都成立，差额单列、不摊派、不隐藏）：
  `渠道Δ = Σ房型Δ + 未分配差额`（结构缺失/占比异常时未分配 = 整个渠道Δ，并写明原因）；
  `门店Δ = Σ渠道Δ + 口径差异`（分渠道中位数基线 vs 门店整体中位数基线的差额）；
- 房型键取当期与基线结构的并集：停售房型（当期占比 0）如实显示为真实拖累，
  从无该房型的门店/渠道不会凭空生成该房型行；
- 纯函数、无外部依赖、确定性（同输入同输出）。
"""
from __future__ import annotations

# 归因口径版本：v1 = 固定 52/36/12 分摊；v2 = 渠道逐日上报结构 + 逐层对账。
# 既有归因行永不改写（重跑幂等），版本号用于区分口径，报告与下钻引用同一版本。
ATTRIBUTION_VERSION = 2

EPS = 1e-9
MIX_SUM_TOLERANCE = 0.02  # 占比合计允许 ±2% 浮动（四舍五入尾差），超出视为占比异常
BALANCE_TOLERANCE = 1.0  # 元；对账差异绝对容差
BALANCE_REL_TOLERANCE = 0.01  # 对账差异相对容差（占门店Δ比例），二者取大

STATUS_OK = "ok"
STATUS_MISSING = "missing_structure"
STATUS_ABNORMAL = "abnormal_share"

STATUS_LABELS: dict[str, str] = {
    STATUS_OK: "结构正常",
    STATUS_MISSING: "结构缺失",
    STATUS_ABNORMAL: "占比异常",
}


def validate_mix(mix: object) -> tuple[str, dict[str, float], str]:
    """校验单条房型结构 → (status, 归一化占比, 原因短句)。"""
    if not isinstance(mix, dict) or not mix:
        return STATUS_MISSING, {}, "未上报房型结构"
    cleaned: dict[str, float] = {}
    for key, value in mix.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return STATUS_ABNORMAL, {}, f"房型「{key}」占比非法（{value!r}）"
        cleaned[str(key)] = float(value)
    total = sum(cleaned.values())
    if total <= 0 or abs(total - 1.0) > MIX_SUM_TOLERANCE:
        return STATUS_ABNORMAL, {}, f"占比合计 {total:.2f}，偏离 1 超过 ±{MIX_SUM_TOLERANCE:.0%}"
    return STATUS_OK, {key: value / total for key, value in cleaned.items()}, ""


def mean_mix(mixes: list[dict] | None) -> tuple[str, dict[str, float], str]:
    """窗口内逐日结构的均值作为基线结构；逐日校验，仅采纳合法上报。"""
    valid: list[dict[str, float]] = []
    for mix in mixes or []:
        status, normalized, _ = validate_mix(mix)
        if status == STATUS_OK:
            valid.append(normalized)
    if not valid:
        return STATUS_MISSING, {}, "窗口内无有效房型结构上报"
    keys = sorted({key for mix in valid for key in mix})
    merged = {key: sum(mix.get(key, 0.0) for mix in valid) / len(valid) for key in keys}
    total = sum(merged.values())
    return STATUS_OK, {key: value / total for key, value in merged.items()}, ""


def _contribution(delta: float, store_delta: float) -> float | None:
    if abs(store_delta) <= EPS:
        return None
    return round(delta / store_delta, 6)


def channel_block(
    *,
    channel: str,
    label: str,
    current: float,
    base: float,
    mix_current: object,
    mix_base: object,
    store_delta: float,
) -> dict:
    """单个渠道的房型下钻：房型行 + 未分配差额（含原因），`渠道Δ = Σ房型Δ + 未分配`。"""
    current = round(float(current), 2)
    base = round(float(base), 2)
    delta = round(current - base, 2)
    cur_status, cur_mix, cur_detail = validate_mix(mix_current)
    base_status, base_mix, base_detail = validate_mix(mix_base)

    status = STATUS_OK
    reason = ""
    if cur_status != STATUS_OK:
        status, reason = cur_status, f"当期{cur_detail}"
    elif base_status != STATUS_OK:
        status, reason = base_status, f"基线{base_detail}"

    room_types: list[dict] = []
    if status == STATUS_OK:
        for key in sorted(set(cur_mix) | set(base_mix)):
            share_cur = cur_mix.get(key, 0.0)
            share_base = base_mix.get(key, 0.0)
            rt_current = round(current * share_cur, 2)
            rt_base = round(base * share_base, 2)
            rt_delta = round(rt_current - rt_base, 2)
            room_types.append(
                {
                    "key": key,
                    "share_current": round(share_cur, 4),
                    "share_base": round(share_base, 4),
                    "current": rt_current,
                    "base": rt_base,
                    "delta": rt_delta,
                    "contribution": _contribution(rt_delta, store_delta),
                }
            )
        room_types.sort(key=lambda item: (-abs(item["delta"]), item["key"]))

    allocated = round(sum(row["delta"] for row in room_types), 2)
    gap = round(delta - allocated, 2)
    unallocated = None
    if status != STATUS_OK:
        unallocated = {"delta": gap, "reason": reason}
    elif abs(gap) > 0.01:
        unallocated = {"delta": gap, "reason": "占比归一化尾差"}

    return {
        "channel": channel,
        "label": label,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "current": current,
        "base": base,
        "delta": delta,
        "contribution": _contribution(delta, store_delta),
        "room_types": room_types,
        "allocated_delta": allocated,
        "unallocated": unallocated,
    }


def build_drilldown(channels: list[dict], store_current: float, store_base: float) -> dict:
    """channels: [{channel, label, current, base, mix_current, mix_base}]（mix_base 为 mean_mix 合并结果）。"""
    store_current = round(float(store_current), 2)
    store_base = round(float(store_base), 2)
    store_delta = round(store_current - store_base, 2)
    blocks = [
        channel_block(
            channel=item["channel"],
            label=item["label"],
            current=item["current"],
            base=item["base"],
            mix_current=item.get("mix_current"),
            mix_base=item.get("mix_base"),
            store_delta=store_delta,
        )
        for item in channels
    ]
    channel_current_sum = round(sum(block["current"] for block in blocks), 2)
    channel_delta_sum = round(sum(block["delta"] for block in blocks), 2)
    current_difference = round(store_current - channel_current_sum, 2)
    difference = round(store_delta - channel_delta_sum, 2)
    unallocated_total = round(
        sum(block["unallocated"]["delta"] for block in blocks if block["unallocated"]), 2
    )
    tolerance = max(BALANCE_TOLERANCE, BALANCE_REL_TOLERANCE * abs(store_delta))
    balanced = abs(difference) <= tolerance and abs(current_difference) <= BALANCE_TOLERANCE
    return {
        "label": "渠道 × 房型",
        "version": ATTRIBUTION_VERSION,
        "store_total": {"current": store_current, "base": store_base, "delta": store_delta},
        "channels": blocks,
        "reconciliation": {
            "channel_current_sum": channel_current_sum,
            "current_difference": current_difference,
            "channel_delta_sum": channel_delta_sum,
            "store_delta": store_delta,
            "difference": difference,
            "difference_reason": "渠道基线为分渠道中位数，与门店整体中位数存在口径差，单列不摊派",
            "unallocated_total": unallocated_total,
            "balanced": balanced,
        },
    }


def aggregate_room_types(drilldown: dict) -> list[dict]:
    """把各渠道房型行汇总成门店级房型行（rank_contributions 输入格式）；未分配差额单列一行。"""
    bucket: dict[str, dict[str, float]] = {}
    for block in drilldown.get("channels", []):
        for row in block.get("room_types", []):
            entry = bucket.setdefault(row["key"], {"current": 0.0, "base": 0.0})
            entry["current"] += row["current"]
            entry["base"] += row["base"]
    groups = [
        {"key": key, "value": round(value["current"], 2), "base": round(value["base"], 2)}
        for key, value in sorted(bucket.items())
    ]
    unallocated = drilldown.get("reconciliation", {}).get("unallocated_total", 0.0)
    if abs(unallocated) > 0.01:
        groups.append({"key": "未分配差额（结构缺失/占比异常）", "value": round(unallocated, 2), "base": 0.0})
    return groups
