"""应用服务层：批量导入、实时查询、维修并发控制、规则升级、快照与重放。

事务约定：所有写路径在 BEGIN IMMEDIATE 中完成「事件落库 + 受影响区间
重放」，SQLite 单写者语义保证并发下不丢更新；派生切片只是事件日志
的物化缓存，任何时刻都可由 rebuild 重建。
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3

from ..domain import capacity as engine
from ..domain.models import (
    CATEGORY_UNKNOWN,
    KNOWN_CATEGORIES,
    Circuit,
    Gun,
    RuleSet,
)
from ..domain.timeutil import (
    SLICE_SECONDS,
    align_down,
    align_up,
    format_iso8601,
    parse_iso8601,
    slice_starts,
)
from ..persistence import db, repositories as repo
from ..ports.clock import Clock
from ..ports.ids import Ids
from ..ports.signing import HmacSigner, canonical_json

MAX_QUERY_SECONDS = 31 * 24 * 3600  # 单次查询/快照区间上限：31 天


class ServiceError(Exception):
    """携带 HTTP 语义的应用异常。"""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        body = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            body["error"]["details"] = self.details
        return body


def _bad_request(message: str) -> ServiceError:
    return ServiceError(400, "bad_request", message)


def _validation(message: str, details: dict | None = None) -> ServiceError:
    return ServiceError(422, "validation_failed", message, details)


def _not_found(message: str) -> ServiceError:
    return ServiceError(404, "not_found", message)


def _conflict(message: str, details: dict | None = None) -> ServiceError:
    return ServiceError(409, "conflict", message, details)


def _require(payload: dict, field: str):
    value = payload.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise _validation(f"缺少必填字段: {field}")
    return value


def _parse_time(payload: dict, field: str) -> int:
    raw = _require(payload, field)
    try:
        return parse_iso8601(raw)
    except ValueError as exc:
        raise _validation(str(exc)) from exc


def _parse_range(payload: dict) -> tuple[int, int]:
    start = _parse_time(payload, "start")
    end = _parse_time(payload, "end")
    if end <= start:
        raise _validation("end 必须晚于 start")
    if end - start > MAX_QUERY_SECONDS:
        raise _validation("区间长度超过 31 天上限")
    return start, end


class CapacityService:
    """服务门面：每个方法独立开连接，天然支持多线程 HTTP 处理。"""

    def __init__(
        self,
        db_path: str,
        clock: Clock | None = None,
        ids: Ids | None = None,
        signer: HmacSigner | None = None,
    ):
        self._db_path = db_path
        self.clock = clock or Clock()
        self.ids = ids or Ids()
        conn = db.connect(db_path)
        try:
            if not db.integrity_check(conn):
                raise ServiceError(500, "storage_corrupt", "SQLite 完整性检查失败")
            db.initialize(conn, self.clock.now())
            if signer is not None:
                self._signer = signer
            else:
                self._signer = HmacSigner(self._load_signing_key(conn))
        finally:
            conn.close()

    # ------------------------------------------------------------ 基础设施

    def _connect(self) -> sqlite3.Connection:
        conn = db.connect(self._db_path)
        conn.isolation_level = None  # 手动管理事务边界
        return conn

    @staticmethod
    def _load_signing_key(conn: sqlite3.Connection) -> bytes:
        env_key = os.environ.get("CAPACITY_SIGNING_KEY")
        if env_key:
            return bytes.fromhex(env_key)
        stored = repo.get_meta(conn, "signing_key_hex")
        if stored is None:
            stored = secrets.token_hex(32)
            repo.set_meta(conn, "signing_key_hex", stored)
            conn.commit()
        return bytes.fromhex(stored)

    # ------------------------------------------------------------ 设备档案

    def import_devices(self, payload: dict) -> dict:
        """登记/更新配电回路与充电枪（按自然键幂等 upsert）。"""
        circuits_payload = payload.get("circuits") or []
        guns_payload = payload.get("guns") or []
        if not circuits_payload and not guns_payload:
            raise _validation("circuits 与 guns 不能同时为空")

        circuits: list[Circuit] = []
        for item in circuits_payload:
            limit = item.get("limit_kw")
            if not isinstance(limit, (int, float)) or limit <= 0:
                raise _validation(f"回路 {item.get('circuit_id')!r} 的 limit_kw 必须为正数")
            circuits.append(
                Circuit(
                    circuit_id=str(_require(item, "circuit_id")),
                    station_id=str(_require(item, "station_id")),
                    limit_kw=float(limit),
                )
            )
        guns: list[Gun] = []
        for item in guns_payload:
            rated = item.get("rated_kw")
            if not isinstance(rated, (int, float)) or rated <= 0:
                raise _validation(f"充电枪 {item.get('gun_id')!r} 的 rated_kw 必须为正数")
            guns.append(
                Gun(
                    gun_id=str(_require(item, "gun_id")),
                    station_id=str(_require(item, "station_id")),
                    circuit_id=item.get("circuit_id"),
                    rated_kw=float(rated),
                )
            )

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            circuit_ids = {c.circuit_id for c in circuits}
            circuit_ids.update(
                r["circuit_id"]
                for r in conn.execute("SELECT circuit_id FROM circuits").fetchall()
            )
            for gun in guns:
                if gun.circuit_id is not None and gun.circuit_id not in circuit_ids:
                    raise _validation(f"充电枪 {gun.gun_id} 挂接的回路不存在: {gun.circuit_id}")
            for circuit in circuits:
                repo.upsert_circuit(conn, circuit)
            for gun in guns:
                repo.upsert_gun(conn, gun)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {
            "circuits": len(circuits),
            "guns": len(guns),
        }

    # ------------------------------------------------------------ 事件导入

    def _validate_event(
        self,
        conn: sqlite3.Connection,
        event: dict,
        seen_raises: set[str],
        seen_windows: dict[str, tuple[str, int]],
    ) -> dict:
        """结构性校验单个事件，返回规范化后的副本。整批任一失败则全批拒绝。"""
        event_id = str(_require(event, "event_id"))
        type_ = str(_require(event, "type"))
        if type_ not in repo.EVENT_TYPES:
            raise _validation(f"未知事件类型: {type_}")
        gun_id = event.get("gun_id")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise _validation(f"事件 {event_id} 缺少 payload 对象")

        occurred_at: int
        station_id: str | None = None
        if type_ == repo.EVENT_ALARM_RAISED:
            if not gun_id:
                raise _validation(f"事件 {event_id} 缺少 gun_id")
            category = str(_require(payload, "category"))
            if category not in KNOWN_CATEGORIES:
                raise _validation(
                    f"事件 {event_id} 的告警类别未知: {category}",
                    {"known_categories": sorted(KNOWN_CATEGORIES)},
                )
            alarm_id = str(_require(payload, "alarm_id"))
            occurred_at = _parse_time(payload, "started_at")
            payload = {
                "alarm_id": alarm_id,
                "category": category,
                "started_at": occurred_at,
            }
            seen_raises.add(alarm_id)
        elif type_ == repo.EVENT_ALARM_CLEARED:
            if not gun_id:
                raise _validation(f"事件 {event_id} 缺少 gun_id")
            alarm_id = str(_require(payload, "alarm_id"))
            occurred_at = _parse_time(payload, "cleared_at")
            raise_payload = repo.find_alarm_raise(conn, alarm_id)
            if raise_payload is None and alarm_id not in seen_raises:
                raise _validation(f"事件 {event_id} 解除的告警不存在: {alarm_id}")
            if raise_payload is not None and occurred_at < raise_payload["started_at"]:
                raise _validation(f"事件 {event_id} 的解除时间早于告警发起时间")
            payload = {"alarm_id": alarm_id, "cleared_at": occurred_at}
        elif type_ == repo.EVENT_POWER_SAMPLES:
            if not gun_id:
                raise _validation(f"事件 {event_id} 缺少 gun_id")
            samples = payload.get("samples")
            if not isinstance(samples, list) or not samples:
                raise _validation(f"事件 {event_id} 的 samples 必须是非空数组")
            normalized = []
            for sample in samples:
                ts = _parse_time(sample, "ts")
                kw = sample.get("kw")
                if not isinstance(kw, (int, float)) or kw < 0:
                    raise _validation(f"事件 {event_id} 存在非法功率采样: {kw!r}")
                normalized.append({"ts": ts, "kw": float(kw)})
            occurred_at = min(s["ts"] for s in normalized)
            payload = {"samples": normalized}
        elif type_ == repo.EVENT_MAINTENANCE_UPSERT:
            if not gun_id:
                raise _validation(f"事件 {event_id} 缺少 gun_id")
            window_id = str(_require(payload, "window_id"))
            start = _parse_time(payload, "start")
            end = _parse_time(payload, "end")
            if end <= start:
                raise _validation(f"事件 {event_id} 的维修窗口 end 必须晚于 start")
            occurred_at = start
            payload = {
                "window_id": window_id,
                "start": start,
                "end": end,
                "reason": str(payload.get("reason", "")),
            }
            seen_windows[window_id] = (str(gun_id), start)
        else:  # maintenance_cancelled
            window_id = str(_require(payload, "window_id"))
            existing = repo.get_maintenance(conn, window_id)
            if existing is not None:
                gun_id = existing["gun_id"]
                occurred_at = existing["start_ts"]
            elif window_id in seen_windows:
                # 同批先登记后取消：引用批内窗口。
                gun_id, occurred_at = seen_windows[window_id]
            else:
                raise _validation(f"事件 {event_id} 取消的维修窗口不存在: {window_id}")
            payload = {"window_id": window_id}

        if gun_id:
            gun = repo.get_gun(conn, str(gun_id))
            if gun is None:
                raise _validation(f"事件 {event_id} 引用的充电枪不存在: {gun_id}")
            station_id = gun.station_id
        return {
            "event_id": event_id,
            "type": type_,
            "gun_id": str(gun_id) if gun_id else None,
            "station_id": station_id,
            "payload": payload,
            "occurred_at": occurred_at,
        }

    def import_events(self, payload: dict) -> dict:
        """批量导入事件：整批原子生效，event_id 去重保证幂等。

        同一批重试时所有事件判重，响应与首次一致（inserted=0），
        派生切片由重放保持收敛，不会产生重复影响。
        """
        events = payload.get("events")
        if not isinstance(events, list) or not events:
            raise _validation("events 必须是非空数组")
        batch_id = str(payload.get("batch_id") or self.ids.new_id("batch"))
        now = self.clock.now()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            seen_raises: set[str] = set()
            seen_windows: dict[str, tuple[str, int]] = {}
            normalized = [
                self._validate_event(conn, event, seen_raises, seen_windows)
                for event in events
            ]
            # 批次内 event_id 自身去重，避免同批重复导致计入两次。
            seen_ids: set[str] = set()
            for event in normalized:
                if event["event_id"] in seen_ids:
                    raise _validation(f"批次内 event_id 重复: {event['event_id']}")
                seen_ids.add(event["event_id"])

            inserted: list[str] = []
            duplicates: list[str] = []
            affected: dict[str, list[tuple[int, int]]] = {}
            for event in normalized:
                ok = repo.insert_event(
                    conn,
                    event_id=event["event_id"],
                    batch_id=batch_id,
                    type=event["type"],
                    station_id=event["station_id"],
                    gun_id=event["gun_id"],
                    payload=event["payload"],
                    occurred_at=event["occurred_at"],
                    recorded_at=now,
                )
                if not ok:
                    duplicates.append(event["event_id"])
                    continue
                inserted.append(event["event_id"])
                # 先取受影响区间（维修窗口移动时旧区间也要重放），再应用副作用。
                ranges = self._affected_ranges(conn, event, now)
                self._apply_side_effects(conn, event, now)
                for station_id, span_start, span_end in ranges:
                    affected.setdefault(station_id, []).append((span_start, span_end))

            repo.record_batch(conn, batch_id, now, len(normalized))
            replays = []
            for station_id, spans in affected.items():
                start = min(s for s, _ in spans)
                end = max(e for _, e in spans)
                replays.append(self._replay(conn, station_id, start, end, "event_import"))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {
            "batch_id": batch_id,
            "inserted": len(inserted),
            "inserted_event_ids": inserted,
            "duplicates": duplicates,
            "replayed": replays,
        }

    def _apply_side_effects(self, conn: sqlite3.Connection, event: dict, now: int) -> None:
        """把事件投影到维修窗口当前态（带版本号，供并发控制）。"""
        payload = event["payload"]
        if event["type"] == repo.EVENT_MAINTENANCE_UPSERT:
            existing = repo.get_maintenance(conn, payload["window_id"])
            repo.upsert_maintenance(
                conn,
                window_id=payload["window_id"],
                gun_id=event["gun_id"],
                station_id=event["station_id"],
                start_ts=payload["start"],
                end_ts=payload["end"],
                reason=payload["reason"],
                status="scheduled",
                version=(existing["version"] + 1) if existing else 1,
                updated_at=now,
            )
        elif event["type"] == repo.EVENT_MAINTENANCE_CANCELLED:
            existing = repo.get_maintenance(conn, payload["window_id"])
            repo.upsert_maintenance(
                conn,
                window_id=existing["window_id"],
                gun_id=existing["gun_id"],
                station_id=existing["station_id"],
                start_ts=existing["start_ts"],
                end_ts=existing["end_ts"],
                reason=existing["reason"],
                status="cancelled",
                version=existing["version"] + 1,
                updated_at=now,
            )

    def _affected_ranges(
        self, conn: sqlite3.Connection, event: dict, now: int
    ) -> list[tuple[str, int, int]]:
        """计算单个事件需要重放的区间（含补录的历史区间）。"""
        payload = event["payload"]
        station_id = event["station_id"]
        horizon = align_up(now) + SLICE_SECONDS
        if event["type"] == repo.EVENT_ALARM_RAISED:
            return [(station_id, payload["started_at"], horizon)]
        if event["type"] == repo.EVENT_ALARM_CLEARED:
            raised = repo.find_alarm_raise(conn, payload["alarm_id"])
            start = raised["started_at"] if raised else payload["cleared_at"]
            return [(station_id, start, max(payload["cleared_at"], horizon))]
        if event["type"] == repo.EVENT_POWER_SAMPLES:
            ts_list = [s["ts"] for s in payload["samples"]]
            return [(station_id, min(ts_list), max(ts_list) + SLICE_SECONDS)]
        if event["type"] == repo.EVENT_MAINTENANCE_UPSERT:
            existing = repo.get_maintenance(conn, payload["window_id"])
            spans = [(payload["start"], payload["end"])]
            if existing and (
                existing["start_ts"] != payload["start"]
                or existing["end_ts"] != payload["end"]
            ):
                # 窗口被移动过：旧区间同样需要重放。
                spans.append((existing["start_ts"], existing["end_ts"]))
            return [(station_id, s, e) for s, e in spans]
        if event["type"] == repo.EVENT_MAINTENANCE_CANCELLED:
            existing = repo.get_maintenance(conn, payload["window_id"])
            return [(station_id, existing["start_ts"], existing["end_ts"])]
        return []

    # ------------------------------------------------------------ 重放

    def _replay(
        self,
        conn: sqlite3.Connection,
        station_id: str,
        start: int,
        end: int,
        reason: str,
    ) -> dict:
        """在 [start, end) 内用当前事件日志与规则重算派生切片。

        必须在调用方的事务内执行；重放是确定性的，重复执行结果一致。
        """
        range_start = align_down(start)
        range_end = align_up(end)
        if range_end <= range_start:
            range_end = range_start + SLICE_SECONDS
        slices = engine.compute_station_slices(
            guns=repo.load_guns(conn, station_id),
            circuits=repo.load_circuits(conn, station_id),
            alarms=repo.load_alarm_intervals(conn, station_id),
            windows=repo.load_maintenance(conn, station_id),
            samples=repo.load_power_samples(conn, station_id),
            rules=repo.load_rules(conn),
            start=range_start,
            end=range_end,
        )
        repo.replace_slices(conn, station_id, range_start, range_end, slices)
        repo.record_replay(
            conn,
            station_id=station_id,
            range_start=range_start,
            range_end=range_end,
            reason=reason,
            detail=f"slices={len(slices)}",
            created_at=self.clock.now(),
        )
        return {
            "station_id": station_id,
            "range_start": format_iso8601(range_start),
            "range_end": format_iso8601(range_end),
            "slice_count": len(slices),
        }

    def rebuild(self) -> dict:
        """恢复路径：丢弃全部派生切片，从事件日志整体重建。"""
        now = self.clock.now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            repo.drop_all_slices(conn)
            stations = [
                r["station_id"]
                for r in conn.execute(
                    "SELECT DISTINCT station_id FROM guns ORDER BY station_id"
                ).fetchall()
            ]
            replays = []
            for station_id in stations:
                starts = [
                    e["occurred_at"]
                    for e in repo.list_events(conn, station_id=station_id)
                ]
                starts.extend(
                    w.start_ts for w in repo.load_maintenance(conn, station_id)
                )
                if not starts:
                    continue
                replays.append(
                    self._replay(conn, station_id, min(starts), align_up(now), "rebuild")
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"rebuilt": replays}

    # ------------------------------------------------------------ 实时查询

    def query_capacity(self, station_id: str, start: int, end: int) -> dict:
        """实时查询：优先读物化切片，缺口按需即时核算补齐。"""
        if end <= start:
            raise _validation("end 必须晚于 start")
        if end - start > MAX_QUERY_SECONDS:
            raise _validation("区间长度超过 31 天上限")
        range_start = align_down(start)
        range_end = align_up(end)
        conn = self._connect()
        try:
            guns = repo.load_guns(conn, station_id)
            if not guns:
                raise _not_found(f"场站不存在或没有充电枪: {station_id}")
            circuits = repo.load_circuits(conn, station_id)
            rows = repo.read_slices(conn, station_id, range_start, range_end)
            by_slice: dict[int, dict[str, dict]] = {}
            for row in rows:
                by_slice.setdefault(row["slice_start"], {})[row["gun_id"]] = {
                    "rule_version": row["rule_version"],
                    "effective_kw": row["effective_kw"],
                    "allocated_kw": row["allocated_kw"],
                    "factors": row["explanation"]["factors"],
                }
            # 对没有物化结果的片（未来区间或从未重放过的区间）即时核算。
            missing = [
                s
                for s in slice_starts(range_start, range_end)
                if s not in by_slice
            ]
            if missing:
                computed = engine.compute_station_slices(
                    guns=guns,
                    circuits=circuits,
                    alarms=repo.load_alarm_intervals(conn, station_id),
                    windows=repo.load_maintenance(conn, station_id),
                    samples=repo.load_power_samples(conn, station_id),
                    rules=repo.load_rules(conn),
                    start=min(missing),
                    end=max(missing) + SLICE_SECONDS,
                )
                for item in computed:
                    by_slice.setdefault(item.slice_start, {})[item.gun_id] = {
                        "rule_version": item.rule_version,
                        "effective_kw": item.effective_kw,
                        "allocated_kw": item.allocated_kw,
                        "factors": [f.to_dict() for f in item.factors],
                    }
        finally:
            conn.close()

        circuits_by_id = {c.circuit_id: c for c in circuits}
        slices_out = []
        for slice_start in slice_starts(range_start, range_end):
            gun_entries = []
            circuit_alloc: dict[str, float] = {}
            station_total = 0.0
            for gun in guns:
                entry = by_slice.get(slice_start, {}).get(gun.gun_id)
                if entry is None:
                    continue
                station_total += entry["allocated_kw"]
                if gun.circuit_id:
                    circuit_alloc[gun.circuit_id] = (
                        circuit_alloc.get(gun.circuit_id, 0.0) + entry["allocated_kw"]
                    )
                gun_entries.append(
                    {
                        "gun_id": gun.gun_id,
                        "rated_kw": gun.rated_kw,
                        "effective_kw": entry["effective_kw"],
                        "allocated_kw": entry["allocated_kw"],
                        "rule_version": entry["rule_version"],
                        "factors": entry["factors"],
                    }
                )
            slices_out.append(
                {
                    "slice_start": format_iso8601(slice_start),
                    "station_allocated_kw": round(station_total + 1e-9, 3),
                    "circuits": [
                        {
                            "circuit_id": cid,
                            "limit_kw": circuits_by_id[cid].limit_kw,
                            "allocated_kw": round(value + 1e-9, 3),
                        }
                        for cid, value in sorted(circuit_alloc.items())
                    ],
                    "guns": gun_entries,
                }
            )
        return {
            "station_id": station_id,
            "slice_seconds": SLICE_SECONDS,
            "slices": slices_out,
        }

    # ------------------------------------------------------------ 维修窗口

    def create_maintenance(self, payload: dict) -> dict:
        """登记维修窗口；window_id 可指定（重复则 409）或由系统生成。"""
        gun_id = str(_require(payload, "gun_id"))
        start = _parse_time(payload, "start")
        end = _parse_time(payload, "end")
        if end <= start:
            raise _validation("维修窗口 end 必须晚于 start")
        reason = str(payload.get("reason", ""))
        window_id = str(payload.get("window_id") or self.ids.new_id("mw"))
        now = self.clock.now()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            gun = repo.get_gun(conn, gun_id)
            if gun is None:
                raise _validation(f"充电枪不存在: {gun_id}")
            if repo.get_maintenance(conn, window_id) is not None:
                raise _conflict(f"维修窗口已存在: {window_id}")
            repo.upsert_maintenance(
                conn,
                window_id=window_id,
                gun_id=gun_id,
                station_id=gun.station_id,
                start_ts=start,
                end_ts=end,
                reason=reason,
                status="scheduled",
                version=1,
                updated_at=now,
            )
            repo.insert_event(
                conn,
                event_id=self.ids.new_id("evt"),
                batch_id=None,
                type=repo.EVENT_MAINTENANCE_UPSERT,
                station_id=gun.station_id,
                gun_id=gun_id,
                payload={"window_id": window_id, "start": start, "end": end, "reason": reason},
                occurred_at=start,
                recorded_at=now,
            )
            replay = self._replay(conn, gun.station_id, start, end, "maintenance_create")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"window_id": window_id, "version": 1, "replayed": replay}

    def update_maintenance(self, window_id: str, payload: dict) -> dict:
        """乐观并发更新：expected_version 不匹配则 409，不丢更新。"""
        expected = payload.get("expected_version")
        if not isinstance(expected, int) or expected < 1:
            raise _validation("expected_version 必须是正整数")
        start = _parse_time(payload, "start")
        end = _parse_time(payload, "end")
        if end <= start:
            raise _validation("维修窗口 end 必须晚于 start")
        reason = str(payload.get("reason", ""))
        status = str(payload.get("status", "scheduled"))
        if status not in ("scheduled", "cancelled"):
            raise _validation("status 仅支持 scheduled / cancelled")
        now = self.clock.now()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = repo.get_maintenance(conn, window_id)
            if existing is None:
                raise _not_found(f"维修窗口不存在: {window_id}")
            ok = repo.cas_update_maintenance(
                conn,
                window_id=window_id,
                expected_version=expected,
                start_ts=start,
                end_ts=end,
                reason=reason,
                status=status,
                updated_at=now,
            )
            if not ok:
                current = repo.get_maintenance(conn, window_id)
                raise _conflict(
                    "维修窗口已被并发修改，请基于最新版本重试",
                    {"current_version": current["version"]},
                )
            repo.insert_event(
                conn,
                event_id=self.ids.new_id("evt"),
                batch_id=None,
                type=(
                    repo.EVENT_MAINTENANCE_CANCELLED
                    if status == "cancelled"
                    else repo.EVENT_MAINTENANCE_UPSERT
                ),
                station_id=existing["station_id"],
                gun_id=existing["gun_id"],
                payload=(
                    {"window_id": window_id}
                    if status == "cancelled"
                    else {
                        "window_id": window_id,
                        "start": start,
                        "end": end,
                        "reason": reason,
                    }
                ),
                occurred_at=min(existing["start_ts"], start),
                recorded_at=now,
            )
            replay = self._replay(
                conn,
                existing["station_id"],
                min(existing["start_ts"], start),
                max(existing["end_ts"], end),
                "maintenance_update",
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"window_id": window_id, "version": expected + 1, "replayed": replay}

    # ------------------------------------------------------------ 规则升级

    def publish_rules(self, payload: dict) -> dict:
        """发布新版核算规则并重放 [effective_from, now] 受影响区间。

        旧切片仍按各自切片时刻的规则版本核算，已发布快照保持原值。
        """
        factors = payload.get("factors")
        if not isinstance(factors, dict) or not factors:
            raise _validation("factors 必须是非空对象")
        normalized_factors: dict[str, float] = {}
        for category, factor in factors.items():
            if not isinstance(factor, (int, float)) or not 0 < factor <= 1:
                raise _validation(f"类别 {category} 的降额系数必须在 (0, 1] 内: {factor!r}")
            normalized_factors[str(category)] = float(factor)
        if CATEGORY_UNKNOWN not in normalized_factors:
            normalized_factors[CATEGORY_UNKNOWN] = 0.5
        effective_from = _parse_time(payload, "effective_from")
        observed_cap = bool(payload.get("observed_power_cap", True))
        description = str(payload.get("description", ""))
        now = self.clock.now()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            version = repo.next_rule_version(conn)
            repo.insert_rule(
                conn,
                RuleSet(
                    version=version,
                    effective_from=effective_from,
                    factors=normalized_factors,
                    observed_power_cap=observed_cap,
                    description=description,
                ),
                created_at=now,
            )
            stations = [
                r["station_id"]
                for r in conn.execute(
                    "SELECT DISTINCT station_id FROM guns ORDER BY station_id"
                ).fetchall()
            ]
            replays = [
                self._replay(conn, station_id, effective_from, align_up(now), "rule_upgrade")
                for station_id in stations
            ]
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"version": version, "effective_from": format_iso8601(effective_from), "replayed": replays}

    # ------------------------------------------------------------ 快照

    def create_snapshot(self, payload: dict) -> dict:
        """发布区间容量快照：先重放保证新鲜，再签名冻结，之后不可变。"""
        station_id = str(_require(payload, "station_id"))
        start, end = _parse_range(payload)
        now = self.clock.now()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            guns = repo.load_guns(conn, station_id)
            if not guns:
                raise _not_found(f"场站不存在或没有充电枪: {station_id}")
            replay = self._replay(conn, station_id, start, end, "snapshot")
            range_start = align_down(start)
            range_end = align_up(end)
            rows = repo.read_slices(conn, station_id, range_start, range_end)
            circuits = repo.load_circuits(conn, station_id)
            rules = repo.load_rules(conn)
            snapshot_seq = repo.next_snapshot_seq(conn, station_id)
            high_water = repo.event_high_water(conn)

            by_slice: dict[int, list[dict]] = {}
            for row in rows:
                by_slice.setdefault(row["slice_start"], []).append(row)
            circuits_by_id = {c.circuit_id: c for c in circuits}
            gun_circuit = {g.gun_id: g.circuit_id for g in guns}
            slice_entries = []
            for slice_start in slice_starts(range_start, range_end):
                gun_entries = []
                circuit_alloc: dict[str, float] = {}
                station_total = 0.0
                for row in sorted(by_slice.get(slice_start, []), key=lambda r: r["gun_id"]):
                    station_total += row["allocated_kw"]
                    cid = gun_circuit.get(row["gun_id"])
                    if cid:
                        circuit_alloc[cid] = circuit_alloc.get(cid, 0.0) + row["allocated_kw"]
                    gun_entries.append(
                        {
                            "gun_id": row["gun_id"],
                            "effective_kw": row["effective_kw"],
                            "allocated_kw": row["allocated_kw"],
                            "rule_version": row["rule_version"],
                            "factors": row["explanation"]["factors"],
                        }
                    )
                slice_entries.append(
                    {
                        "slice_start": format_iso8601(slice_start),
                        "slice_start_epoch": slice_start,
                        "station_allocated_kw": round(station_total + 1e-9, 3),
                        "circuits": [
                            {
                                "circuit_id": cid,
                                "limit_kw": circuits_by_id[cid].limit_kw,
                                "allocated_kw": round(value + 1e-9, 3),
                            }
                            for cid, value in sorted(circuit_alloc.items())
                        ],
                        "guns": gun_entries,
                    }
                )
            snapshot_payload = {
                "station_id": station_id,
                "range_start": format_iso8601(range_start),
                "range_end": format_iso8601(range_end),
                "slice_seconds": SLICE_SECONDS,
                "snapshot_seq": snapshot_seq,
                "created_at": format_iso8601(now),
                "event_high_water": high_water,
                "rule_versions": [r.version for r in rules],
                "slices": slice_entries,
            }
            signature = self._signer.sign(snapshot_payload)
            snapshot_id = self.ids.new_id("snap")
            repo.insert_snapshot(
                conn,
                snapshot_id=snapshot_id,
                station_id=station_id,
                range_start=range_start,
                range_end=range_end,
                snapshot_seq=snapshot_seq,
                event_high_water=high_water,
                rule_versions=[r.version for r in rules],
                created_at=now,
                payload=canonical_json(snapshot_payload).decode("utf-8"),
                signature=signature,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {
            "snapshot_id": snapshot_id,
            "snapshot_seq": snapshot_seq,
            "station_id": station_id,
            "range_start": format_iso8601(range_start),
            "range_end": format_iso8601(range_end),
            "signature": signature,
            "replayed": replay,
        }

    def get_snapshot(self, snapshot_id: str) -> dict:
        conn = self._connect()
        try:
            row = repo.get_snapshot(conn, snapshot_id)
        finally:
            conn.close()
        if row is None:
            raise _not_found(f"快照不存在: {snapshot_id}")
        return {
            "snapshot_id": row["snapshot_id"],
            "station_id": row["station_id"],
            "range_start": format_iso8601(row["range_start"]),
            "range_end": format_iso8601(row["range_end"]),
            "snapshot_seq": row["snapshot_seq"],
            "event_high_water": row["event_high_water"],
            "rule_versions": row["rule_versions"],
            "created_at": format_iso8601(row["created_at"]),
            "signature": row["signature"],
            "payload": json.loads(row["payload"]),
        }

    def verify_snapshot(self, snapshot_id: str) -> dict:
        conn = self._connect()
        try:
            row = repo.get_snapshot(conn, snapshot_id)
        finally:
            conn.close()
        if row is None:
            raise _not_found(f"快照不存在: {snapshot_id}")
        valid = self._signer.verify(json.loads(row["payload"]), row["signature"])
        return {"snapshot_id": snapshot_id, "valid": valid}

    def diff_snapshots(self, from_id: str, to_id: str) -> dict:
        """对比两版快照，给出每处变化的原因与因果事件。"""
        conn = self._connect()
        try:
            from_row = repo.get_snapshot(conn, from_id)
            to_row = repo.get_snapshot(conn, to_id)
            if from_row is None:
                raise _not_found(f"快照不存在: {from_id}")
            if to_row is None:
                raise _not_found(f"快照不存在: {to_id}")
            if (
                from_row["station_id"] != to_row["station_id"]
                or from_row["range_start"] != to_row["range_start"]
                or from_row["range_end"] != to_row["range_end"]
            ):
                raise _validation("仅支持对比同场站、同区间的两版快照")
            causal = repo.list_events(
                conn,
                station_id=from_row["station_id"],
                seq_after=from_row["event_high_water"],
            )
        finally:
            conn.close()
        causal = [e for e in causal if e["seq"] <= to_row["event_high_water"]]

        from_payload = json.loads(from_row["payload"])
        to_payload = json.loads(to_row["payload"])

        def index(payload: dict) -> dict[tuple[int, str], dict]:
            result = {}
            for slice_entry in payload["slices"]:
                for gun_entry in slice_entry["guns"]:
                    result[(slice_entry["slice_start_epoch"], gun_entry["gun_id"])] = gun_entry
            return result

        from_index, to_index = index(from_payload), index(to_payload)
        changed_guns: set[str] = set()
        changes = []
        for key in sorted(to_index):
            old = from_index.get(key)
            new = to_index[key]
            if old is None:
                continue
            if (
                old["effective_kw"] != new["effective_kw"]
                or old["allocated_kw"] != new["allocated_kw"]
            ):
                slice_start, gun_id = key
                changed_guns.add(gun_id)
                reasons = _explain_factors(new["factors"], new["rule_version"])
                if not reasons and old["factors"]:
                    reasons.append("原影响因素已消除（告警解除/维修结束），详见因果事件")
                if old["rule_version"] != new["rule_version"]:
                    reasons.append(
                        f"核算规则版本变化：v{old['rule_version']} → v{new['rule_version']}"
                    )
                changes.append(
                    {
                        "slice_start": format_iso8601(slice_start),
                        "gun_id": gun_id,
                        "old": {
                            "effective_kw": old["effective_kw"],
                            "allocated_kw": old["allocated_kw"],
                            "rule_version": old["rule_version"],
                        },
                        "new": {
                            "effective_kw": new["effective_kw"],
                            "allocated_kw": new["allocated_kw"],
                            "rule_version": new["rule_version"],
                        },
                        "reasons": reasons,
                    }
                )

        range_start, range_end = from_row["range_start"], from_row["range_end"]
        causal_events = [
            {
                "event_id": e["event_id"],
                "type": e["type"],
                "gun_id": e["gun_id"],
                "occurred_at": format_iso8601(e["occurred_at"]),
                "summary": _summarize_event(e),
            }
            for e in causal
            if range_start <= e["occurred_at"] < range_end
            and (e["gun_id"] is None or e["gun_id"] in changed_guns)
        ]
        rule_changes = []
        if from_payload["rule_versions"] != to_payload["rule_versions"]:
            rule_changes.append(
                {
                    "from": from_payload["rule_versions"],
                    "to": to_payload["rule_versions"],
                    "summary": "核算规则版本发生变化，受影响切片按新版规则重放",
                }
            )
        return {
            "from_snapshot": from_id,
            "to_snapshot": to_id,
            "station_id": from_row["station_id"],
            "range_start": format_iso8601(range_start),
            "range_end": format_iso8601(range_end),
            "changed_slices": changes,
            "causal_events": causal_events,
            "rule_changes": rule_changes,
        }


_CATEGORY_NAMES = {
    "temperature_control": "温控",
    "power_distribution": "配电",
    "communication": "通信",
    "unknown": "未知",
}


def _explain_factors(factors: list[dict], rule_version: int) -> list[str]:
    """把切片因子翻译为可读的差异原因。"""
    reasons = []
    for factor in factors:
        kind = factor.get("kind")
        if kind == "maintenance":
            reasons.append(
                f"维修窗口 {factor['window_id']} 覆盖"
                f"（{factor['window_start']}~{factor['window_end']}），"
                "容量置零"
            )
        elif kind == "alarm":
            category = _CATEGORY_NAMES.get(factor["category"], factor["category"])
            binding = "，为最严格约束" if factor.get("binding") else ""
            reasons.append(
                f"{category}告警 {factor['alarm_id']} 按规则v{rule_version}"
                f"系数 {factor['factor']} 降额至 {factor['derated_kw']}kW{binding}"
            )
        elif kind == "observed_power":
            reasons.append(
                f"片内实测最大功率 {factor['observed_max_kw']}kW，据此收紧估值"
            )
        elif kind == "circuit_limit":
            reasons.append(
                f"回路 {factor['circuit_id']} 限额 {factor['limit_kw']}kW，"
                f"需求 {factor['demand_kw']}kW，按比例 {factor['scale']} 分摊"
            )
    return reasons


def _summarize_event(event: dict) -> str:
    payload = event["payload"]
    type_ = event["type"]
    if type_ == repo.EVENT_ALARM_RAISED:
        category = _CATEGORY_NAMES.get(payload["category"], payload["category"])
        return f"{category}告警 {payload['alarm_id']} 发起"
    if type_ == repo.EVENT_ALARM_CLEARED:
        return f"告警 {payload['alarm_id']} 解除（补录于 {format_iso8601(payload['cleared_at'])}）"
    if type_ == repo.EVENT_POWER_SAMPLES:
        return f"补录 {len(payload['samples'])} 条功率采样"
    if type_ == repo.EVENT_MAINTENANCE_UPSERT:
        return (
            f"维修窗口 {payload['window_id']} 登记/调整"
            f"（{format_iso8601(payload['start'])}~{format_iso8601(payload['end'])}）"
        )
    if type_ == repo.EVENT_MAINTENANCE_CANCELLED:
        return f"维修窗口 {payload['window_id']} 取消"
    return type_
