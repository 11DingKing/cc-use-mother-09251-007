"""HTTP API 端到端测试：批量导入、实时查询、维修并发、快照签署与差异说明。"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from service_09251_007.api.server import create_server

from support import ServiceTestCase


class ApiTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server = create_server(self.service, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        headers = {"Content-Type": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow(self) -> None:
        # 健康检查
        status, body = self.call("GET", "/api/v1/health")
        self.assertEqual((status, body["status"]), (200, "ok"))

        # 设备登记
        status, body = self.call(
            "POST",
            "/api/v1/devices",
            {
                "circuits": [{"circuit_id": "C1", "station_id": "ST", "limit_kw": 200}],
                "guns": [
                    {"gun_id": "G1", "station_id": "ST", "circuit_id": "C1", "rated_kw": 120},
                    {"gun_id": "G2", "station_id": "ST", "circuit_id": "C1", "rated_kw": 120},
                ],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["guns"], 2)

        # 批量导入：温控告警 + 功率采样（事件幂等）
        batch = {
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
                    "type": "power_samples",
                    "gun_id": "G1",
                    "payload": {"samples": [{"ts": "2026-09-25T10:03:00Z", "kw": 50.0}]},
                },
            ],
        }
        status, body = self.call("POST", "/api/v1/events/batch", batch)
        self.assertEqual(status, 200)
        self.assertEqual(body["inserted"], 2)
        status, retry = self.call("POST", "/api/v1/events/batch", batch)
        self.assertEqual(status, 200)
        self.assertEqual(retry["inserted"], 0)
        self.assertEqual(sorted(retry["duplicates"]), ["E1", "E2"])

        # 实时查询：G1 被实测收紧到 50；回路 200 限额下 G1+G2=170 不触发分摊
        status, body = self.call(
            "GET",
            "/api/v1/capacity?station_id=ST&start=2026-09-25T10:00:00Z&end=2026-09-25T10:15:00Z",
        )
        self.assertEqual(status, 200)
        slice_entry = body["slices"][0]
        self.assertEqual(slice_entry["station_allocated_kw"], 170.0)
        guns = {g["gun_id"]: g for g in slice_entry["guns"]}
        self.assertEqual(guns["G1"]["effective_kw"], 50.0)
        self.assertEqual(guns["G2"]["effective_kw"], 120.0)
        factor_kinds = {f["kind"] for f in guns["G1"]["factors"]}
        self.assertEqual(factor_kinds, {"alarm", "observed_power"})

        # 维修窗口：创建 → 版本冲突 → 按最新版本更新
        status, window = self.call(
            "POST",
            "/api/v1/maintenance",
            {
                "window_id": "W1",
                "gun_id": "G2",
                "start": "2026-09-25T23:00:00Z",
                "end": "2026-09-26T01:00:00Z",
                "reason": "跨午夜检修",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(window["version"], 1)

        status, conflict = self.call(
            "PUT",
            "/api/v1/maintenance/W1",
            {
                "expected_version": 9,
                "start": "2026-09-25T23:00:00Z",
                "end": "2026-09-26T02:00:00Z",
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["details"]["current_version"], 1)

        status, updated = self.call(
            "PUT",
            "/api/v1/maintenance/W1",
            {
                "expected_version": 1,
                "start": "2026-09-25T23:00:00Z",
                "end": "2026-09-26T02:00:00Z",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["version"], 2)

        # 跨午夜维修在查询中生效（00:30 切片 G2 置零；G1 告警未解除仍降额）
        status, body = self.call(
            "GET",
            "/api/v1/capacity?station_id=ST&start=2026-09-26T00:30:00Z&end=2026-09-26T00:45:00Z",
        )
        self.assertEqual(status, 200)
        guns = {g["gun_id"]: g for g in body["slices"][0]["guns"]}
        self.assertEqual(guns["G2"]["effective_kw"], 0.0)
        self.assertEqual(guns["G1"]["effective_kw"], 72.0)

        # 快照签署与验签
        status, snap1 = self.call(
            "POST",
            "/api/v1/snapshots",
            {"station_id": "ST", "start": "2026-09-25T10:00:00Z", "end": "2026-09-25T12:00:00Z"},
        )
        self.assertEqual(status, 201)
        self.assertTrue(snap1["signature"])
        status, verify = self.call("POST", f"/api/v1/snapshots/{snap1['snapshot_id']}/verify")
        self.assertEqual((status, verify["valid"]), (200, True))

        # 补录解除 → 重放 → 新快照 → 差异说明
        status, _ = self.call(
            "POST",
            "/api/v1/events/batch",
            {
                "events": [
                    {
                        "event_id": "E3",
                        "type": "alarm_cleared",
                        "gun_id": "G1",
                        "payload": {"alarm_id": "A1", "cleared_at": "2026-09-25T10:30:00Z"},
                    }
                ]
            },
        )
        self.assertEqual(status, 200)
        status, snap2 = self.call(
            "POST",
            "/api/v1/snapshots",
            {"station_id": "ST", "start": "2026-09-25T10:00:00Z", "end": "2026-09-25T12:00:00Z"},
        )
        self.assertEqual(status, 201)
        status, diff = self.call(
            "GET",
            f"/api/v1/snapshots/diff?from_id={snap1['snapshot_id']}&to_id={snap2['snapshot_id']}",
        )
        self.assertEqual(status, 200)
        self.assertTrue(diff["changed_slices"])
        self.assertIn("E3", [e["event_id"] for e in diff["causal_events"]])

        # 规则升级 → 重放生效
        status, rules = self.call(
            "POST",
            "/api/v1/rules",
            {
                "factors": {"temperature_control": 0.4},
                "effective_from": "2026-09-25T11:00:00Z",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(rules["version"], 2)

        # 恢复重建
        status, rebuilt = self.call("POST", "/api/v1/admin/rebuild")
        self.assertEqual(status, 200)
        self.assertTrue(rebuilt["rebuilt"])

    def test_error_shapes(self) -> None:
        status, body = self.call("GET", "/api/v1/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

        status, body = self.call("POST", "/api/v1/events/batch", {"events": []})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "validation_failed")

        status, body = self.call(
            "GET", "/api/v1/capacity?station_id=ST&start=bad&end=2026-09-25T11:00:00Z"
        )
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
