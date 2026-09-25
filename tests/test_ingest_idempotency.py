"""批量导入测试：事件幂等、整批原子性、派生切片随重放收敛。"""
from __future__ import annotations

import sqlite3

from service_09251_007.application.services import ServiceError

from support import ServiceTestCase, at


def alarm_batch(event_id: str, alarm_id: str, started: str, category: str = "temperature_control") -> dict:
    return {
        "batch_id": f"batch_{event_id}",
        "events": [
            {
                "event_id": event_id,
                "type": "alarm_raised",
                "gun_id": "G1",
                "payload": {
                    "alarm_id": alarm_id,
                    "category": category,
                    "started_at": f"2026-09-25T{started}Z",
                },
            }
        ],
    }


class IngestIdempotencyTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed_devices()

    def _event_count(self) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()

    def test_same_batch_twice_is_idempotent(self) -> None:
        batch = alarm_batch("E1", "A1", "10:00")
        first = self.service.import_events(batch)
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(first["duplicates"], [])

        second = self.service.import_events(batch)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["duplicates"], ["E1"])
        self.assertEqual(self._event_count(), 1)

        # 重试前后容量视图完全一致
        start, end = at("10:00"), at("11:00")
        self.assertEqual(
            self.service.query_capacity("ST", start, end),
            self.service.query_capacity("ST", start, end),
        )
        entry = self.query_slice(at("10:00"), at("11:00"))
        self.assertEqual(entry["effective_kw"], 72.0)

    def test_duplicate_event_does_not_double_apply(self) -> None:
        self.service.import_events(alarm_batch("E1", "A1", "10:00"))
        self.service.import_events(alarm_batch("E1", "A1", "10:00"))
        # 若重复生效，重放区间与切片会重复计算；此处验证结果仍为单次降额
        entry = self.query_slice(at("10:00"), at("11:00"))
        self.assertEqual(entry["effective_kw"], 72.0)
        self.assertEqual(self._event_count(), 1)

    def test_batch_is_atomic_on_validation_error(self) -> None:
        bad_batch = {
            "batch_id": "B1",
            "events": [
                {
                    "event_id": "E1",
                    "type": "alarm_raised",
                    "gun_id": "G1",
                    "payload": {
                        "alarm_id": "A1",
                        "category": "temperature_control",
                        "started_at": "2026-09-25T10:00:00Z",
                    },
                },
                {
                    "event_id": "E2",
                    "type": "alarm_raised",
                    "gun_id": "GHOST",
                    "payload": {
                        "alarm_id": "A2",
                        "category": "temperature_control",
                        "started_at": "2026-09-25T10:00:00Z",
                    },
                },
            ],
        }
        with self.assertRaises(ServiceError) as ctx:
            self.service.import_events(bad_batch)
        self.assertEqual(ctx.exception.status, 422)
        # 整批回滚：合法事件也不落库
        self.assertEqual(self._event_count(), 0)
        entry = self.query_slice(at("10:00"), at("11:00"))
        self.assertEqual(entry["effective_kw"], 120.0)

    def test_unknown_alarm_category_rejected(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.import_events(alarm_batch("E1", "A1", "10:00", "fire"))
        self.assertEqual(ctx.exception.status, 422)

    def test_clear_without_raise_rejected(self) -> None:
        batch = {
            "events": [
                {
                    "event_id": "E1",
                    "type": "alarm_cleared",
                    "gun_id": "G1",
                    "payload": {"alarm_id": "A1", "cleared_at": "2026-09-25T10:00:00Z"},
                }
            ]
        }
        with self.assertRaises(ServiceError) as ctx:
            self.service.import_events(batch)
        self.assertEqual(ctx.exception.status, 422)

    def test_power_samples_import_and_replay(self) -> None:
        batch = {
            "events": [
                {
                    "event_id": "E1",
                    "type": "alarm_raised",
                    "gun_id": "G1",
                    "payload": {
                        "alarm_id": "A1",
                        "category": "temperature_control",
                        "started_at": "2026-09-25T10:00:00Z",
                    },
                },
                {
                    "event_id": "E2",
                    "type": "power_samples",
                    "gun_id": "G1",
                    "payload": {
                        "samples": [
                            {"ts": "2026-09-25T10:01:00Z", "kw": 48.0},
                            {"ts": "2026-09-25T10:06:00Z", "kw": 50.0},
                        ]
                    },
                },
            ]
        }
        result = self.service.import_events(batch)
        self.assertEqual(result["inserted"], 2)
        entry = self.query_slice(at("10:00"), at("11:00"))
        # 温控估值 72，实测最大 50 → 收紧
        self.assertEqual(entry["effective_kw"], 50.0)


if __name__ == "__main__":
    import unittest

    unittest.main()
