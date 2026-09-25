"""领域模型：核算所需的不可变输入结构。

这些 dataclass 是纯数据载体，由持久化层从事件日志与设备档案装配，
由 capacity 模块的纯函数消费；不依赖时钟、数据库或网络，保证核算
结果只取决于输入，从而可重放、可审计。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 告警类别：温控 / 配电 / 通信，其余归入 unknown 使用兜底降额系数。
CATEGORY_TEMPERATURE = "temperature_control"
CATEGORY_DISTRIBUTION = "power_distribution"
CATEGORY_COMMUNICATION = "communication"
CATEGORY_UNKNOWN = "unknown"

KNOWN_CATEGORIES = frozenset(
    {CATEGORY_TEMPERATURE, CATEGORY_DISTRIBUTION, CATEGORY_COMMUNICATION}
)


@dataclass(frozen=True)
class Gun:
    """一把充电枪。circuit_id 为 None 表示未挂接配电回路（不受回路限额约束）。"""

    gun_id: str
    station_id: str
    rated_kw: float
    circuit_id: str | None = None


@dataclass(frozen=True)
class Circuit:
    """配电回路：同回路各枪有效容量之和不得超过 limit_kw。"""

    circuit_id: str
    station_id: str
    limit_kw: float


@dataclass(frozen=True)
class AlarmInterval:
    """一次告警的活动区间；cleared_at 为 None 表示仍未解除。"""

    alarm_id: str
    gun_id: str
    category: str
    started_at: int
    cleared_at: int | None

    def active_during(self, start: int, end: int) -> bool:
        cleared = self.cleared_at if self.cleared_at is not None else end
        return self.started_at < end and cleared > start


@dataclass(frozen=True)
class MaintenanceWindow:
    """维修窗口：窗口内充电枪容量置零。status 为 cancelled 时不参与核算。"""

    window_id: str
    gun_id: str
    start_ts: int
    end_ts: int
    reason: str = ""
    status: str = "scheduled"

    def active_during(self, start: int, end: int) -> bool:
        return (
            self.status != "cancelled"
            and self.start_ts < end
            and self.end_ts > start
        )


@dataclass(frozen=True)
class PowerSample:
    """一次实测功率采样。"""

    gun_id: str
    ts: int
    kw: float


@dataclass(frozen=True)
class RuleSet:
    """一版可追溯的核算规则。

    factors: 各告警类别的降额系数（有效容量 = 额定 × 系数）。
    observed_power_cap: 存在活动降额告警时，是否用片内实测最大功率
        进一步收紧估值（只收紧不放松，方向保守）。
    """

    version: int
    effective_from: int
    factors: dict[str, float]
    observed_power_cap: bool = True
    description: str = ""

    def factor_for(self, category: str) -> float:
        if category in self.factors:
            return self.factors[category]
        return self.factors.get(CATEGORY_UNKNOWN, 0.5)


@dataclass
class Factor:
    """切片解释中的一个影响因子，构成可追溯证据链。"""

    kind: str  # alarm | maintenance | observed_power | circuit_limit
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, **self.detail}


@dataclass
class GunSlice:
    """一把枪在一个时间片内的核算结果。"""

    gun_id: str
    slice_start: int
    rule_version: int
    rated_kw: float
    effective_kw: float  # 枪级有效容量（回路分摊前）
    allocated_kw: float  # 回路限额分摊后的入账容量
    factors: list[Factor] = field(default_factory=list)

    def explanation(self) -> dict:
        return {
            "gun_id": self.gun_id,
            "slice_start": self.slice_start,
            "rule_version": self.rule_version,
            "rated_kw": self.rated_kw,
            "effective_kw": self.effective_kw,
            "allocated_kw": self.allocated_kw,
            "factors": [f.to_dict() for f in self.factors],
        }
