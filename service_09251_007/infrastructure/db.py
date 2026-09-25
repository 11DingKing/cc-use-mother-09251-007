"""SQLite 连接与 schema。

- 全部写入走事务（BEGIN IMMEDIATE），崩溃后由 WAL 自动恢复；
- snapshots 表带 UPDATE/DELETE 触发器，已签署快照物理上不可变；
- 事件表靠 UNIQUE 约束实现幂等。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS circuits (
    circuit_id TEXT PRIMARY KEY,
    limit_kw REAL NOT NULL,
    name TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS guns (
    gun_id TEXT PRIMARY KEY,
    circuit_id TEXT NOT NULL REFERENCES circuits(circuit_id),
    rated_kw REAL NOT NULL
);

-- 告警：同一 event_ref 的重复上报幂等；seq 防止迟到的旧消息覆盖解除消息
CREATE TABLE IF NOT EXISTS alarms (
    event_ref TEXT PRIMARY KEY,
    gun_id TEXT NOT NULL,
    factor REAL NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    seq INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alarms_gun_time
    ON alarms(gun_id, started_at, ended_at);

-- 维修窗口主表，version 做乐观并发
CREATE TABLE IF NOT EXISTS maintenances (
    window_ref TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('gun', 'circuit')),
    target_id TEXT NOT NULL,
    factor REAL NOT NULL DEFAULT 0,
    planned_start TEXT NOT NULL,
    planned_end TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('planned', 'in_progress', 'done', 'cancelled')),
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_maint_target
    ON maintenances(scope, target_id);

-- 维修更新追加日志：并发更新各自落一行，UNIQUE 保证同一顺序号只生效一次
CREATE TABLE IF NOT EXISTS maintenance_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_ref TEXT NOT NULL REFERENCES maintenances(window_ref),
    op_seq INTEGER NOT NULL,
    status TEXT,
    started_at TEXT,
    ended_at TEXT,
    factor REAL,
    received_at TEXT NOT NULL,
    UNIQUE(window_ref, op_seq)
);

CREATE TABLE IF NOT EXISTS curve_samples (
    gun_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kw REAL NOT NULL,
    PRIMARY KEY (gun_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_curves_time ON curve_samples(ts);

CREATE TABLE IF NOT EXISTS rules (
    code TEXT NOT NULL,
    version INTEGER NOT NULL,
    params TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (code, version)
);

-- 最近一次核算落盘结果（重放时覆盖），用于实时查询与审计
CREATE TABLE IF NOT EXISTS capacity_slices (
    scope TEXT NOT NULL CHECK (scope IN ('gun', 'circuit')),
    target_id TEXT NOT NULL,
    slice_start TEXT NOT NULL,
    effective_kw REAL NOT NULL,
    detail TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, target_id, slice_start)
);
CREATE INDEX IF NOT EXISTS idx_slices_start ON capacity_slices(slice_start);

-- 重放台账：每次受影响区间重算都留痕
CREATE TABLE IF NOT EXISTS replays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reason TEXT NOT NULL,
    ref TEXT,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    changed_slices INTEGER NOT NULL,
    replayed_at TEXT NOT NULL
);

-- 已发布快照：触发器拒绝任何修改与删除
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    content TEXT NOT NULL,
    hash_sha256 TEXT NOT NULL,
    rule_versions TEXT NOT NULL,
    signer TEXT NOT NULL DEFAULT '',
    signed_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS snapshots_no_update
BEFORE UPDATE ON snapshots
BEGIN SELECT RAISE(ABORT, 'snapshot is immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_delete
BEFORE DELETE ON snapshots
BEGIN SELECT RAISE(ABORT, 'snapshot is immutable'); END;

-- 批量导入幂等键
CREATE TABLE IF NOT EXISTS import_batches (
    batch_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    summary TEXT NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if path.parent != Path("") and not str(path.parent).startswith(":"):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )


def quick_check(conn: sqlite3.Connection) -> bool:
    """供启动/恢复流程调用的完整性检查。"""
    return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
