"""容量核算服务的集成测试。

覆盖需求点名场景：重叠降级、跨午夜维修、回路级限额；
以及事件幂等、并发维修更新、SQLite 可恢复、快照不可变、规则升级重放与 HTTP API。
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime
from pathlib import Path

from service_09251_007.application.service import CapacityService
from service_09251_007.bootstrap import build_service
from service_09251_007.errors import ConflictError
from service_09251_007.interfaces.http_api import build_server
from service_09251_007.ports import FixedClock

NOW = datetime(2026, 9, 25, 12, 0)


class CapacityCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "capacity.db")
        self.clock = FixedClock(NOW)
        self.svc = build_service(self.db_path, clock=self.clock)
        # C1 限额 150kW 带两把 100kW 枪；C2 限额 60kW 带一把 80kW 枪
        self.svc.upsert_topology(
            circuits=[
                {"circuit_id": "C1", "limit_kw": 150, "name": "一号回路"},
                {"circuit_id": "C2", "limit_kw": 60, "name": "二号回路"},
            ],
            guns=[
                {"gun_id": "G1", "circuit_id": "C1", "rated_kw": 100},
                {"gun_id": "G2", "circuit_id": "C1", "rated_kw": 100},
                {"gun_id": "G3", "circuit_id": "C2", "rated_kw": 80},
            ],
        )

    def tearDown(self) -> None:
        self.svc.repo.conn.close()
        self.tmp.cleanup()

    # -- 工具 -------------------------------------------------------------
    def _find(self, start: str, scope: str, target_id: str):
        end = datetime.fromisoformat(start)
        from datetime import timedelta
        data = self.svc.query_capacity(start, end + timedelta(minutes=15))
        for row in data["slices"]:
            if row["scope"] == scope and row["target_id"] == target_id \
                    and row["slice_start"] == start:
                return row
        return None

    def gun_slice(self, start: str, gun_id: str):
        row = self._find(start, "gun", gun_id)
        if row is None:
            raise AssertionError("未找到枪片: %s %s" % (gun_id, start))
        return row

    def circuit_slice(self, start: str, cid: str):
        row = self._find(start, "circuit", cid)
        if row is None:
            raise AssertionError("未找到回路片: %s %s" % (cid, start))
        return row

    # -- 1. 重叠降级 ------------------------------------------------------
    def test_overlapping_derating_uses_strictest_factor(self) -> None:
        # A1: 0.5 因子 10:00-11:00；A2: 0.25 因子 10:30-11:30
        self.svc.record_alarm("A1", "G1", 0.5,
                              "2026-09-25T10:00", "2026-09-25T11:00", seq=1)
        self.svc.record_alarm("A2", "G1", 0.25,
                              "2026-09-25T10:30", "2026-09-25T11:30", seq=1)

        only_a1 = self.gun_slice("2026-09-25T10:15:00", "G1")
        overlap = self.gun_slice("2026-09-25T10:30:00", "G1")
        only_a2 = self.gun_slice("2026-09-25T11:00:00", "G1")
        self.assertEqual(only_a1["effective_kw"], 50.0)
        self.assertEqual(overlap["effective_kw"], 25.0)  # min(0.5,0.25)
        self.assertEqual(only_a2["effective_kw"], 25.0)
        refs = {f["ref"] for s in overlap["detail"]["segments"] for f in s["factors"]}
        self.assertEqual(refs, {"A1", "A2"})  # 可追溯：两条来源都在

    def test_partial_slice_is_time_weighted(self) -> None:
        # 10:07 开始降为 0.5，片 10:00-10:15 内覆盖 8 分钟
        self.svc.record_alarm("A3", "G1", 0.5,
                              "2026-09-25T10:07", "2026-09-25T11:00", seq=1)
        row = self.gun_slice("2026-09-25T10:00:00", "G1")
        self.assertAlmostEqual(row["detail"]["ratio"], 11 / 15, places=5)
        self.assertAlmostEqual(row["effective_kw"], 100 * 11 / 15, places=2)
        kinds = {(s["from_s"], s["to_s"]) for s in row["detail"]["segments"]}
        self.assertIn((0.0, 420.0), kinds)
        self.assertIn((420.0, 900.0), kinds)

    # -- 2. 跨午夜维修 ----------------------------------------------------
    def test_maintenance_crosses_midnight(self) -> None:
        self.svc.create_maintenance(
            "M-NIGHT", "gun", "G1",
            planned_start="2026-09-25T23:30",
            planned_end="2026-09-26T00:30",
            factor=0.0,
        )
        for t in ("2026-09-25T23:30:00", "2026-09-25T23:45:00",
                  "2026-09-26T00:00:00", "2026-09-26T00:15:00"):
            self.assertEqual(self.gun_slice(t, "G1")["effective_kw"], 0.0, t)
        # 窗口外的相邻时间片不受影响
        self.assertEqual(
            self.gun_slice("2026-09-25T23:15:00", "G1")["effective_kw"], 100.0)
        self.assertEqual(
            self.gun_slice("2026-09-26T00:30:00", "G1")["effective_kw"], 100.0)

    def test_circuit_scope_maintenance_covers_all_member_guns(self) -> None:
        self.svc.create_maintenance(
            "M-C", "circuit", "C1",
            planned_start="2026-09-25T08:00",
            planned_end="2026-09-25T09:00",
            factor=0.0,
        )
        self.assertEqual(self.gun_slice("2026-09-25T08:15:00", "G1")["effective_kw"], 0.0)
        self.assertEqual(self.gun_slice("2026-09-25T08:15:00", "G2")["effective_kw"], 0.0)
        # C1 回路维修：回路容量 0；其他回路不受影响，G3 仍被 C2 限额压到 60
        self.assertEqual(self.circuit_slice("2026-09-25T08:15:00", "C1")["effective_kw"], 0.0)
        self.assertEqual(self.circuit_slice("2026-09-25T08:15:00", "C2")["effective_kw"], 60.0)

    # -- 3. 回路级限额 ----------------------------------------------------
    def test_circuit_limit_caps_member_sum(self) -> None:
        # G1+G2 名义 200kW，回路共同上限 150kW
        row = self.circuit_slice("2026-09-25T09:00:00", "C1")
        self.assertEqual(row["effective_kw"], 150.0)
        self.assertTrue(row["detail"]["binding"])
        self.assertEqual(row["detail"]["sum_kw"], 200.0)

        # G3 名义 80kW，C2 上限 60kW
        c2 = self.circuit_slice("2026-09-25T09:00:00", "C2")
        self.assertEqual(c2["effective_kw"], 60.0)
        self.assertTrue(c2["detail"]["binding"])

    def test_circuit_cap_relaxes_when_guns_derated(self) -> None:
        # G1 降到 40kW，与 G2 合计 140kW，不再触及 150kW 上限
        self.svc.record_alarm("A4", "G1", 0.4,
                              "2026-09-25T09:00", "2026-09-25T10:00", seq=1)
        row = self.circuit_slice("2026-09-25T09:15:00", "C1")
        self.assertEqual(row["effective_kw"], 140.0)
        self.assertFalse(row["detail"]["binding"])

    # -- 4. 事件幂等 ------------------------------------------------------
    def test_alarm_reports_are_idempotent(self) -> None:
        r1 = self.svc.record_alarm("A5", "G1", 0.5,
                                   "2026-09-25T09:00", None, seq=1)
        r2 = self.svc.record_alarm("A5", "G1", 0.5,
                                   "2026-09-25T09:00", None, seq=1)
        r3 = self.svc.record_alarm("A5", "G1", 0.5,
                                   "2026-09-25T09:00", None, seq=0)  # 迟到旧消息
        self.assertEqual(r1["status"], "applied")
        self.assertEqual(r2["status"], "ignored")
        self.assertEqual(r3["status"], "ignored")
        # 解除消息 seq 更大才生效
        r4 = self.svc.record_alarm("A5", "G1", 0.5,
                                   "2026-09-25T09:00",
                                   "2026-09-25T09:30", seq=2)
        self.assertEqual(r4["status"], "applied")
        self.assertEqual(
            self.gun_slice("2026-09-25T09:45:00", "G1")["effective_kw"], 100.0)

    def test_batch_import_is_idempotent(self) -> None:
        payload = {
            "batch_id": "B-1",
            "alarms": [
                {"event_ref": "AB", "gun_id": "G1", "factor": 0.3,
                 "started_at": "2026-09-25T09:00",
                 "ended_at": "2026-09-25T09:30", "seq": 1}
            ],
        }
        first = self.svc.batch_import(payload)
        second = self.svc.batch_import(payload)
        self.assertEqual(first["status"], "applied")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(
            self.gun_slice("2026-09-25T09:15:00", "G1")["effective_kw"], 30.0)

    # -- 5. 并发维修更新不丢失 --------------------------------------------
    def test_concurrent_maintenance_events_both_survive(self) -> None:
        self.svc.create_maintenance(
            "M-CONC", "gun", "G1",
            planned_start="2026-09-25T14:00",
            planned_end="2026-09-25T15:00",
            factor=0.0,
        )
        barrier = threading.Barrier(2)
        results: list[dict] = []

        def worker(op_seq: int, changes: dict) -> None:
            svc = build_service(self.db_path, clock=FixedClock(NOW))
            try:
                barrier.wait()
                results.append(svc.update_maintenance("M-CONC", op_seq, **changes))
            finally:
                svc.repo.conn.close()

        t1 = threading.Thread(target=worker, args=(1, {
            "status": "in_progress",
            "started_at": "2026-09-25T14:05",
        }))
        t2 = threading.Thread(target=worker, args=(2, {
            "status": "done",
            "ended_at": "2026-09-25T14:50",
        }))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertTrue(all(r["applied"] for r in results), results)

        window = self.svc.repo.get_maintenance("M-CONC")
        self.assertEqual(window["status"], "done")
        self.assertEqual(window["started_at"], datetime(2026, 9, 25, 14, 5))
        self.assertEqual(window["ended_at"], datetime(2026, 9, 25, 14, 50))

        # 重复投递同一 op_seq 幂等，且内容不同则冲突
        dup = self.svc.update_maintenance(
            "M-CONC", 1, status="in_progress",
            started_at="2026-09-25T14:05")
        self.assertFalse(dup["applied"])
        with self.assertRaises(ConflictError):
            self.svc.update_maintenance(
                "M-CONC", 1, status="in_progress",
                started_at="2026-09-25T14:06")

    def test_optimistic_version_conflict(self) -> None:
        self.svc.create_maintenance(
            "M-OPT", "gun", "G1",
            planned_start="2026-09-25T14:00",
            planned_end="2026-09-25T15:00",
        )
        self.svc.update_maintenance(
            "M-OPT", 1, status="in_progress",
            started_at="2026-09-25T14:00")
        with self.assertRaises(ConflictError):
            self.svc.update_maintenance(
                "M-OPT", 2, status="done",
                ended_at="2026-09-25T14:40", expected_version=1)

    # -- 6. SQLite 状态可恢复 ---------------------------------------------
    def test_state_recovers_after_reopen(self) -> None:
        self.svc.record_alarm("A6", "G1", 0.5,
                              "2026-09-25T09:00", "2026-09-25T10:00", seq=1)
        self.svc.repo.conn.close()

        reopened = build_service(self.db_path, clock=self.clock)
        try:
            row = None
            for r in reopened.query_capacity(
                    "2026-09-25T09:00", "2026-09-25T09:30")["slices"]:
                if r["scope"] == "gun" and r["target_id"] == "G1":
                    row = r
            self.assertIsNotNone(row)
            self.assertEqual(row["effective_kw"], 50.0)
            replays = reopened.replays()
            self.assertTrue(any(r["reason"] == "alarm" for r in replays))
        finally:
            reopened.repo.conn.close()

    # -- 7. 快照不可变 + 差异说明 -----------------------------------------
    def test_signed_snapshot_is_immutable(self) -> None:
        window = ("2026-09-25T09:00", "2026-09-25T09:30")
        s1 = self.svc.sign_snapshot("S1", *window, signer="值班员甲")
        before = self.svc.get_snapshot("S1")
        self.assertEqual(before["hash_sha256"], s1["hash_sha256"])

        # 之后出现新告警并重放了同一区间
        self.svc.record_alarm("A7", "G1", 0.5,
                              "2026-09-25T09:00", "2026-09-25T09:30", seq=1)
        s2 = self.svc.sign_snapshot("S2", *window, signer="值班员甲")
        after = self.svc.get_snapshot("S1")
        self.assertEqual(after["content"], before["content"])  # 已发布内容不变
        self.assertNotEqual(s1["hash_sha256"], s2["hash_sha256"])

        # 物理触发器拦截 UPDATE/DELETE
        conn = self.svc.repo.conn
        with self.assertRaisesRegex(Exception, "immutable"):
            conn.execute("UPDATE snapshots SET signer='x' WHERE snapshot_id='S1'")
        with self.assertRaisesRegex(Exception, "immutable"):
            conn.execute("DELETE FROM snapshots WHERE snapshot_id='S1'")
        # 同号快照不可重复签署
        with self.assertRaises(ConflictError):
            self.svc.sign_snapshot("S1", *window)

    def test_diff_explains_changes_between_snapshots(self) -> None:
        window = ("2026-09-25T09:00", "2026-09-25T09:15")
        self.svc.sign_snapshot("D1", *window)
        self.svc.record_alarm("A8", "G1", 0.5,
                              "2026-09-25T09:00", "2026-09-25T09:15", seq=1)
        self.svc.sign_snapshot("D2", *window)
        diff = self.svc.diff_snapshots("D1", "D2")
        gun_change = next(c for c in diff["changes"]
                          if c["scope"] == "gun" and c["target_id"] == "G1"
                          and c["slice_start"] == "2026-09-25T09:00:00")
        self.assertEqual(gun_change["old_kw"], 100.0)
        self.assertEqual(gun_change["new_kw"], 50.0)
        self.assertEqual(gun_change["delta_kw"], -50.0)
        self.assertEqual(gun_change["kind"], "changed")
        self.assertGreater(diff["change_count"], 0)

    # -- 8. 曲线补录与规则升级重放 ----------------------------------------
    def test_backfilled_curve_caps_capacity(self) -> None:
        self.svc.import_curve_samples("G1", [
            {"ts": "2026-09-25T09:05", "kw": 40},
            {"ts": "2026-09-25T09:10", "kw": 38},
        ])
        row = self.gun_slice("2026-09-25T09:00:00", "G1")
        self.assertEqual(row["effective_kw"], 40.0)
        self.assertEqual(row["detail"]["curve"]["samples"], 2)

    def test_rule_upgrade_replays_affected_window(self) -> None:
        self.svc.record_alarm("R1", "G1", 0.5,
                              "2026-09-25T10:00", "2026-09-25T11:00", seq=1)
        self.svc.record_alarm("R2", "G1", 0.25,
                              "2026-09-25T10:30", "2026-09-25T11:30", seq=1)
        # v1（min）下重叠片为 25kW
        self.assertEqual(
            self.gun_slice("2026-09-25T10:30:00", "G1")["effective_kw"], 25.0)

        # 升级为连乘规则，生效时间 10:00；此前片仍用 v1
        self.svc.upgrade_rule(
            "derating-default", 2,
            {"compose": "multiply", "curve_min_samples": 2, "curve_stat": "max"},
            effective_from="2026-09-25T10:00",
            replay_to="2026-09-25T12:00",
        )
        upgraded = self.gun_slice("2026-09-25T10:30:00", "G1")
        self.assertEqual(upgraded["effective_kw"], 12.5)  # 100*0.5*0.25
        self.assertEqual(upgraded["rule_version"], 2)
        legacy = self.gun_slice("2026-09-25T09:45:00", "G1")
        self.assertEqual(legacy["rule_version"], 1)

    # -- 9. HTTP API ------------------------------------------------------
    def test_http_api_roundtrip(self) -> None:
        server = build_server(self.db_path, "127.0.0.1", 0, clock=self.clock)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def call(method: str, path: str, payload=None):
                data = json.dumps(payload).encode() if payload is not None else None
                req = urllib.request.Request(
                    "http://127.0.0.1:%d%s" % (port, path), data=data,
                    headers={"Content-Type": "application/json"}, method=method)
                with urllib.request.urlopen(req) as resp:
                    return json.loads(resp.read())

            call("POST", "/imports", {
                "batch_id": "B-HTTP",
                "alarms": [
                    {"event_ref": "AH", "gun_id": "G1", "factor": 0.5,
                     "started_at": "2026-09-25T09:00",
                     "ended_at": "2026-09-25T09:30", "seq": 1}
                ],
            })
            dup = call("POST", "/imports", {
                "batch_id": "B-HTTP",
                "alarms": [
                    {"event_ref": "AH", "gun_id": "G1", "factor": 0.5,
                     "started_at": "2026-09-25T09:00",
                     "ended_at": "2026-09-25T09:30", "seq": 1}
                ],
            })
            self.assertEqual(dup["status"], "duplicate")

            signed = call("POST", "/snapshots", {
                "snapshot_id": "SH", "start": "2026-09-25T09:00",
                "end": "2026-09-25T09:15", "signer": "api"})
            self.assertIn("hash_sha256", signed)

            data = call("GET", "/capacity?start=2026-09-25T09:00&end=2026-09-25T09:15")
            guns = [r for r in data["slices"]
                    if r["scope"] == "gun" and r["target_id"] == "G1"]
            self.assertEqual(guns[0]["effective_kw"], 50.0)

            fetched = call("GET", "/snapshots/SH")
            self.assertEqual(fetched["hash_sha256"], signed["hash_sha256"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
