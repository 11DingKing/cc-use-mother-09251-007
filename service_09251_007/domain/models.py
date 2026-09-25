"""领域模型：只承载数据与可追溯的核算结果，不依赖数据库或框架。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# 告警 / 维修等降级来源
KIND_ALARM = "alarm"
KIND_MAINTENANCE = "maintenance"

# 维修作用范围
SCOPE_GUN = "gun"
SCOPE_CIRCUIT = "circuit"


@dataclass(frozen=True)
class Circuit:
    circuit_id: str
    limit_kw: float
    name: str = ""


@dataclass(frozen=True)
class Gun:
    gun_id: str
    circuit_id: str
    rated_kw: float


@dataclass(frozen=True)
class InputInterval:
    """作用在某把枪上的一段降级区间（回路维修会被展开为每把枪一条）。"""

    kind: str            # alarm | maintenance
    ref: str             # 告警事件号或维修窗口号，用于可追溯
    factor: float        # 可用功率比例，0=全停，1=满功率
    start: datetime
    end: Optional[datetime]  # None 表示尚未解除/结束


@dataclass(frozen=True)
class RuleVersion:
    code: str
    version: int
    params: dict
    effective_from: datetime


@dataclass
class FactorHit:
    kind: str
    ref: str
    factor: float

    def as_dict(self) -> dict:
        return {"kind": self.kind, "ref": self.ref, "factor": self.factor}


@dataclass
class Segment:
    """时间片内部按事件边界切出的小段，支持部分覆盖加权。"""

    offset_from_s: float
    offset_to_s: float
    ratio: float
    factors: list[FactorHit] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "from_s": round(self.offset_from_s, 3),
            "to_s": round(self.offset_to_s, 3),
            "ratio": round(self.ratio, 6),
            "factors": [f.as_dict() for f in self.factors],
        }


@dataclass
class GunSliceResult:
    slice_start: datetime
    gun_id: str
    circuit_id: str
    rated_kw: float
    ratio: float
    effective_kw: float
    segments: list[Segment]
    rule: RuleVersion
    curve: Optional[dict] = None  # {"samples": n, "max_kw": x, "factor": r}

    def basis(self) -> dict:
        return {
            "rated_kw": self.rated_kw,
            "ratio": round(self.ratio, 6),
            "segments": [s.as_dict() for s in self.segments],
            "curve": self.curve,
            "compose": self.rule.params.get("compose", "min"),
            "rule_code": self.rule.code,
            "rule_version": self.rule.version,
        }


@dataclass
class CircuitSliceResult:
    slice_start: datetime
    circuit: Circuit
    member_kw: dict[str, float]
    sum_kw: float
    effective_kw: float
    binding: bool
    rule_versions: list[int]

    def basis(self) -> dict:
        return {
            "limit_kw": self.circuit.limit_kw,
            "sum_kw": round(self.sum_kw, 3),
            "binding": self.binding,
            "members": [
                {"gun_id": g, "effective_kw": round(v, 3)}
                for g, v in sorted(self.member_kw.items())
            ],
            "rule_versions": sorted(set(self.rule_versions)),
        }
