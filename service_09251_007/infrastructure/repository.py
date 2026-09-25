"""仓储：领域对象与 SQLite 行之间的转换与事务边界。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Optional

from ..domain.models import (
    Circuit,
    Gun,
    InputInterval,
    KIND_ALARM,
    KIND_MAINTENANCE,
    RuleVersion,
    SCOPE_CIRCUIT,
    SCOPE_GUN,
)
from ..domain.timeline import iso, parse_dt
from ..errors import ConflictError, ImmutableError, NotFoundError


class Repository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._in_transaction = False

    # ---- 事务原语 -------------------------------------------------------
    @property
    def in_transaction(self) -> bool:
        return self._in_transaction

    def begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        self._in_transaction = True

    def commit(self) -> None:
        self.conn.commit()
        self._in_transaction = False

    def rollback(self) -> None:
        self.conn.rollback()
        self._in_transaction = False

    # ---- 主数据 ---------------------------------------------------------
    def upsert_circuit(self, circuit: Circuit) -> None:
        self.conn.execute(
            "INSERT INTO circuits(circuit_id, limit_kw, name) VALUES (?,?,?) "
            "ON CONFLICT(circuit_id) DO UPDATE SET "
            "limit_kw=excluded.limit_kw, name=excluded.name",
            (circuit.circuit_id, circuit.limit_kw, circuit.name),
        )

    def upsert_gun(self, gun: Gun) -> None:
        self.conn.execute(
            "INSERT INTO guns(gun_id, circuit_id, rated_kw) VALUES (?,?,?) "
            "ON CONFLICT(gun_id) DO UPDATE SET "
            "circuit_id=excluded.circuit_id, rated_kw=excluded.rated_kw",
            (gun.gun_id, gun.circuit_id, gun.rated_kw),
        )

    def list_circuits(self) -> list[Circuit]:
        rows = self.conn.execute(
            "SELECT circuit_id, limit_kw, name FROM circuits ORDER BY circuit_id"
        ).fetchall()
        return [Circuit(r[0], r[1], r[2]) for r in rows]

    def list_guns(self) -> list[Gun]:
        rows = self.conn.execute(
            "SELECT gun_id, circuit_id, rated_kw FROM guns ORDER BY gun_id"
        ).fetchall()
        return [Gun(r[0], r[1], r[2]) for r in rows]

    def guns_of_circuit(self, circuit_id: str) -> list[Gun]:
        return [g for g in self.list_guns() if g.circuit_id == circuit_id]

    # ---- 告警（幂等 + seq 防止旧消息覆盖新消息） -------------------------
    def upsert_alarm(
        self,
        event_ref: str,
        gun_id: str,
        factor: float,
        started_at: datetime,
        ended_at: Optional[datetime],
        seq: int,
        now: datetime,
    ) -> str:
        """返回 applied（新写入）或 ignored（重复/过期消息）。"""
        row = self.conn.execute(
            "SELECT seq, gun_id FROM alarms WHERE event_ref=?", (event_ref,)
        ).fetchone()
        if row is not None:
            if seq < row["seq"]:
                return "ignored"
            if seq == row["seq"]:
                return "ignored"  # 同序号重投：幂等忽略
            if row["gun_id"] != gun_id:
                raise ConflictError("告警 %s 关联的枪与首次上报不一致" % event_ref)
            self.conn.execute(
                "UPDATE alarms SET factor=?, ended_at=?, seq=?, updated_at=? "
                "WHERE event_ref=?",
                (factor, iso(ended_at) if ended_at else None, seq, iso(now), event_ref),
            )
            return "applied"
        self.conn.execute(
            "INSERT INTO alarms(event_ref, gun_id, factor, started_at, ended_at, "
            "seq, updated_at) VALUES (?,?,?,?,?,?,?)",
            (
                event_ref,
                gun_id,
                factor,
                iso(started_at),
                iso(ended_at) if ended_at else None,
                seq,
                iso(now),
            ),
        )
        return "applied"

    def list_active_alarms(self, start: datetime, end: datetime) -> list[dict]:
        rows = self.conn.execute(
            "SELECT event_ref, gun_id, factor, started_at, ended_at "
            "FROM alarms WHERE started_at < ? AND "
            "(ended_at IS NULL OR ended_at > ?)",
            (iso(end), iso(start)),
        ).fetchall()
        return [
            {
                "event_ref": r["event_ref"],
                "gun_id": r["gun_id"],
                "factor": r["factor"],
                "started_at": parse_dt(r["started_at"]),
                "ended_at": parse_dt(r["ended_at"]) if r["ended_at"] else None,
            }
            for r in rows
        ]

    # ---- 维修窗口（追加日志 + 折叠，保证并发更新不丢失） ------------------
    def create_maintenance(
        self,
        window_ref: str,
        scope: str,
        target_id: str,
        factor: float,
        planned_start: datetime,
        planned_end: datetime,
        now: datetime,
    ) -> str:
        row = self.conn.execute(
            "SELECT window_ref FROM maintenances WHERE window_ref=?", (window_ref,)
        ).fetchone()
        if row is not None:
            return "ignored"
        self.conn.execute(
            "INSERT INTO maintenances(window_ref, scope, target_id, factor, "
            "planned_start, planned_end, status, version, updated_at) "
            "VALUES (?,?,?,?,?,?,'planned',1,?)",
            (
                window_ref,
                scope,
                target_id,
                factor,
                iso(planned_start),
                iso(planned_end),
                iso(now),
            ),
        )
        return "applied"

    def apply_maintenance_event(
        self,
        window_ref: str,
        op_seq: int,
        now: datetime,
        status: Optional[str] = None,
        started_at: Optional[datetime] = None,
        ended_at: Optional[datetime] = None,
        factor: Optional[float] = None,
        expected_version: Optional[int] = None,
    ) -> dict:
        row = self.conn.execute(
            "SELECT version, status FROM maintenances WHERE window_ref=?",
            (window_ref,),
        ).fetchone()
        if row is None:
            raise NotFoundError("维修窗口不存在: %s" % window_ref)

        payload = {
            "status": status,
            "started_at": iso(started_at) if started_at else None,
            "ended_at": iso(ended_at) if ended_at else None,
            "factor": factor,
        }
        existing = self.conn.execute(
            "SELECT status, started_at, ended_at, factor FROM maintenance_events "
            "WHERE window_ref=? AND op_seq=?",
            (window_ref, op_seq),
        ).fetchone()
        if existing is not None:
            old = dict(existing)
            if any(old[k] != payload[k] for k in payload):
                raise ConflictError(
                    "维修窗口 %s 的顺序号 %s 已用于不同内容" % (window_ref, op_seq)
                )
            return {"window_ref": window_ref, "op_seq": op_seq, "applied": False}

        # 新事件才校验乐观锁版本，保证同 op_seq 的重投永远幂等
        if expected_version is not None and row["version"] != expected_version:
            raise ConflictError(
                "维修窗口 %s 版本 %s 与期望 %s 不一致"
                % (window_ref, row["version"], expected_version)
            )

        self.conn.execute(
            "INSERT INTO maintenance_events(window_ref, op_seq, status, "
            "started_at, ended_at, factor, received_at) VALUES (?,?,?,?,?,?,?)",
            (
                window_ref,
                op_seq,
                status,
                payload["started_at"],
                payload["ended_at"],
                factor,
                iso(now),
            ),
        )
        self._fold_maintenance(window_ref, now)
        return {"window_ref": window_ref, "op_seq": op_seq, "applied": True}

    def _fold_maintenance(self, window_ref: str, now: datetime) -> None:
        """按 op_seq 重放追加日志，把各字段最后一次非空值折叠到主行。"""
        rows = self.conn.execute(
            "SELECT op_seq, status, started_at, ended_at, factor "
            "FROM maintenance_events WHERE window_ref=? ORDER BY op_seq",
            (window_ref,),
        ).fetchall()
        base = self.conn.execute(
            "SELECT status, factor FROM maintenances WHERE window_ref=?",
            (window_ref,),
        ).fetchone()
        folded = {
            "status": base["status"],
            "started_at": None,
            "ended_at": None,
            "factor": base["factor"],
        }
        for r in rows:
            for key in folded:
                val = r[key]
                if val is not None:
                    folded[key] = val
        self.conn.execute(
            "UPDATE maintenances SET status=?, started_at=?, ended_at=?, "
            "factor=?, version=version+1, updated_at=? WHERE window_ref=?",
            (
                folded["status"],
                folded["started_at"],
                folded["ended_at"],
                folded["factor"],
                iso(now),
                window_ref,
            ),
        )

    def list_maintenances(self, start: datetime, end: datetime) -> list[dict]:
        rows = self.conn.execute(
            "SELECT window_ref, scope, target_id, factor, planned_start, "
            "planned_end, started_at, ended_at, status "
            "FROM maintenances WHERE status != 'cancelled' "
            "AND COALESCE(started_at, planned_start) < ? "
            "AND COALESCE(ended_at, planned_end) > ?",
            (iso(end), iso(start)),
        ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "window_ref": r["window_ref"],
                    "scope": r["scope"],
                    "target_id": r["target_id"],
                    "factor": r["factor"],
                    "planned_start": parse_dt(r["planned_start"]),
                    "planned_end": parse_dt(r["planned_end"]),
                    "started_at": parse_dt(r["started_at"]) if r["started_at"] else None,
                    "ended_at": parse_dt(r["ended_at"]) if r["ended_at"] else None,
                    "status": r["status"],
                }
            )
        return out

    def get_maintenance(self, window_ref: str) -> dict:
        r = self.conn.execute(
            "SELECT window_ref, scope, target_id, factor, planned_start, "
            "planned_end, started_at, ended_at, status, version "
            "FROM maintenances WHERE window_ref=?",
            (window_ref,),
        ).fetchone()
        if r is None:
            raise NotFoundError("维修窗口不存在: %s" % window_ref)
        return {
            "window_ref": r["window_ref"],
            "scope": r["scope"],
            "target_id": r["target_id"],
            "factor": r["factor"],
            "planned_start": parse_dt(r["planned_start"]),
            "planned_end": parse_dt(r["planned_end"]),
            "started_at": parse_dt(r["started_at"]) if r["started_at"] else None,
            "ended_at": parse_dt(r["ended_at"]) if r["ended_at"] else None,
            "status": r["status"],
            "version": r["version"],
        }

    # ---- 功率曲线 -------------------------------------------------------
    def insert_curve_samples(
        self, gun_id: str, samples: list[tuple[datetime, float]]
    ) -> int:
        rows = [(gun_id, iso(t), kw) for t, kw in samples]
        cur = self.conn.executemany(
            "INSERT OR IGNORE INTO curve_samples(gun_id, ts, kw) VALUES (?,?,?)",
            rows,
        )
        return cur.rowcount if cur.rowcount != -1 else len(rows)

    def list_curve_samples(
        self, start: datetime, end: datetime
    ) -> dict[str, list[tuple[datetime, float]]]:
        rows = self.conn.execute(
            "SELECT gun_id, ts, kw FROM curve_samples WHERE ts >= ? AND ts < ? "
            "ORDER BY gun_id, ts",
            (iso(start), iso(end)),
        ).fetchall()
        out: dict[str, list[tuple[datetime, float]]] = {}
        for r in rows:
            out.setdefault(r["gun_id"], []).append((parse_dt(r["ts"]), r["kw"]))
        return out

    # ---- 规则 -----------------------------------------------------------
    def add_rule(self, rule: RuleVersion, now: datetime) -> str:
        try:
            self.conn.execute(
                "INSERT INTO rules(code, version, params, effective_from, created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    rule.code,
                    rule.version,
                    json.dumps(rule.params, ensure_ascii=False, sort_keys=True),
                    iso(rule.effective_from),
                    iso(now),
                ),
            )
            return "applied"
        except sqlite3.IntegrityError:
            return "ignored"

    def list_rules(self) -> list[RuleVersion]:
        rows = self.conn.execute(
            "SELECT code, version, params, effective_from FROM rules "
            "ORDER BY effective_from, version"
        ).fetchall()
        return [
            RuleVersion(
                code=r["code"],
                version=r["version"],
                params=json.loads(r["params"]),
                effective_from=parse_dt(r["effective_from"]),
            )
            for r in rows
        ]

    # ---- 核算结果落盘 ---------------------------------------------------
    def replace_slices(
        self,
        gun_rows: list[tuple],
        circuit_rows: list[tuple],
        window_start: datetime,
        window_end: datetime,
        now: datetime,
    ) -> None:
        self.conn.execute(
            "DELETE FROM capacity_slices WHERE slice_start >= ? AND slice_start < ?",
            (iso(window_start), iso(window_end)),
        )
        self.conn.executemany(
            "INSERT INTO capacity_slices(scope, target_id, slice_start, "
            "effective_kw, detail, rule_version, updated_at) VALUES (?,?,?,?,?,?,?)",
            gun_rows + circuit_rows,
        )

    @staticmethod
    def _slice_row(scope: str, target_id: str, start: datetime, kw: float,
                   detail: dict, rule_version: int, now: datetime) -> tuple:
        return (
            scope,
            target_id,
            iso(start),
            kw,
            json.dumps(detail, ensure_ascii=False, sort_keys=True),
            rule_version,
            iso(now),
        )

    def query_slices(
        self, start: datetime, end: datetime, scope: Optional[str] = None,
        target_id: Optional[str] = None,
    ) -> list[dict]:
        sql = (
            "SELECT scope, target_id, slice_start, effective_kw, detail, "
            "rule_version FROM capacity_slices WHERE slice_start >= ? "
            "AND slice_start < ?"
        )
        params: list = [iso(start), iso(end)]
        if scope:
            sql += " AND scope=?"
            params.append(scope)
        if target_id:
            sql += " AND target_id=?"
            params.append(target_id)
        sql += " ORDER BY slice_start, scope, target_id"
        rows = self.conn.execute(sql, params).fetchall()
        return [
            {
                "scope": r["scope"],
                "target_id": r["target_id"],
                "slice_start": r["slice_start"],
                "effective_kw": r["effective_kw"],
                "detail": json.loads(r["detail"]),
                "rule_version": r["rule_version"],
            }
            for r in rows
        ]

    def log_replay(
        self, reason: str, ref: Optional[str], window_start: datetime,
        window_end: datetime, changed: int, now: datetime,
    ) -> None:
        self.conn.execute(
            "INSERT INTO replays(reason, ref, window_start, window_end, "
            "changed_slices, replayed_at) VALUES (?,?,?,?,?,?)",
            (reason, ref, iso(window_start), iso(window_end), changed, iso(now)),
        )

    def list_replays(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT reason, ref, window_start, window_end, changed_slices, "
            "replayed_at FROM replays ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def prior_replay_end(self, start: datetime, ref: Optional[str] = None) -> Optional[str]:
        """此前覆盖过 start 的重放所到达的最远终点；ref 精确匹配优先。

        用于让"先持续中（重放到 24h 后）、后解除"的事件把旧未来片一并重算。
        """
        row = None
        if ref is not None:
            row = self.conn.execute(
                "SELECT MAX(window_end) AS m FROM replays WHERE ref=? AND "
                "window_start <= ?",
                (ref, iso(start)),
            ).fetchone()
        if row is None or row["m"] is None:
            row = self.conn.execute(
                "SELECT MAX(window_end) AS m FROM replays "
                "WHERE window_start <= ? AND window_end > ?",
                (iso(start), iso(start)),
            ).fetchone()
        return row["m"] if row and row["m"] else None

    # ---- 快照（不可变） -------------------------------------------------
    def insert_snapshot(self, snap: dict) -> None:
        try:
            self.conn.execute(
                "INSERT INTO snapshots(snapshot_id, window_start, window_end, "
                "content, hash_sha256, rule_versions, signer, signed_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    snap["snapshot_id"],
                    snap["window_start"],
                    snap["window_end"],
                    snap["content"],
                    snap["hash_sha256"],
                    snap["rule_versions"],
                    snap.get("signer", ""),
                    snap["signed_at"],
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("快照编号已存在: %s" % snap["snapshot_id"]) from exc
        except sqlite3.OperationalError as exc:
            raise ImmutableError(str(exc)) from exc

    def get_snapshot(self, snapshot_id: str) -> dict:
        row = self.conn.execute(
            "SELECT snapshot_id, window_start, window_end, content, hash_sha256, "
            "rule_versions, signer, signed_at FROM snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("快照不存在: %s" % snapshot_id)
        return dict(row)

    # ---- 导入批次 -------------------------------------------------------
    def claim_batch(self, batch_id: str, now: datetime, summary: str) -> bool:
        """成功占位返回 True；重复批次返回 False（整批幂等跳过）。"""
        try:
            self.conn.execute(
                "INSERT INTO import_batches(batch_id, received_at, summary) "
                "VALUES (?,?,?)",
                (batch_id, iso(now), summary),
            )
            return True
        except sqlite3.IntegrityError:
            return False
