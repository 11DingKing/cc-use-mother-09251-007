"""纯规则引擎：输入设备、区间、曲线与规则版本，输出可追溯的逐片核算结果。

不读写数据库、不接触当前时间，便于对任意历史区间重放。
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from .models import (
    Circuit,
    CircuitSliceResult,
    FactorHit,
    Gun,
    GunSliceResult,
    InputInterval,
    RuleVersion,
    Segment,
)
from .timeline import SLICE

SLICE_SECONDS = SLICE.total_seconds()

DEFAULT_RULE = RuleVersion(
    code="derating-default",
    version=1,
    params={
        "compose": "min",        # 同一枪上多重降级的合成方式
        "curve_min_samples": 2,  # 片内功率样本少于该值则不采信曲线
        "curve_stat": "max",     # 以片内最高输出作为可达能力证据
    },
    effective_from=datetime(1970, 1, 1),
)


def resolve_rule(rules: list[RuleVersion], at: datetime) -> RuleVersion:
    """取生效时间不晚于 at 的最高版本规则。"""
    applicable = [r for r in rules if r.effective_from <= at]
    if not applicable:
        return DEFAULT_RULE
    return max(applicable, key=lambda r: (r.version, r.code))


def _compose(values: list[float], mode: str) -> float:
    if not values:
        return 1.0
    if mode == "multiply":
        p = 1.0
        for v in values:
            p *= v
        return p
    return min(values)  # 默认取最严格的降级


def _build_segments(
    slice_start: datetime,
    intervals: list[InputInterval],
    compose_mode: str,
) -> list[Segment]:
    """按区间边界把时间片切成小段，每段给出因子来源。"""
    boundaries = {0.0, SLICE_SECONDS}
    clipped: list[tuple[float, float, InputInterval]] = []
    for iv in intervals:
        a = max(0.0, (iv.start - slice_start).total_seconds())
        end = iv.end if iv.end is not None else slice_start + SLICE
        b = min(SLICE_SECONDS, (end - slice_start).total_seconds())
        if b <= 0 or a >= SLICE_SECONDS or b <= a:
            continue
        clipped.append((a, b, iv))
        boundaries.add(a)
        boundaries.add(b)

    points = sorted(boundaries)
    segments: list[Segment] = []
    for lo, hi in zip(points, points[1:]):
        if hi <= lo:
            continue
        mid = (lo + hi) / 2
        hits = [
            FactorHit(kind=iv.kind, ref=iv.ref, factor=iv.factor)
            for a, b, iv in clipped
            if a <= mid < b
        ]
        ratio = _compose([h.factor for h in hits], compose_mode)
        segments.append(
            Segment(offset_from_s=lo, offset_to_s=hi, ratio=ratio, factors=hits)
        )
    return segments


def curve_factor(
    samples: list[tuple[datetime, float]],
    slice_start: datetime,
    rated_kw: float,
    rule: RuleVersion,
) -> Optional[dict]:
    """从片内功率样本求曲线因子；样本不足返回 None（不采信）。"""
    slice_end = slice_start + SLICE
    in_slice = [kw for t, kw in samples if slice_start <= t < slice_end]
    min_samples = rule.params.get("curve_min_samples", 2)
    if len(in_slice) < min_samples or rated_kw <= 0:
        return None
    stat = rule.params.get("curve_stat", "max")
    value = max(in_slice) if stat == "max" else sum(in_slice) / len(in_slice)
    return {
        "samples": len(in_slice),
        "max_kw": round(max(in_slice), 3),
        "factor": round(min(1.0, max(0.0, value / rated_kw)), 6),
    }


def calc_gun_slice(
    slice_start: datetime,
    gun: Gun,
    intervals: list[InputInterval],
    samples: list[tuple[datetime, float]],
    rule: RuleVersion,
) -> GunSliceResult:
    segments = _build_segments(slice_start, intervals, rule.params.get("compose", "min"))
    ratio = sum(
        (s.offset_to_s - s.offset_from_s) / SLICE_SECONDS * s.ratio for s in segments
    )
    curve = curve_factor(samples, slice_start, gun.rated_kw, rule)
    if curve is not None:
        ratio = min(ratio, curve["factor"])
    return GunSliceResult(
        slice_start=slice_start,
        gun_id=gun.gun_id,
        circuit_id=gun.circuit_id,
        rated_kw=gun.rated_kw,
        ratio=round(min(1.0, max(0.0, ratio)), 6),
        effective_kw=round(gun.rated_kw * ratio, 3),
        segments=segments,
        rule=rule,
        curve=curve,
    )


def cap_circuit_slice(
    slice_start: datetime,
    circuit: Circuit,
    gun_results: list[GunSliceResult],
) -> CircuitSliceResult:
    """同一配电回路的共同上限：先逐枪降级，再受回路总额约束。"""
    member_kw = {g.gun_id: g.effective_kw for g in gun_results}
    sum_kw = round(sum(member_kw.values()), 3)
    effective = round(min(sum_kw, circuit.limit_kw), 3)
    return CircuitSliceResult(
        slice_start=slice_start,
        circuit=circuit,
        member_kw=member_kw,
        sum_kw=sum_kw,
        effective_kw=effective,
        binding=sum_kw > circuit.limit_kw + 1e-9,
        rule_versions=[g.rule.version for g in gun_results],
    )


def evaluate(
    guns: Iterable[Gun],
    circuits: Iterable[Circuit],
    intervals_by_gun: dict[str, list[InputInterval]],
    samples_by_gun: dict[str, list[tuple[datetime, float]]],
    rules: list[RuleVersion],
    slice_starts: list[datetime],
) -> dict:
    """对给定时间片集合做完整核算，返回逐枪、逐回路与总量结果。"""
    guns = list(guns)
    circuit_map = {c.circuit_id: c for c in circuits}
    gun_results: dict[datetime, dict[str, GunSliceResult]] = {}
    circuit_results: dict[str, list[CircuitSliceResult]] = {
        cid: [] for cid in circuit_map
    }

    for start in slice_starts:
        rule = resolve_rule(rules, start)
        per_slice: dict[str, GunSliceResult] = {}
        by_circuit: dict[str, list[GunSliceResult]] = {}
        for gun in guns:
            res = calc_gun_slice(
                start,
                gun,
                intervals_by_gun.get(gun.gun_id, []),
                samples_by_gun.get(gun.gun_id, []),
                rule,
            )
            per_slice[gun.gun_id] = res
            by_circuit.setdefault(gun.circuit_id, []).append(res)
        gun_results[start] = per_slice
        for cid, members in by_circuit.items():
            if cid in circuit_map:
                circuit_results[cid].append(
                    cap_circuit_slice(start, circuit_map[cid], members)
                )

    totals = {}
    for start in slice_starts:
        totals[start] = round(
            sum(
                cr.effective_kw
                for crs in circuit_results.values()
                for cr in crs
                if cr.slice_start == start
            ),
            3,
        )
    return {
        "guns": gun_results,
        "circuits": circuit_results,
        "totals": totals,
    }
