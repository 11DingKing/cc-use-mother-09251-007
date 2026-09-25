"""标识端口：集中生成各类业务标识，测试可注入确定性序列。"""
from __future__ import annotations

import itertools
import uuid


class Ids:
    """默认实现：uuid4 后缀保证全局唯一。"""

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:16]}"


class SequentialIds(Ids):
    """测试用确定性标识：prefix_000001、prefix_000002……"""

    def __init__(self):
        self._counter = itertools.count(1)

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._counter):06d}"
