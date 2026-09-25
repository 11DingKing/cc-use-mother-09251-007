"""应用服务：编排采集数据 -> 重放核算 -> 落盘/签署的完整用例。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Optional

from ..domain import engine
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
from ..domain.timeline import SLICE, ceil_slice, floor_slice, iso, iter_slices, parse_dt
from ..errors import ConflictError, ImmutableError, NotFoundError, ValidationError
from ..infrastructure.repository import Repository
from ..ports import Clock

HORIZON_SLICES = 96  # 未解除事件向前重放 24 小时


def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class CapacityService:
    def __init__(self, repo: Repository, clock: Clock):
        self.repo = repo
        self.clock = clock

    # ---- 受影响区间 -----------------------------------------------------
    def affected_window(
        self,
        start: datetime,
        end: Optional[datetime],
        pad_before: bool = False,
    ) -> tuple[datetime, datetime]:
        """把事件区间扩展为受影响的重放时间片范围。

        pad_before: 补录历史事件时，起点所在片也要重算（区间按片裁剪，本就覆盖）。
        """
        s = floor_slice(start)
        if end is None:
            horizon = ceil_slice(self.clock.now()) + HORIZON_SLICES * SLICE
            e = horizon
        else:
            e = ceil_slice(end)
        return s, e

    def _extend_to_prior(self, w0: datetime, w1: datetime,
                         ref: Optional[str]) -> tuple[datetime, datetime]:
        """延伸到历史重放的最远终点，清理此前"持续中"写入的过期未来片。"""
        prior = self.repo.prior_replay_end(w0, ref)
        if prior:
            w1 = max(w1, ceil_slice(parse_dt(prior)))
        return w0, w1

    # ---- 重放 -----------------------------------------------------------
    def _load_inputs(self, start: datetime, end: datetime):
        guns = self.repo.list_guns()
        circuits = self.repo.list_circuits()
        rules = self.repo.list_rules()

        intervals: dict[str, list[InputInterval]] = {g.gun_id: [] for g in guns}
        for a in self.repo.list_active_alarms(start, end):
            intervals.setdefault(a["gun_id"], []).append(
                InputInterval(
                    kind=KIND_ALARM,
                    ref=a["event_ref"],
                    factor=a["factor"],
                    start=a["started_at"],
                    end=a["ended_at"],
                )
            )
        guns_by_circuit: dict[str, list[str]] = {}
        for g in guns:
            guns_by_circuit.setdefault(g.circuit_id, []).append(g.gun_id)
        for m in self.repo.list_maintenances(start, end):
            if m["scope"] == SCOPE_GUN:
                targets = [m["target_id"]]
            elif m["scope"] == SCOPE_CIRCUIT:
                targets = guns_by_circuit.get(m["target_id"], [])
            else:
                continue
            eff_start = m["started_at"] or m["planned_start"]
            eff_end = m["ended_at"] or m["planned_end"]
            for gid in targets:
                intervals.setdefault(gid, []).append(
                    InputInterval(
                        kind=KIND_MAINTENANCE,
                        ref=m["window_ref"],
                        factor=m["factor"],
                        start=eff_start,
                        end=eff_end,
                    )
                )
        samples = self.repo.list_curve_samples(start, end)
        return guns, circuits, rules, intervals, samples

    def replay(
        self,
        start: datetime,
        end: Optional[datetime] = None,
        reason: str = "manual",
        ref: Optional[str] = None,
    ) -> dict:
        """重算 [start,end) 内全部时间片并覆盖落盘；调用方负责事务或由本方法开事务。"""
        own_tx = not self.repo.in_transaction
        if own_tx:
            self.repo.begin()
        try:
            start = parse_dt(start)
            if end is not None:
                end = parse_dt(end)
            window_start = floor_slice(start)
            if end is None:
                _, window_end = self.affected_window(start, None)
            else:
                window_end = ceil_slice(end)
            if window_end <= window_start:
                raise ValidationError("重放区间为空")

            guns, circuits, rules, intervals, samples = self._load_inputs(
                window_start, window_end
            )
            slice_starts = list(iter_slices(window_start, window_end))
            result = engine.evaluate(
                guns, circuits, intervals, samples, rules, slice_starts
            )

            now = self.clock.now()
            gun_rows, circuit_rows = [], []
            totals = []
            for s in slice_starts:
                per_guns = result["guns"][s]
                for gid, gr in per_guns.items():
                    gun_rows.append(
                        self.repo._slice_row(
                            "gun", gid, s, gr.effective_kw, gr.basis(),
                            gr.rule.version, now,
                        )
                    )
                slice_total = 0.0
                for cid, crs in result["circuits"].items():
                    for cr in crs:
                        if cr.slice_start == s:
                            circuit_rows.append(
                                self.repo._slice_row(
                                    "circuit", cid, s, cr.effective_kw, cr.basis(),
                                    max(cr.rule_versions) if cr.rule_versions else 1,
                                    now,
                                )
                            )
                            slice_total += cr.effective_kw
                totals.append({"slice_start": iso(s), "total_kw": round(slice_total, 3)})

            self.repo.replace_slices(
                gun_rows, circuit_rows, window_start, window_end, now
            )
            changed = len(gun_rows) + len(circuit_rows)
            self.repo.log_replay(reason, ref, window_start, window_end, changed, now)
            if own_tx:
                self.repo.commit()
            return {
                "window_start": iso(window_start),
                "window_end": iso(window_end),
                "slices": len(slice_starts),
                "changed_rows": changed,
                "totals": totals,
            }
        except Exception:
            if own_tx:
                self.repo.rollback()
            raise

    # ---- 采集写入：告警 -------------------------------------------------
    def record_alarm(
        self,
        event_ref: str,
        gun_id: str,
        factor: float,
        started_at: str | datetime,
        ended_at: Optional[str | datetime] = None,
        seq: int = 0,
    ) -> dict:
        start = parse_dt(started_at)
        end = parse_dt(ended_at) if ended_at else None
        self._validate_factor(factor)
        if end is not None and end < start:
            raise ValidationError("告警结束时间早于开始时间")
        self.repo.begin()
        try:
            status = self.repo.upsert_alarm(
                event_ref, gun_id, factor, start, end, seq, self.clock.now()
            )
            if status == "applied":
                w0, w1 = self.affected_window(start, end)
                w0, w1 = self._extend_to_prior(w0, w1, event_ref)
                replay_info = self.replay(w0, w1, reason="alarm", ref=event_ref)
            else:
                replay_info = None
            self.repo.commit()
            return {"event_ref": event_ref, "status": status, "replay": replay_info}
        except Exception:
            self.repo.rollback()
            raise

    # ---- 采集写入：维修窗口 ---------------------------------------------
    def create_maintenance(
        self,
        window_ref: str,
        scope: str,
        target_id: str,
        planned_start: str | datetime,
        planned_end: str | datetime,
        factor: float = 0.0,
    ) -> dict:
        if scope not in (SCOPE_GUN, SCOPE_CIRCUIT):
            raise ValidationError("维修范围必须是 gun 或 circuit")
        self._validate_factor(factor)
        p_start = parse_dt(planned_start)
        p_end = parse_dt(planned_end)
        if p_end <= p_start:
            raise ValidationError("维修结束时间必须晚于开始时间（支持跨午夜）")
        self.repo.begin()
        try:
            status = self.repo.create_maintenance(
                window_ref, scope, target_id, factor, p_start, p_end,
                self.clock.now(),
            )
            if status == "applied":
                w0, w1 = self.affected_window(p_start, p_end)
                replay_info = self.replay(w0, w1, reason="maintenance", ref=window_ref)
            else:
                replay_info = None
            self.repo.commit()
            return {"window_ref": window_ref, "status": status, "replay": replay_info}
        except Exception:
            self.repo.rollback()
            raise

    def update_maintenance(self, window_ref: str, op_seq: int, **changes) -> dict:
        """追加一条维修更新并重折叠。op_seq 必须在窗口内单调、由调用方分配。

        并发两路更新使用不同 op_seq 时都会保留；携带 expected_version 时
        走乐观锁，版本不符直接冲突，要求调用方重读后重试。
        """
        now = self.clock.now()
        expected_version = changes.pop("expected_version", None)
        status = changes.get("status")
        if status and status not in ("planned", "in_progress", "done", "cancelled"):
            raise ValidationError("非法维修状态: %s" % status)
        factor = changes.get("factor")
        if factor is not None:
            self._validate_factor(factor)
        started_at = parse_dt(changes["started_at"]) if changes.get("started_at") else None
        ended_at = parse_dt(changes["ended_at"]) if changes.get("ended_at") else None

        self.repo.begin()
        try:
            applied = self.repo.apply_maintenance_event(
                window_ref, op_seq, now, status=status, started_at=started_at,
                ended_at=ended_at, factor=factor, expected_version=expected_version,
            )
            replay_info = None
            if applied["applied"]:
                m = self.repo.get_maintenance(window_ref)
                eff_start = m["started_at"] or m["planned_start"]
                eff_end = m["ended_at"] or m["planned_end"]
                w0, w1 = self.affected_window(eff_start, eff_end)
                w0, w1 = self._extend_to_prior(w0, w1, window_ref)
                replay_info = self.replay(
                    w0, w1, reason="maintenance_event", ref=window_ref
                )
            self.repo.commit()
            return {**applied, "replay": replay_info}
        except Exception:
            self.repo.rollback()
            raise

    # ---- 采集写入：曲线 / 主数据 / 规则 ---------------------------------
    def import_curve_samples(
        self, gun_id: str, samples: list[dict], replay_now: bool = True
    ) -> dict:
        parsed = []
        for item in samples:
            parsed.append((parse_dt(item["ts"]), float(item["kw"])))
        if not parsed:
            return {"inserted": 0}
        self.repo.begin()
        try:
            inserted = self.repo.insert_curve_samples(gun_id, parsed)
            replay_info = None
            if replay_now:
                w0 = floor_slice(min(t for t, _ in parsed))
                w1 = ceil_slice(max(t for t, _ in parsed))
                replay_info = self.replay(w0, w1, reason="curve", ref=gun_id)
            self.repo.commit()
            return {"inserted": inserted, "replay": replay_info}
        except Exception:
            self.repo.rollback()
            raise

    def upsert_topology(self, circuits: list[dict], guns: list[dict]) -> dict:
        self.repo.begin()
        try:
            circuit_objs = [
                Circuit(c["circuit_id"], float(c["limit_kw"]), c.get("name", ""))
                for c in circuits
            ]
            gun_objs = [
                Gun(g["gun_id"], g["circuit_id"], float(g["rated_kw"])) for g in guns
            ]
            known = {c.circuit_id for c in circuit_objs}
            existing = {c.circuit_id for c in self.repo.list_circuits()}
            for g in gun_objs:
                if g.circuit_id not in known and g.circuit_id not in existing:
                    raise ValidationError("枪 %s 引用了未知回路 %s" % (
                        g.gun_id, g.circuit_id))
            for c in circuit_objs:
                self.repo.upsert_circuit(c)
            for g in gun_objs:
                self.repo.upsert_gun(g)
            self.repo.commit()
            return {"circuits": len(circuit_objs), "guns": len(gun_objs)}
        except Exception:
            self.repo.rollback()
            raise

    def upgrade_rule(
        self,
        code: str,
        version: int,
        params: dict,
        effective_from: str | datetime,
        replay_to: Optional[str | datetime] = None,
    ) -> dict:
        """登记新版本规则；默认重放生效时间到当前片之后的全部区间。"""
        rule = RuleVersion(code, int(version), params, parse_dt(effective_from))
        self.repo.begin()
        try:
            status = self.repo.add_rule(rule, self.clock.now())
            replay_info = None
            if status == "applied":
                end = parse_dt(replay_to) if replay_to else ceil_slice(self.clock.now())
                replay_info = self.replay(
                    rule.effective_from, end, reason="rule_upgrade", ref=code
                )
            self.repo.commit()
            return {"rule": code, "version": version, "status": status,
                    "replay": replay_info}
        except Exception:
            self.repo.rollback()
            raise

    # ---- 批量导入（整批幂等） -------------------------------------------
    def batch_import(self, payload: dict) -> dict:
        batch_id = payload.get("batch_id")
        if not batch_id:
            raise ValidationError("batch_id 必填")
        now = self.clock.now()
        self.repo.begin()
        try:
            if not self.repo.claim_batch(batch_id, now, _canonical(payload)[:200]):
                self.repo.commit()
                return {"batch_id": batch_id, "status": "duplicate", "replay": None}

            windows: list[tuple[datetime, datetime]] = []
            topo = payload.get("topology")
            if topo:
                for c in topo.get("circuits", []):
                    self.repo.upsert_circuit(
                        Circuit(c["circuit_id"], float(c["limit_kw"]),
                                c.get("name", ""))
                    )
                for g in topo.get("guns", []):
                    self.repo.upsert_gun(
                        Gun(g["gun_id"], g["circuit_id"], float(g["rated_kw"]))
                    )

            counts = {"alarms": 0, "maintenances": 0, "samples": 0, "rules": 0}
            for a in payload.get("alarms", []):
                start = parse_dt(a["started_at"])
                end = parse_dt(a["ended_at"]) if a.get("ended_at") else None
                self._validate_factor(a["factor"])
                self.repo.upsert_alarm(
                    a["event_ref"], a["gun_id"], float(a["factor"]), start, end,
                    int(a.get("seq", 0)), now,
                )
                counts["alarms"] += 1
                windows.append(self.affected_window(start, end))
            for m in payload.get("maintenances", []):
                p_start, p_end = parse_dt(m["planned_start"]), parse_dt(m["planned_end"])
                self._validate_factor(m.get("factor", 0.0))
                self.repo.create_maintenance(
                    m["window_ref"], m["scope"], m["target_id"],
                    float(m.get("factor", 0.0)), p_start, p_end, now,
                )
                counts["maintenances"] += 1
                windows.append(self.affected_window(p_start, p_end))
            for chunk in payload.get("curves", []):
                parsed = [(parse_dt(x["ts"]), float(x["kw"]))
                          for x in chunk.get("samples", [])]
                if parsed:
                    self.repo.insert_curve_samples(chunk["gun_id"], parsed)
                    counts["samples"] += len(parsed)
                    windows.append((
                        floor_slice(min(t for t, _ in parsed)),
                        ceil_slice(max(t for t, _ in parsed)),
                    ))
            for r in payload.get("rules", []):
                rule = RuleVersion(
                    r["code"], int(r["version"]), r["params"],
                    parse_dt(r["effective_from"]),
                )
                if self.repo.add_rule(rule, now) == "applied":
                    counts["rules"] += 1
                    windows.append((
                        floor_slice(rule.effective_from),
                        ceil_slice(self.clock.now()),
                    ))

            replay_info = None
            if windows:
                w0 = min(w[0] for w in windows)
                w1 = max(w[1] for w in windows)
                w0, w1 = self._extend_to_prior(w0, w1, None)
                replay_info = self.replay(w0, w1, reason="batch", ref=batch_id)
            self.repo.commit()
            return {"batch_id": batch_id, "status": "applied",
                    "counts": counts, "replay": replay_info}
        except Exception:
            self.repo.rollback()
            raise

    # ---- 实时查询 -------------------------------------------------------
    def query_capacity(
        self,
        start: str | datetime,
        end: str | datetime,
        scope: Optional[str] = None,
        target_id: Optional[str] = None,
        auto_replay: bool = True,
    ) -> dict:
        s, e = parse_dt(start), parse_dt(end)
        if e <= s:
            raise ValidationError("查询区间为空")
        if auto_replay:
            # 缺片即重放，保证实时查询反映最新采集数据
            existing = {
                r["slice_start"]
                for r in self.repo.query_slices(floor_slice(s), ceil_slice(e),
                                                scope="circuit")
            }
            wanted = {iso(x) for x in iter_slices(floor_slice(s), ceil_slice(e))}
            if not wanted.issubset(existing):
                self.replay(floor_slice(s), ceil_slice(e), reason="query")
        rows = self.repo.query_slices(s, e, scope=scope, target_id=target_id)
        totals: dict[str, float] = {}
        for r in rows:
            if r["scope"] == "circuit":
                totals[r["slice_start"]] = round(
                    totals.get(r["slice_start"], 0.0) + r["effective_kw"], 3)
        return {
            "window_start": iso(floor_slice(s)),
            "window_end": iso(ceil_slice(e)),
            "slices": rows,
            "totals": [
                {"slice_start": k, "total_kw": v}
                for k, v in sorted(totals.items())
            ],
        }

    def replays(self, limit: int = 50) -> list[dict]:
        return self.repo.list_replays(limit)

    # ---- 快照签署（不可变） ---------------------------------------------
    def sign_snapshot(
        self,
        snapshot_id: str,
        start: str | datetime,
        end: str | datetime,
        signer: str = "",
    ) -> dict:
        s, e = floor_slice(parse_dt(start)), ceil_slice(parse_dt(end))
        self.repo.begin()
        try:
            data = self.query_capacity(s, e, auto_replay=True)
            content = {
                "window_start": iso(s),
                "window_end": iso(e),
                "slices": [
                    {
                        "scope": row["scope"],
                        "target_id": row["target_id"],
                        "slice_start": row["slice_start"],
                        "effective_kw": row["effective_kw"],
                        "rule_version": row["rule_version"],
                        "detail": row["detail"],
                    }
                    for row in data["slices"]
                ],
                "totals": data["totals"],
            }
            canonical = _canonical(content)
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            signed_at = iso(self.clock.now())
            rule_versions = sorted({row["rule_version"] for row in data["slices"]})
            try:
                self.repo.insert_snapshot({
                    "snapshot_id": snapshot_id,
                    "window_start": iso(s),
                    "window_end": iso(e),
                    "content": canonical,
                    "hash_sha256": digest,
                    "rule_versions": _canonical(rule_versions),
                    "signer": signer,
                    "signed_at": signed_at,
                })
            except ImmutableError:
                raise
            self.repo.commit()
            return {
                "snapshot_id": snapshot_id,
                "hash_sha256": digest,
                "window_start": iso(s),
                "window_end": iso(e),
                "signed_at": signed_at,
                "rule_versions": rule_versions,
                "immutable": True,
            }
        except Exception:
            self.repo.rollback()
            raise

    def get_snapshot(self, snapshot_id: str) -> dict:
        row = self.repo.get_snapshot(snapshot_id)
        return {
            "snapshot_id": row["snapshot_id"],
            "window_start": row["window_start"],
            "window_end": row["window_end"],
            "hash_sha256": row["hash_sha256"],
            "rule_versions": json.loads(row["rule_versions"]),
            "signer": row["signer"],
            "signed_at": row["signed_at"],
            "content": json.loads(row["content"]),
        }

    def diff_snapshots(self, base_id: str, target_id: str) -> dict:
        """两份已签署快照之间的逐片差异说明。"""
        base = self.get_snapshot(base_id)
        target = self.get_snapshot(target_id)

        def index(snap):
            return {
                (x["scope"], x["target_id"], x["slice_start"]): x["effective_kw"]
                for x in snap["content"]["slices"]
            }

        b, t = index(base), index(target)
        changes = []
        for key in sorted(set(b) | set(t)):
            old = b.get(key)
            new = t.get(key)
            if old == new:
                continue
            if old is None or new is None:
                kind = "added" if old is None else "removed"
                pct = None
            else:
                kind = "changed"
                pct = round((new - old) / old * 100, 2) if old else None
            changes.append({
                "scope": key[0],
                "target_id": key[1],
                "slice_start": key[2],
                "old_kw": old,
                "new_kw": new,
                "delta_kw": None if old is None or new is None else round(new - old, 3),
                "delta_pct": pct,
                "kind": kind,
            })
        return {
            "base": base_id,
            "target": target_id,
            "base_window": [base["window_start"], base["window_end"]],
            "target_window": [target["window_start"], target["window_end"]],
            "changes": changes,
            "change_count": len(changes),
        }

    # ---- 校验 -----------------------------------------------------------
    @staticmethod
    def _validate_factor(factor: float) -> None:
        f = float(factor)
        if not (0.0 <= f <= 1.0):
            raise ValidationError("降级因子必须在 [0,1] 之间，收到 %r" % factor)
