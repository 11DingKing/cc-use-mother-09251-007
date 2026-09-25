"""重放与规则升级测试：告警解除补录、规则版本生效、派生态恢复重建。"""
from __future__ import annotations

import sqlite3
import unittest

from support import ServiceTestCase, at


class LateClearReplayTests(ServiceTestCase):
    """告警解除事件补录后，受影响历史区间被重放修正。"""

    def setUp(self) -> None:
        super().setUp()
        self.seed_devices()
        self.service.import_events(
            {
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
                    }
                ]
            }
        )

    def test_late_clear_backfill_replays_affected_range(self) -> None:
        before = self.query_slice(at("10:30"), at("11:00"))
        self.assertEqual(before["effective_kw"], 72.0)

        # 补录：告警实际在 10:30 就已解除
        self.service.import_events(
            {
                "events": [
                    {
                        "event_id": "E2",
                        "type": "alarm_cleared",
                        "gun_id": "G1",
                        "payload": {
                            "alarm_id": "A1",
                            "cleared_at": "2026-09-25T10:30:00Z",
                        },
                    }
                ]
            }
        )
        self.assertEqual(self.query_slice(at("10:15"), at("11:00"))["effective_kw"], 72.0)
        self.assertEqual(self.query_slice(at("10:30"), at("11:00"))["effective_kw"], 120.0)
        self.assertEqual(self.query_slice(at("11:00"), at("12:00"))["effective_kw"], 120.0)

    def test_clear_replay_is_idempotent(self) -> None:
        clear_event = {
            "events": [
                {
                    "event_id": "E2",
                    "type": "alarm_cleared",
                    "gun_id": "G1",
                    "payload": {"alarm_id": "A1", "cleared_at": "2026-09-25T10:30:00Z"},
                }
            ]
        }
        self.service.import_events(clear_event)
        first = self.service.query_capacity("ST", at("10:00"), at("12:00"))
        self.service.import_events(clear_event)  # 重复补录
        second = self.service.query_capacity("ST", at("10:00"), at("12:00"))
        self.assertEqual(first, second)


class RuleUpgradeTests(ServiceTestCase):
    """规则升级只影响生效时点之后的切片，历史切片保留原规则版本。"""

    def setUp(self) -> None:
        super().setUp()
        self.seed_devices()
        self.service.import_events(
            {
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
                        "type": "alarm_cleared",
                        "gun_id": "G1",
                        "payload": {
                            "alarm_id": "A1",
                            "cleared_at": "2026-09-25T11:30:00Z",
                        },
                    },
                ]
            }
        )

    def test_rule_upgrade_replays_only_after_effective_from(self) -> None:
        self.assertEqual(self.query_slice(at("10:00"), at("12:00"))["effective_kw"], 72.0)

        result = self.service.publish_rules(
            {
                "factors": {"temperature_control": 0.4},
                "effective_from": "2026-09-25T11:00:00Z",
                "description": "温控降额收紧到40%",
            }
        )
        self.assertEqual(result["version"], 2)

        # 11:00 前保持规则 v1 的 72；11:00 起按规则 v2 的 48
        early = self.query_slice(at("10:45"), at("12:00"))
        self.assertEqual(early["effective_kw"], 72.0)
        self.assertEqual(early["rule_version"], 1)
        late = self.query_slice(at("11:00"), at("12:00"))
        self.assertEqual(late["effective_kw"], 48.0)
        self.assertEqual(late["rule_version"], 2)
        # 告警 11:30 解除后恢复额定
        self.assertEqual(self.query_slice(at("11:30"), at("12:00"))["effective_kw"], 120.0)

    def test_rule_publish_rejects_invalid_factor(self) -> None:
        from service_09251_007.application.services import ServiceError

        with self.assertRaises(ServiceError):
            self.service.publish_rules(
                {
                    "factors": {"temperature_control": 1.5},
                    "effective_from": "2026-09-25T11:00:00Z",
                }
            )


class RecoveryTests(ServiceTestCase):
    """派生切片丢失后可从事件日志完整重建（SQLite 状态可恢复）。"""

    def test_rebuild_restores_derived_state(self) -> None:
        self.seed_devices()
        self.service.import_events(
            {
                "events": [
                    {
                        "event_id": "E1",
                        "type": "alarm_raised",
                        "gun_id": "G1",
                        "payload": {
                            "alarm_id": "A1",
                            "category": "communication",
                            "started_at": "2026-09-25T09:00:00Z",
                        },
                    },
                    {
                        "event_id": "E2",
                        "type": "alarm_cleared",
                        "gun_id": "G1",
                        "payload": {
                            "alarm_id": "A1",
                            "cleared_at": "2026-09-25T10:00:00Z",
                        },
                    },
                    {
                        "event_id": "E3",
                        "type": "maintenance_upsert",
                        "gun_id": "G2",
                        "payload": {
                            "window_id": "W1",
                            "start": "2026-09-25T11:00:00Z",
                            "end": "2026-09-25T11:30:00Z",
                            "reason": "例行检查",
                        },
                    },
                ]
            }
        )
        before = self.service.query_capacity("ST", at("08:00"), at("12:00"))

        # 模拟派生态丢失：直接清空物化切片表
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM capacity_slices")
            conn.commit()
        finally:
            conn.close()

        result = self.service.rebuild()
        self.assertTrue(result["rebuilt"])
        after = self.service.query_capacity("ST", at("08:00"), at("12:00"))
        self.assertEqual(before, after)

    def test_restart_recovers_state(self) -> None:
        """新服务实例打开同一数据库，历史事件与切片仍然可用。"""
        self.seed_devices()
        self.service.import_events(
            {
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
                    }
                ]
            }
        )
        restarted = self.make_service()
        result = restarted.query_capacity("ST", at("10:00"), at("10:15"))
        entry = result["slices"][0]["guns"][0]
        self.assertEqual(entry["effective_kw"], 72.0)


if __name__ == "__main__":
    unittest.main()
