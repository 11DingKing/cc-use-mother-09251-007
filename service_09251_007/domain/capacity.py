"""容量核算引擎：把设备、事件与规则版本映射为逐时间片的有效容量。

纯函数实现：相同输入必然产生相同输出，因此任意区间都可安全重放；
每个切片同时产出解释（factors），说明结果由哪些告警、维修窗口、
实测功率与回路限额共同决定。

核算规则（按命中顺序，全部可追溯）：
1. 维修窗口覆盖的分片容量置零；
2. 活动告警按规则版本的类别系数降额，多个告警重叠时取最严格值
   （不做连乘，避免重复惩罚同一物理限制）；
3. 存在活动降额告警且片内有实测功率时，用实测最大功率收紧估值
   （只收紧不放松；无告警时低功率只代表需求不足，不压低能力）；
4. 同一配电回路按分片瞬时求和，超过回路限额时按比例分摊。
"""
from __future__ import annotations

from .timeutil import SLICE_SECONDS, format_iso8601, slice_starts
from .models import (
    AlarmInterval,
    Circuit,
    Factor,
    Gun,
    GunSlice,
    MaintenanceWindow,
    PowerSample,
    RuleSet,
)


def _round(kw: float) -> float:
    """统一数值精度，保证重放与快照的字节级稳定。"""
    return round(kw + 1e-9, 3)


def rule_for(rules: list[RuleSet], slice_start: int) -> RuleSet:
    """选取对切片生效的规则版本：effective_from <= slice_start 的最新版。"""
    applicable = [r for r in rules if r.effective_from <= slice_start]
    if not applicable:
        raise ValueError(f"没有覆盖 {slice_start} 的规则版本")
    return max(applicable, key=lambda r: (r.effective_from, r.version))


def _gun_fragments(
    gun: Gun,
    slice_start: int,
    slice_end: int,
    alarms: list[AlarmInterval],
    windows: list[MaintenanceWindow],
    observed_max: float | None,
    rule: RuleSet,
) -> tuple[list[tuple[int, int, float]], list[Factor]]:
    """计算一把枪在单个时间片内的分片容量与影响因子。

    返回 (fragments, factors)：fragments 为 [(起, 止, 有效kW)]，完整覆盖
    [slice_start, slice_end)；factors 为去重后的影响因子列表。
    """
    boundaries = {slice_start, slice_end}
    relevant_alarms = [a for a in alarms if a.active_during(slice_start, slice_end)]
    relevant_windows = [w for w in windows if w.active_during(slice_start, slice_end)]
    for alarm in relevant_alarms:
        boundaries.add(max(slice_start, alarm.started_at))
        if alarm.cleared_at is not None:
            boundaries.add(min(slice_end, alarm.cleared_at))
    for window in relevant_windows:
        boundaries.add(max(slice_start, window.start_ts))
        boundaries.add(min(slice_end, window.end_ts))
    ordered = sorted(boundaries)

    fragments: list[tuple[int, int, float]] = []
    factors: list[Factor] = []
    for f0, f1 in zip(ordered, ordered[1:]):
        if f0 >= f1:
            continue
        active_windows = [w for w in relevant_windows if w.active_during(f0, f1)]
        if active_windows:
            for window in active_windows:
                factors.append(
                    Factor(
                        "maintenance",
                        {
                            "window_id": window.window_id,
                            "window_start": format_iso8601(window.start_ts),
                            "window_end": format_iso8601(window.end_ts),
                            "reason": window.reason,
                        },
                    )
                )
            fragments.append((f0, f1, 0.0))
            continue

        active_alarms = [a for a in relevant_alarms if a.active_during(f0, f1)]
        effective = gun.rated_kw
        if active_alarms:
            # 重叠降级取最严格值；全部活动告警都记入解释，标记最严格者。
            derated = [
                (rule.factor_for(a.category) * gun.rated_kw, a) for a in active_alarms
            ]
            binding_value = min(value for value, _ in derated)
            for value, alarm in derated:
                factors.append(
                    Factor(
                        "alarm",
                        {
                            "alarm_id": alarm.alarm_id,
                            "category": alarm.category,
                            "factor": rule.factor_for(alarm.category),
                            "derated_kw": _round(value),
                            "binding": value == binding_value,
                            "alarm_started_at": format_iso8601(alarm.started_at),
                            "alarm_cleared_at": (
                                format_iso8601(alarm.cleared_at)
                                if alarm.cleared_at is not None
                                else None
                            ),
                        },
                    )
                )
            effective = binding_value
            if (
                rule.observed_power_cap
                and observed_max is not None
                and observed_max < effective
            ):
                factors.append(
                    Factor(
                        "observed_power",
                        {"observed_max_kw": _round(observed_max)},
                    )
                )
                effective = observed_max
        fragments.append((f0, f1, effective))

    # 去重因子（同一告警/窗口可能跨多个分片重复出现）。
    unique: dict[tuple, Factor] = {}
    for factor in factors:
        key = (
            factor.kind,
            factor.detail.get("alarm_id")
            or factor.detail.get("window_id")
            or factor.kind,
        )
        unique[key] = factor
    return fragments, list(unique.values())


def compute_station_slices(
    guns: list[Gun],
    circuits: list[Circuit],
    alarms: list[AlarmInterval],
    windows: list[MaintenanceWindow],
    samples: list[PowerSample],
    rules: list[RuleSet],
    start: int,
    end: int,
    slice_seconds: int = SLICE_SECONDS,
) -> list[GunSlice]:
    """核算一个场站在 [start, end) 内每把枪每个时间片的有效容量。"""
    if end <= start:
        return []
    alarms_by_gun: dict[str, list[AlarmInterval]] = {}
    for alarm in alarms:
        alarms_by_gun.setdefault(alarm.gun_id, []).append(alarm)
    windows_by_gun: dict[str, list[MaintenanceWindow]] = {}
    for window in windows:
        windows_by_gun.setdefault(window.gun_id, []).append(window)
    samples_by_gun: dict[str, list[PowerSample]] = {}
    for sample in samples:
        samples_by_gun.setdefault(sample.gun_id, []).append(sample)
    circuits_by_id = {c.circuit_id: c for c in circuits}
    guns_by_circuit: dict[str, list[Gun]] = {}
    for gun in guns:
        if gun.circuit_id:
            guns_by_circuit.setdefault(gun.circuit_id, []).append(gun)

    results: list[GunSlice] = []
    for slice_start in slice_starts(start, end, slice_seconds):
        slice_end = slice_start + slice_seconds
        rule = rule_for(rules, slice_start)

        # 第一步：枪级分片容量（不含回路限额）。
        per_gun: dict[str, tuple[list[tuple[int, int, float]], list[Factor], float]] = {}
        for gun in guns:
            gun_samples = samples_by_gun.get(gun.gun_id, [])
            in_slice = [s.kw for s in gun_samples if slice_start <= s.ts < slice_end]
            observed_max = max(in_slice) if in_slice else None
            fragments, factors = _gun_fragments(
                gun,
                slice_start,
                slice_end,
                alarms_by_gun.get(gun.gun_id, []),
                windows_by_gun.get(gun.gun_id, []),
                observed_max,
                rule,
            )
            weighted = sum((f1 - f0) * kw for f0, f1, kw in fragments) / slice_seconds
            per_gun[gun.gun_id] = (fragments, factors, weighted)

        # 第二步：回路限额分摊（按分片瞬时求和，超限按比例压缩）。
        allocated: dict[str, float] = {}
        circuit_factors: dict[str, Factor] = {}
        for circuit_id, circuit_guns in guns_by_circuit.items():
            circuit = circuits_by_id[circuit_id]
            boundaries = {slice_start, slice_end}
            for gun in circuit_guns:
                for f0, f1, _ in per_gun[gun.gun_id][0]:
                    boundaries.add(f0)
                    boundaries.add(f1)
            ordered = sorted(boundaries)
            for f0, f1 in zip(ordered, ordered[1:]):
                if f0 >= f1:
                    continue
                duration = f1 - f0
                instant: dict[str, float] = {}
                for gun in circuit_guns:
                    for g0, g1, kw in per_gun[gun.gun_id][0]:
                        if g0 <= f0 and f1 <= g1:
                            instant[gun.gun_id] = kw
                            break
                total = sum(instant.values())
                scale = min(1.0, circuit.limit_kw / total) if total > 0 else 1.0
                for gun_id, kw in instant.items():
                    allocated[gun_id] = (
                        allocated.get(gun_id, 0.0) + duration * kw * scale / slice_seconds
                    )
                if scale < 1.0:
                    for gun_id in instant:
                        circuit_factors[gun_id] = Factor(
                            "circuit_limit",
                            {
                                "circuit_id": circuit_id,
                                "limit_kw": circuit.limit_kw,
                                "demand_kw": _round(total),
                                "scale": _round(scale),
                            },
                        )
        for gun in guns:
            if gun.gun_id not in allocated:
                allocated[gun.gun_id] = per_gun[gun.gun_id][2]

        # 第三步：汇总成切片结果。
        for gun in guns:
            fragments, factors, weighted = per_gun[gun.gun_id]
            all_factors = list(factors)
            if gun.gun_id in circuit_factors:
                all_factors.append(circuit_factors[gun.gun_id])
            results.append(
                GunSlice(
                    gun_id=gun.gun_id,
                    slice_start=slice_start,
                    rule_version=rule.version,
                    rated_kw=gun.rated_kw,
                    effective_kw=_round(weighted),
                    allocated_kw=_round(allocated[gun.gun_id]),
                    factors=all_factors,
                )
            )
    return results
