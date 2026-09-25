"""时间端口：业务代码通过 Clock 取当前时间，测试可注入固定时钟。"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now().replace(microsecond=0)


class FixedClock:
    def __init__(self, moment: datetime):
        self._moment = moment

    def now(self) -> datetime:
        return self._moment

    def advance_to(self, moment: datetime) -> None:
        self._moment = moment
