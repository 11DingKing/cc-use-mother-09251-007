"""测试公共支撑：固定时钟、确定性标识、临时数据库与设备种子数据。"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from service_09251_007.application.services import CapacityService
from service_09251_007.domain.timeutil import parse_iso8601
from service_09251_007.ports.clock import FixedClock
from service_09251_007.ports.ids import SequentialIds
from service_09251_007.ports.signing import HmacSigner

# 所有测试围绕固定“现在”展开：2026-09-25T12:00:00Z。
NOW = parse_iso8601("2026-09-25T12:00:00Z")
DAY = "2026-09-25"
NEXT_DAY = "2026-09-26"

TEST_KEY = bytes.fromhex("00" * 32)


def at(time_str: str, day: str = DAY) -> int:
    """快捷构造当天某个时刻的 epoch 秒。"""
    return parse_iso8601(f"{day}T{time_str}Z")


class ServiceTestCase(unittest.TestCase):
    """提供注入固定时钟/确定性标识的服务实例与临时数据库。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="capacity_test_")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.clock = FixedClock(NOW)
        self.service = self.make_service()

    def make_service(self) -> CapacityService:
        """构造服务；每次调用生成独立的确定性标识序列。"""
        return CapacityService(
            self.db_path,
            clock=self.clock,
            ids=SequentialIds(),
            signer=HmacSigner(TEST_KEY),
        )

    def seed_devices(self, limit_kw: float = 10_000.0, guns: int = 2) -> None:
        """登记一个场站：一个回路 + 若干 120kW 充电枪。"""
        self.service.import_devices(
            {
                "circuits": [
                    {"circuit_id": "C1", "station_id": "ST", "limit_kw": limit_kw}
                ],
                "guns": [
                    {
                        "gun_id": f"G{i + 1}",
                        "station_id": "ST",
                        "circuit_id": "C1",
                        "rated_kw": 120,
                    }
                    for i in range(guns)
                ],
            }
        )

    def query_slice(self, start: int, end: int, gun_id: str = "G1") -> dict:
        """查询并返回指定枪在指定片（起点对齐）的条目。"""
        result = self.service.query_capacity("ST", start, end)
        target = None
        for slice_entry in result["slices"]:
            if parse_iso8601(slice_entry["slice_start"]) != start:
                continue
            for gun_entry in slice_entry["guns"]:
                if gun_entry["gun_id"] == gun_id:
                    target = gun_entry
        assert target is not None, f"未找到 {gun_id} 在 {start} 的切片"
        return target
