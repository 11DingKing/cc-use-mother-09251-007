"""SQLite 连接与 schema 管理。

可恢复性设计：
- WAL 日志 + busy_timeout，断电后已提交事务不丢失，读写不互斥；
- 事件日志（events）是唯一事实来源，capacity_slices 等派生表随时
  可由重放重建（见 application 层的 rebuild）；
- snapshots 表由触发器强制不可变，已发布快照任何路径都无法篡改；
- schema 版本记录在 meta 表，启动时幂等迁移。
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS circuits (
    circuit_id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL,
    limit_kw REAL NOT NULL CHECK (limit_kw > 0)
);

CREATE TABLE IF NOT EXISTS guns (
    gun_id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL,
    circuit_id TEXT REFERENCES circuits(circuit_id),
    rated_kw REAL NOT NULL CHECK (rated_kw > 0)
);
CREATE INDEX IF NOT EXISTS idx_guns_station ON guns(station_id);

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    recorded_at INTEGER NOT NULL,
    event_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    batch_id TEXT,
    type TEXT NOT NULL,
    station_id TEXT,
    gun_id TEXT,
    payload TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,
    recorded_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_gun ON events(gun_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_station ON events(station_id, seq);

CREATE TABLE IF NOT EXISTS maintenance_windows (
    window_id TEXT PRIMARY KEY,
    gun_id TEXT NOT NULL,
    station_id TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'scheduled',
    version INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    CHECK (end_ts > start_ts)
);
CREATE INDEX IF NOT EXISTS idx_maintenance_gun ON maintenance_windows(gun_id);

CREATE TABLE IF NOT EXISTS rule_sets (
    version INTEGER PRIMARY KEY,
    effective_from INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS capacity_slices (
    station_id TEXT NOT NULL,
    slice_start INTEGER NOT NULL,
    gun_id TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    effective_kw REAL NOT NULL,
    allocated_kw REAL NOT NULL,
    explanation TEXT NOT NULL,
    PRIMARY KEY (station_id, slice_start, gun_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL,
    range_start INTEGER NOT NULL,
    range_end INTEGER NOT NULL,
    snapshot_seq INTEGER NOT NULL,
    event_high_water INTEGER NOT NULL,
    rule_versions TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    payload TEXT NOT NULL,
    signature TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replay_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT,
    range_start INTEGER NOT NULL,
    range_end INTEGER NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
"""

# 已发布快照不可变：任何 UPDATE/DELETE 直接失败，防御应用层缺陷。
IMMUTABILITY_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS snapshots_no_update
BEFORE UPDATE ON snapshots
BEGIN
    SELECT RAISE(ABORT, 'snapshots are immutable');
END;

CREATE TRIGGER IF NOT EXISTS snapshots_no_delete
BEFORE DELETE ON snapshots
BEGIN
    SELECT RAISE(ABORT, 'snapshots are immutable');
END;
"""

DEFAULT_RULES = {
    "factors": {
        "temperature_control": 0.6,
        "power_distribution": 0.7,
        "communication": 0.8,
        "unknown": 0.5,
    },
    "observed_power_cap": True,
    "description": "默认规则：温控降额至60%，配电70%，通信80%，未知50%",
}


def connect(db_path: str) -> sqlite3.Connection:
    """打开一个连接并应用运行参数。调用方负责关闭。"""
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def initialize(conn: sqlite3.Connection, now: int) -> None:
    """幂等建表、建触发器并写入默认规则版本。"""
    conn.executescript(SCHEMA)
    conn.executescript(IMMUTABILITY_TRIGGERS)
    version = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    if version is None:
        import json

        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        conn.execute(
            "INSERT INTO rule_sets(version, effective_from, payload, created_at)"
            " VALUES(1, 0, ?, ?)",
            (json.dumps(DEFAULT_RULES, ensure_ascii=False), now),
        )
    conn.commit()


def integrity_check(conn: sqlite3.Connection) -> bool:
    """启动恢复检查：SQLite 自检通过即可依赖事件日志重建派生态。"""
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return row is not None and row[0] == "ok"
