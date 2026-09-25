"""快照测试：签名验签、发布不可变、重放产生新版本、差异说明可追溯。"""
from __future__ import annotations

import sqlite3
import unittest

from support import ServiceTestCase, at


class SnapshotTests(ServiceTestCase):
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
        self.range = {"station_id": "ST", "start": "2026-09-25T10:00:00Z", "end": "2026-09-25T12:00:00Z"}
        self.snap1 = self.service.create_snapshot(self.range)

    def test_snapshot_signature_verifies(self) -> None:
        result = self.service.verify_snapshot(self.snap1["snapshot_id"])
        self.assertTrue(result["valid"])

    def test_snapshot_is_immutable_in_storage(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE snapshots SET signature='tampered' WHERE snapshot_id=?",
                    (self.snap1["snapshot_id"],),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM snapshots WHERE snapshot_id=?",
                    (self.snap1["snapshot_id"],),
                )
        finally:
            conn.close()

    def test_published_snapshot_survives_later_replay(self) -> None:
        before = self.service.get_snapshot(self.snap1["snapshot_id"])
        # 补录解除 + 规则升级，触发受影响区间重放
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
        self.service.publish_rules(
            {
                "factors": {"temperature_control": 0.4},
                "effective_from": "2026-09-25T11:00:00Z",
            }
        )
        after = self.service.get_snapshot(self.snap1["snapshot_id"])
        # 已发布快照字节级不变，验签仍通过
        self.assertEqual(before, after)
        self.assertTrue(self.service.verify_snapshot(self.snap1["snapshot_id"])["valid"])

    def test_diff_explains_changes_with_causal_events(self) -> None:
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
        snap2 = self.service.create_snapshot(self.range)
        self.assertEqual(snap2["snapshot_seq"], self.snap1["snapshot_seq"] + 1)

        diff = self.service.diff_snapshots(self.snap1["snapshot_id"], snap2["snapshot_id"])
        changed = diff["changed_slices"]
        self.assertTrue(changed)
        # 10:30 起的切片从 72 恢复为 120
        restored = [c for c in changed if c["slice_start"] == "2026-09-25T10:30:00Z"]
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["old"]["effective_kw"], 72.0)
        self.assertEqual(restored[0]["new"]["effective_kw"], 120.0)
        self.assertTrue(restored[0]["reasons"])
        # 因果事件指向补录的解除事件
        causal_ids = [e["event_id"] for e in diff["causal_events"]]
        self.assertIn("E2", causal_ids)

    def test_diff_reports_rule_change(self) -> None:
        self.service.publish_rules(
            {
                "factors": {"temperature_control": 0.4},
                "effective_from": "2026-09-25T11:00:00Z",
            }
        )
        snap2 = self.service.create_snapshot(self.range)
        diff = self.service.diff_snapshots(self.snap1["snapshot_id"], snap2["snapshot_id"])
        self.assertTrue(diff["rule_changes"])
        changed = [c for c in diff["changed_slices"] if c["slice_start"] == "2026-09-25T11:00:00Z"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["old"]["effective_kw"], 72.0)
        self.assertEqual(changed[0]["new"]["effective_kw"], 48.0)
        self.assertTrue(any("规则版本" in r for r in changed[0]["reasons"]))

    def test_diff_rejects_mismatched_ranges(self) -> None:
        other = self.service.create_snapshot(
            {"station_id": "ST", "start": "2026-09-25T08:00:00Z", "end": "2026-09-25T12:00:00Z"}
        )
        from service_09251_007.application.services import ServiceError

        with self.assertRaises(ServiceError) as ctx:
            self.service.diff_snapshots(self.snap1["snapshot_id"], other["snapshot_id"])
        self.assertEqual(ctx.exception.status, 422)

    def test_signing_key_persists_across_restart(self) -> None:
        """密钥由首实例生成并落库，重启后的新实例验签历史快照仍通过。"""
        from service_09251_007.application.services import CapacityService
        from service_09251_007.ports.clock import FixedClock
        from service_09251_007.ports.ids import SequentialIds
        from support import NOW

        first = CapacityService(self.db_path, clock=FixedClock(NOW), ids=SequentialIds())
        snap = first.create_snapshot(self.range)
        self.assertTrue(first.verify_snapshot(snap["snapshot_id"])["valid"])

        # 模拟重启：全新实例，不注入签名器，密钥应来自数据库 meta 表
        restarted = CapacityService(self.db_path, clock=FixedClock(NOW), ids=SequentialIds())
        self.assertTrue(restarted.verify_snapshot(snap["snapshot_id"])["valid"])


if __name__ == "__main__":
    unittest.main()
