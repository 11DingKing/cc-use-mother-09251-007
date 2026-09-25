"""时钟端口：生产用真实时钟，测试注入固定时钟。"""
from __future__ import annotations

import time


class Clock:
    """返回当前 UTC epoch 秒。"""

    def now(self) -> int:
        return int(time.time())


class FixedClock(Clock):
    """测试用固定时钟，可手动推进。"""

    def __init__(self, epoch_seconds: int):
        self._now = int(epoch_seconds)

    def now(self) -> int:
        return self._now

    def advance(self, seconds: int) -> None:
        self._now += int(seconds)
