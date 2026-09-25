"""仓储函数：在 SQLite 行与领域对象之间转换。

事件日志是唯一事实来源；这里既提供写入（幂等插入事件），也提供
把事件流装配为领域输入（告警区间、功率采样序列）的读取路径。
"""
from __future__ import annotations

import json
import sqlite3

from ..domain.models import (
    AlarmInterval,
    Circuit,
    Gun,
    GunSlice,
    MaintenanceWindow,
    PowerSample,
    RuleSet,
)

EVENT_ALARM_RAISED = "alarm_raised"
EVENT_ALARM_CLEARED = "alarm_cleared"
EVENT_POWER_SAMPLES = "power_samples"
EVENT_MAINTENANCE_UPSERT = "maintenance_upsert"
EVENT_MAINTENANCE_CANCELLED = "maintenance_cancelled"

EVENT_TYPES = frozenset(
    {
        EVENT_ALARM_RAISED,
        EVENT_ALARM_CLEARED,
        EVENT_POWER_SAMPLES,
        EVENT_MAINTENANCE_UPSERT,
        EVENT_MAINTENANCE_CANCELLED,
    }
)


# ---------------------------------------------------------------- 设备档案

def upsert_circuit(conn: sqlite3.Connection, circuit: Circuit) -> None:
    conn.execute(
        "INSERT INTO circuits(circuit_id, station_id, limit_kw) VALUES(?,?,?)"
        " ON CONFLICT(circuit_id) DO UPDATE SET"
        " station_id=excluded.station_id, limit_kw=excluded.limit_kw",
        (circuit.circuit_id, circuit.station_id, circuit.limit_kw),
    )


def upsert_gun(conn: sqlite3.Connection, gun: Gun) -> None:
    conn.execute(
        "INSERT INTO guns(gun_id, station_id, circuit_id, rated_kw) VALUES(?,?,?,?)"
        " ON CONFLICT(gun_id) DO UPDATE SET"
        " station_id=excluded.station_id, circuit_id=excluded.circuit_id,"
        " rated_kw=excluded.rated_kw",
        (gun.gun_id, gun.station_id, gun.circuit_id, gun.rated_kw),
    )


def load_guns(conn: sqlite3.Connection, station_id: str) -> list[Gun]:
    rows = conn.execute(
        "SELECT gun_id, station_id, circuit_id, rated_kw FROM guns"
        " WHERE station_id=? ORDER BY gun_id",
        (station_id,),
    ).fetchall()
    return [
        Gun(
            gun_id=r["gun_id"],
            station_id=r["station_id"],
            circuit_id=r["circuit_id"],
            rated_kw=r["rated_kw"],
        )
        for r in rows
    ]


def load_circuits(conn: sqlite3.Connection, station_id: str) -> list[Circuit]:
    rows = conn.execute(
        "SELECT circuit_id, station_id, limit_kw FROM circuits"
        " WHERE station_id=? ORDER BY circuit_id",
        (station_id,),
    ).fetchall()
    return [
        Circuit(
            circuit_id=r["circuit_id"],
            station_id=r["station_id"],
            limit_kw=r["limit_kw"],
        )
        for r in rows
    ]


def get_gun(conn: sqlite3.Connection, gun_id: str) -> Gun | None:
    row = conn.execute(
        "SELECT gun_id, station_id, circuit_id, rated_kw FROM guns WHERE gun_id=?",
        (gun_id,),
    ).fetchone()
    if row is None:
        return None
    return Gun(
        gun_id=row["gun_id"],
        station_id=row["station_id"],
        circuit_id=row["circuit_id"],
        rated_kw=row["rated_kw"],
    )


# ---------------------------------------------------------------- 规则版本

def load_rules(conn: sqlite3.Connection) -> list[RuleSet]:
    rows = conn.execute(
        "SELECT version, effective_from, payload FROM rule_sets ORDER BY version"
    ).fetchall()
    rules = []
    for r in rows:
        payload = json.loads(r["payload"])
        rules.append(
            RuleSet(
                version=r["version"],
                effective_from=r["effective_from"],
                factors={k: float(v) for k, v in payload["factors"].items()},
                observed_power_cap=bool(payload.get("observed_power_cap", True)),
                description=payload.get("description", ""),
            )
        )
    return rules


def insert_rule(conn: sqlite3.Connection, rule: RuleSet, created_at: int) -> None:
    payload = {
        "factors": rule.factors,
        "observed_power_cap": rule.observed_power_cap,
        "description": rule.description,
    }
    conn.execute(
        "INSERT INTO rule_sets(version, effective_from, payload, created_at)"
        " VALUES(?,?,?,?)",
        (
            rule.version,
            rule.effective_from,
            json.dumps(payload, ensure_ascii=False),
            created_at,
        ),
    )


def next_rule_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 AS v FROM rule_sets").fetchone()
    return int(row["v"])


# ---------------------------------------------------------------- 事件日志

def insert_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    batch_id: str | None,
    type: str,
    station_id: str | None,
    gun_id: str | None,
    payload: dict,
    occurred_at: int,
    recorded_at: int,
) -> bool:
    """幂等插入事件；已存在相同 event_id 时返回 False（重复）。"""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO events"
        "(event_id, batch_id, type, station_id, gun_id, payload, occurred_at, recorded_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (
            event_id,
            batch_id,
            type,
            station_id,
            gun_id,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            occurred_at,
            recorded_at,
        ),
    )
    return cursor.rowcount == 1


def record_batch(conn: sqlite3.Connection, batch_id: str, recorded_at: int, count: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO batches(batch_id, recorded_at, event_count) VALUES(?,?,?)",
        (batch_id, recorded_at, count),
    )


def event_high_water(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS hw FROM events").fetchone()
    return int(row["hw"])


def list_events(
    conn: sqlite3.Connection,
    *,
    station_id: str | None = None,
    seq_after: int | None = None,
) -> list[dict]:
    sql = "SELECT seq, event_id, type, station_id, gun_id, payload, occurred_at, recorded_at FROM events"
    clauses, params = [], []
    if station_id is not None:
        clauses.append("station_id=?")
        params.append(station_id)
    if seq_after is not None:
        clauses.append("seq>?")
        params.append(seq_after)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY seq"
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "seq": r["seq"],
            "event_id": r["event_id"],
            "type": r["type"],
            "station_id": r["station_id"],
            "gun_id": r["gun_id"],
            "payload": json.loads(r["payload"]),
            "occurred_at": r["occurred_at"],
            "recorded_at": r["recorded_at"],
        }
        for r in rows
    ]


def find_alarm_raise(
    conn: sqlite3.Connection, alarm_id: str
) -> dict | None:
    """按 alarm_id 找最近的告警发起事件（校验解除事件用）。"""
    rows = conn.execute(
        "SELECT payload FROM events WHERE type=? ORDER BY seq",
        (EVENT_ALARM_RAISED,),
    ).fetchall()
    found = None
    for r in rows:
        payload = json.loads(r["payload"])
        if payload.get("alarm_id") == alarm_id:
            found = payload
    return found


def load_alarm_intervals(conn: sqlite3.Connection, station_id: str) -> list[AlarmInterval]:
    """把告警事件流装配为活动区间。

    按 (occurred_at, seq) 回放：raise 开启区间，clear 关闭；同一 alarm_id
    重复 raise 时先关闭上一段（防御脏数据），未匹配的 clear 忽略。
    """
    events = [
        e
        for e in list_events(conn, station_id=station_id)
        if e["type"] in (EVENT_ALARM_RAISED, EVENT_ALARM_CLEARED)
    ]
    events.sort(key=lambda e: (e["occurred_at"], e["seq"]))
    open_intervals: dict[str, AlarmInterval] = {}
    closed: list[AlarmInterval] = []
    for event in events:
        payload = event["payload"]
        alarm_id = payload["alarm_id"]
        if event["type"] == EVENT_ALARM_RAISED:
            previous = open_intervals.pop(alarm_id, None)
            if previous is not None:
                closed.append(
                    AlarmInterval(
                        alarm_id=previous.alarm_id,
                        gun_id=previous.gun_id,
                        category=previous.category,
                        started_at=previous.started_at,
                        cleared_at=payload["started_at"],
                    )
                )
            open_intervals[alarm_id] = AlarmInterval(
                alarm_id=alarm_id,
                gun_id=event["gun_id"],
                category=payload["category"],
                started_at=payload["started_at"],
                cleared_at=None,
            )
        else:
            current = open_intervals.pop(alarm_id, None)
            if current is not None:
                closed.append(
                    AlarmInterval(
                        alarm_id=current.alarm_id,
                        gun_id=current.gun_id,
                        category=current.category,
                        started_at=current.started_at,
                        cleared_at=payload["cleared_at"],
                    )
                )
    return closed + list(open_intervals.values())


def load_power_samples(conn: sqlite3.Connection, station_id: str) -> list[PowerSample]:
    samples: list[PowerSample] = []
    for event in list_events(conn, station_id=station_id):
        if event["type"] != EVENT_POWER_SAMPLES:
            continue
        for sample in event["payload"]["samples"]:
            samples.append(
                PowerSample(
                    gun_id=event["gun_id"],
                    ts=int(sample["ts"]),
                    kw=float(sample["kw"]),
                )
            )
    return samples


# ---------------------------------------------------------------- 维修窗口

def get_maintenance(conn: sqlite3.Connection, window_id: str) -> dict | None:
    row = conn.execute(
        "SELECT window_id, gun_id, station_id, start_ts, end_ts, reason, status,"
        " version, updated_at FROM maintenance_windows WHERE window_id=?",
        (window_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def upsert_maintenance(
    conn: sqlite3.Connection,
    *,
    window_id: str,
    gun_id: str,
    station_id: str,
    start_ts: int,
    end_ts: int,
    reason: str,
    status: str,
    version: int,
    updated_at: int,
) -> None:
    conn.execute(
        "INSERT INTO maintenance_windows"
        "(window_id, gun_id, station_id, start_ts, end_ts, reason, status, version, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(window_id) DO UPDATE SET"
        " gun_id=excluded.gun_id, station_id=excluded.station_id,"
        " start_ts=excluded.start_ts, end_ts=excluded.end_ts,"
        " reason=excluded.reason, status=excluded.status,"
        " version=excluded.version, updated_at=excluded.updated_at",
        (window_id, gun_id, station_id, start_ts, end_ts, reason, status, version, updated_at),
    )


def cas_update_maintenance(
    conn: sqlite3.Connection,
    *,
    window_id: str,
    expected_version: int,
    start_ts: int,
    end_ts: int,
    reason: str,
    status: str,
    updated_at: int,
) -> bool:
    """乐观并发更新：仅当版本匹配时生效，返回是否成功。"""
    cursor = conn.execute(
        "UPDATE maintenance_windows SET start_ts=?, end_ts=?, reason=?, status=?,"
        " version=version+1, updated_at=?"
        " WHERE window_id=? AND version=?",
        (start_ts, end_ts, reason, status, updated_at, window_id, expected_version),
    )
    return cursor.rowcount == 1


def load_maintenance(conn: sqlite3.Connection, station_id: str) -> list[MaintenanceWindow]:
    rows = conn.execute(
        "SELECT window_id, gun_id, start_ts, end_ts, reason, status"
        " FROM maintenance_windows WHERE station_id=?",
        (station_id,),
    ).fetchall()
    return [
        MaintenanceWindow(
            window_id=r["window_id"],
            gun_id=r["gun_id"],
            start_ts=r["start_ts"],
            end_ts=r["end_ts"],
            reason=r["reason"],
            status=r["status"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------- 派生切片

def replace_slices(
    conn: sqlite3.Connection,
    station_id: str,
    range_start: int,
    range_end: int,
    slices: list[GunSlice],
) -> None:
    """用重放结果整体替换 [range_start, range_end) 内的派生切片。"""
    conn.execute(
        "DELETE FROM capacity_slices"
        " WHERE station_id=? AND slice_start>=? AND slice_start<?",
        (station_id, range_start, range_end),
    )
    conn.executemany(
        "INSERT INTO capacity_slices"
        "(station_id, slice_start, gun_id, rule_version, effective_kw, allocated_kw, explanation)"
        " VALUES(?,?,?,?,?,?,?)",
        [
            (
                station_id,
                s.slice_start,
                s.gun_id,
                s.rule_version,
                s.effective_kw,
                s.allocated_kw,
                json.dumps(s.explanation(), ensure_ascii=False, sort_keys=True),
            )
            for s in slices
        ],
    )


def read_slices(
    conn: sqlite3.Connection, station_id: str, range_start: int, range_end: int
) -> list[dict]:
    rows = conn.execute(
        "SELECT slice_start, gun_id, rule_version, effective_kw, allocated_kw, explanation"
        " FROM capacity_slices"
        " WHERE station_id=? AND slice_start>=? AND slice_start<?"
        " ORDER BY slice_start, gun_id",
        (station_id, range_start, range_end),
    ).fetchall()
    return [
        {
            "slice_start": r["slice_start"],
            "gun_id": r["gun_id"],
            "rule_version": r["rule_version"],
            "effective_kw": r["effective_kw"],
            "allocated_kw": r["allocated_kw"],
            "explanation": json.loads(r["explanation"]),
        }
        for r in rows
    ]


def drop_all_slices(conn: sqlite3.Connection) -> None:
    """清空派生切片表（恢复重建用；事件日志不受影响）。"""
    conn.execute("DELETE FROM capacity_slices")


def record_replay(
    conn: sqlite3.Connection,
    *,
    station_id: str | None,
    range_start: int,
    range_end: int,
    reason: str,
    detail: str,
    created_at: int,
) -> None:
    conn.execute(
        "INSERT INTO replay_runs(station_id, range_start, range_end, reason, detail, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (station_id, range_start, range_end, reason, detail, created_at),
    )


# ---------------------------------------------------------------- 快照

def insert_snapshot(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str,
    station_id: str,
    range_start: int,
    range_end: int,
    snapshot_seq: int,
    event_high_water: int,
    rule_versions: list[int],
    created_at: int,
    payload: str,
    signature: str,
) -> None:
    conn.execute(
        "INSERT INTO snapshots"
        "(snapshot_id, station_id, range_start, range_end, snapshot_seq,"
        " event_high_water, rule_versions, created_at, payload, signature)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            snapshot_id,
            station_id,
            range_start,
            range_end,
            snapshot_seq,
            event_high_water,
            json.dumps(rule_versions),
            created_at,
            payload,
            signature,
        ),
    )


def get_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> dict | None:
    row = conn.execute(
        "SELECT snapshot_id, station_id, range_start, range_end, snapshot_seq,"
        " event_high_water, rule_versions, created_at, payload, signature"
        " FROM snapshots WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["rule_versions"] = json.loads(result["rule_versions"])
    return result


def next_snapshot_seq(conn: sqlite3.Connection, station_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(snapshot_seq), 0) + 1 AS s FROM snapshots WHERE station_id=?",
        (station_id,),
    ).fetchone()
    return int(row["s"])


# ---------------------------------------------------------------- 元数据

def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
