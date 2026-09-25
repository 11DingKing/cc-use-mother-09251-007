"""领域核算规则测试：重叠降级、跨午夜维修、回路限额、实测功率收紧。"""
from __future__ import annotations

import unittest

from service_09251_007.domain.capacity import compute_station_slices
from service_09251_007.domain.models import (
    AlarmInterval,
    Circuit,
    Gun,
    MaintenanceWindow,
    PowerSample,
    RuleSet,
)
from service_09251_007.domain.timeutil import parse_iso8601

RULES = [
    RuleSet(
        version=1,
        effective_from=0,
        factors={
            "temperature_control": 0.6,
            "power_distribution": 0.7,
            "communication": 0.8,
            "unknown": 0.5,
        },
    )
]

GUN = Gun(gun_id="G1", station_id="ST", circuit_id=None, rated_kw=120.0)


def at(time_str: str, day: str = "2026-09-25") -> int:
    return parse_iso8601(f"{day}T{time_str}Z")


def compute(guns, circuits, alarms, windows, samples, start, end):
    return compute_station_slices(
        guns=guns,
        circuits=circuits,
        alarms=alarms,
        windows=windows,
        samples=samples,
        rules=RULES,
        start=start,
        end=end,
    )


def slice_of(result, gun_id, slice_start):
    for item in result:
        if item.gun_id == gun_id and item.slice_start == slice_start:
            return item
    raise AssertionError(f"缺少切片: {gun_id} @ {slice_start}")


class OverlappingDerateTests(unittest.TestCase):
    """多个告警重叠时取最严格降额，解除后回落到次严格。"""

    def test_overlapping_alarms_take_most_restrictive(self) -> None:
        alarms = [
            AlarmInterval("A1", "G1", "temperature_control", at("10:00"), at("11:00")),
            AlarmInterval("A2", "G1", "communication", at("10:30"), at("11:30")),
        ]
        result = compute([GUN], [], alarms, [], [], at("10:00"), at("12:00"))

        # 仅温控：120 × 0.6 = 72
        self.assertEqual(slice_of(result, "G1", at("10:00")).effective_kw, 72.0)
        # 重叠段：min(72, 96) = 72，温控为最严格约束
        overlap = slice_of(result, "G1", at("10:30"))
        self.assertEqual(overlap.effective_kw, 72.0)
        alarm_factors = [f for f in overlap.factors if f.kind == "alarm"]
        self.assertEqual(len(alarm_factors), 2)
        binding = [f for f in alarm_factors if f.detail["binding"]]
        self.assertEqual([f.detail["alarm_id"] for f in binding], ["A1"])
        # 温控解除后只剩通信：120 × 0.8 = 96
        self.assertEqual(slice_of(result, "G1", at("11:00")).effective_kw, 96.0)
        # 全部解除后恢复额定
        self.assertEqual(slice_of(result, "G1", at("11:30")).effective_kw, 120.0)

    def test_partial_overlap_within_slice_is_time_weighted(self) -> None:
        alarms = [
            AlarmInterval("A1", "G1", "unknown", at("10:05"), at("10:10")),
        ]
        result = compute([GUN], [], alarms, [], [], at("10:00"), at("10:15"))
        # 片内 5 分钟降额至 60，10 分钟额定：(10×120 + 5×60) / 15 = 100
        self.assertEqual(slice_of(result, "G1", at("10:00")).effective_kw, 100.0)

    def test_maintenance_overrides_alarm(self) -> None:
        alarms = [
            AlarmInterval("A1", "G1", "temperature_control", at("10:00"), at("11:00")),
        ]
        windows = [
            MaintenanceWindow("W1", "G1", at("10:15"), at("10:45"), "更换模块"),
        ]
        result = compute([GUN], [], alarms, windows, [], at("10:00"), at("11:00"))
        # 10:15~10:45 维修置零，其余时间温控降额 72
        self.assertEqual(slice_of(result, "G1", at("10:00")).effective_kw, 72.0)
        self.assertEqual(slice_of(result, "G1", at("10:15")).effective_kw, 0.0)
        self.assertEqual(slice_of(result, "G1", at("10:30")).effective_kw, 0.0)
        self.assertEqual(slice_of(result, "G1", at("10:45")).effective_kw, 72.0)


class CrossMidnightMaintenanceTests(unittest.TestCase):
    """跨午夜维修窗口在相邻两天的切片上正确置零与恢复。"""

    def test_window_spanning_midnight(self) -> None:
        windows = [
            MaintenanceWindow(
                "W1", "G1", at("22:00"), at("02:00", "2026-09-26"), "夜间检修"
            ),
        ]
        result = compute(
            [GUN], [], [], windows, [], at("21:00"), at("03:00", "2026-09-26")
        )
        self.assertEqual(slice_of(result, "G1", at("21:45")).effective_kw, 120.0)
        for hhmm in ("22:00", "22:15", "22:30", "22:45", "23:00", "23:30", "23:45"):
            self.assertEqual(slice_of(result, "G1", at(hhmm)).effective_kw, 0.0, hhmm)
        for hhmm in ("00:00", "00:30", "01:00", "01:45"):
            self.assertEqual(
                slice_of(result, "G1", at(hhmm, "2026-09-26")).effective_kw, 0.0, hhmm
            )
        self.assertEqual(
            slice_of(result, "G1", at("02:00", "2026-09-26")).effective_kw, 120.0
        )

    def test_window_crossing_midnight_inside_slices(self) -> None:
        windows = [
            MaintenanceWindow(
                "W1", "G1", at("23:55"), at("00:10", "2026-09-26"), "跨零点短修"
            ),
        ]
        result = compute(
            [GUN], [], [], windows, [], at("23:45"), at("00:15", "2026-09-26")
        )
        # 23:45~24:00：10 分钟额定 + 5 分钟置零 → 80
        self.assertEqual(slice_of(result, "G1", at("23:45")).effective_kw, 80.0)
        # 00:00~00:15：10 分钟置零 + 5 分钟额定 → 40
        self.assertEqual(
            slice_of(result, "G1", at("00:00", "2026-09-26")).effective_kw, 40.0
        )


class CircuitLimitTests(unittest.TestCase):
    """同一配电回路的共同上限：超限按比例分摊，未超限不压缩。"""

    def setUp(self) -> None:
        self.circuit = Circuit("C1", "ST", limit_kw=200.0)
        self.g1 = Gun("G1", "ST", rated_kw=120.0, circuit_id="C1")
        self.g2 = Gun("G2", "ST", rated_kw=120.0, circuit_id="C1")

    def test_prorata_allocation_when_over_limit(self) -> None:
        result = compute(
            [self.g1, self.g2], [self.circuit], [], [], [], at("10:00"), at("10:15")
        )
        # 需求 240 > 限额 200：各分得 120 × 200/240 = 100
        g1 = slice_of(result, "G1", at("10:00"))
        g2 = slice_of(result, "G2", at("10:00"))
        self.assertEqual(g1.effective_kw, 120.0)
        self.assertEqual(g1.allocated_kw, 100.0)
        self.assertEqual(g2.allocated_kw, 100.0)
        self.assertEqual(g1.allocated_kw + g2.allocated_kw, 200.0)
        factor = next(f for f in g1.factors if f.kind == "circuit_limit")
        self.assertEqual(factor.detail["circuit_id"], "C1")
        self.assertEqual(factor.detail["limit_kw"], 200.0)

    def test_no_scaling_when_under_limit(self) -> None:
        alarms = [
            AlarmInterval("A1", "G1", "unknown", at("10:00"), at("11:00")),
        ]
        result = compute(
            [self.g1, self.g2], [self.circuit], alarms, [], [], at("10:00"), at("10:15")
        )
        # G1 降额至 60，需求 180 < 200：不压缩
        self.assertEqual(slice_of(result, "G1", at("10:00")).allocated_kw, 60.0)
        self.assertEqual(slice_of(result, "G2", at("10:00")).allocated_kw, 120.0)

    def test_derated_gun_frees_capacity_for_peers(self) -> None:
        alarms = [
            AlarmInterval("A1", "G1", "temperature_control", at("10:00"), at("11:00")),
        ]
        g3 = Gun("G3", "ST", rated_kw=120.0, circuit_id="C1")
        result = compute(
            [self.g1, self.g2, g3],
            [self.circuit],
            alarms,
            [],
            [],
            at("10:00"),
            at("10:15"),
        )
        # 需求 72+120+120=312 > 200：按比例 200/312 分摊
        scale = 200.0 / 312.0
        self.assertAlmostEqual(
            slice_of(result, "G1", at("10:00")).allocated_kw, round(72 * scale + 1e-9, 3)
        )
        total = sum(
            slice_of(result, g, at("10:00")).allocated_kw for g in ("G1", "G2", "G3")
        )
        self.assertAlmostEqual(total, 200.0, places=2)


class ObservedPowerCapTests(unittest.TestCase):
    """实测功率曲线只在存在降额告警时收紧估值，绝不放松。"""

    def test_observed_power_tightens_derated_estimate(self) -> None:
        alarms = [
            AlarmInterval("A1", "G1", "temperature_control", at("10:00"), at("11:00")),
        ]
        samples = [
            PowerSample("G1", at("10:01"), 45.0),
            PowerSample("G1", at("10:07"), 50.0),
        ]
        result = compute([GUN], [], alarms, [], samples, at("10:00"), at("10:15"))
        entry = slice_of(result, "G1", at("10:00"))
        # 温控估值 72，实测最大 50 → 收紧至 50
        self.assertEqual(entry.effective_kw, 50.0)
        self.assertTrue(any(f.kind == "observed_power" for f in entry.factors))

    def test_observed_power_ignored_without_alarm(self) -> None:
        samples = [PowerSample("G1", at("10:01"), 50.0)]
        result = compute([GUN], [], [], [], samples, at("10:00"), at("10:15"))
        # 无告警时低功率只是需求不足，不压低能力
        self.assertEqual(slice_of(result, "G1", at("10:00")).effective_kw, 120.0)

    def test_observed_power_never_relaxes(self) -> None:
        alarms = [
            AlarmInterval("A1", "G1", "temperature_control", at("10:00"), at("11:00")),
        ]
        samples = [PowerSample("G1", at("10:01"), 100.0)]
        result = compute([GUN], [], alarms, [], samples, at("10:00"), at("10:15"))
        # 实测 100 高于温控估值 72：不放松
        self.assertEqual(slice_of(result, "G1", at("10:00")).effective_kw, 72.0)


if __name__ == "__main__":
    unittest.main()
