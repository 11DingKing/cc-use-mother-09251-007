"""维修窗口并发更新测试：乐观并发保证不丢更新。"""
from __future__ import annotations

import sqlite3
import threading
import unittest

from service_09251_007.application.services import ServiceError

from support import ServiceTestCase, at


class MaintenanceConcurrencyTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed_devices()
        created = self.service.create_maintenance(
            {
                "window_id": "W1",
                "gun_id": "G1",
                "start": "2026-09-25T22:00:00Z",
                "end": "2026-09-26T02:00:00Z",
                "reason": "夜间检修",
            }
        )
        self.assertEqual(created["version"], 1)

    def _window_version(self) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT version FROM maintenance_windows WHERE window_id='W1'"
            ).fetchone()
            return row[0]
        finally:
            conn.close()

    def _update(self, expected_version: int, end: str) -> dict:
        return self.service.update_maintenance(
            "W1",
            {
                "expected_version": expected_version,
                "start": "2026-09-25T22:00:00Z",
                "end": end,
                "reason": "夜间检修",
            },
        )

    def test_conflicting_updates_exactly_one_wins(self) -> None:
        results: list[str] = []
        lock = threading.Lock()

        def worker(end: str) -> None:
            try:
                self._update(1, end)
                outcome = "ok"
            except ServiceError as exc:
                outcome = exc.code
            with lock:
                results.append(outcome)

        threads = [
            threading.Thread(target=worker, args=(f"2026-09-26T0{h}:00:00Z",))
            for h in range(2, 8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("conflict"), len(threads) - 1)
        self.assertEqual(self._window_version(), 2)

    def test_no_lost_updates_under_contention(self) -> None:
        """8 线程 × 5 次成功更新：每次成功必须恰好推进一个版本。"""
        successes: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            completed = 0
            expected = 1
            while completed < 5:
                try:
                    result = self._update(expected, "2026-09-26T02:00:00Z")
                    with lock:
                        successes.append(result["version"])
                    completed += 1
                    expected = result["version"]
                except ServiceError as exc:
                    if exc.code != "conflict":
                        raise
                    # 冲突响应携带最新版本号，据此重试
                    expected = exc.details["current_version"]

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(successes), 40)
        # 版本号连续推进 2..41，没有任何更新被覆盖丢失
        self.assertEqual(sorted(successes), list(range(2, 42)))
        self.assertEqual(self._window_version(), 41)

    def test_update_reflects_in_capacity_after_replay(self) -> None:
        self._update(1, "2026-09-25T23:00:00Z")
        # 窗口缩短到 23:00 结束：23:00 切片恢复额定
        entry = self.query_slice(at("23:00"), at("23:15"))
        self.assertEqual(entry["effective_kw"], 120.0)
        entry = self.query_slice(at("22:00"), at("22:15"))
        self.assertEqual(entry["effective_kw"], 0.0)


if __name__ == "__main__":
    unittest.main()
