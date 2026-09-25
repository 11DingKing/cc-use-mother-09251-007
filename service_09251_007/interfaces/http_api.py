"""HTTP 接口边界。仅做 JSON 解析与错误映射，业务规则全部在应用层。

路由：
  POST /topology                 批量导入回路与枪
  POST /imports                  批量导入（告警/维修/曲线/规则，batch_id 幂等）
  POST /alarms                   单条告警上报（含解除）
  POST /maintenances             新建维修窗口
  POST /maintenances/events      追加维修更新（op_seq 幂等、expected_version 乐观锁）
  POST /curves                   补录功率曲线
  POST /rules                    规则升级
  POST /replays                  手动重放区间
  GET  /capacity?start=&end=     实时查询（可按 scope/target_id 过滤）
  POST /snapshots                快照签署
  GET  /snapshots/{id}           读取快照
  GET  /snapshots/{a}/diff/{b}   差异说明
  GET  /replays                  重放台账
  GET  /health                   完整性检查
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..errors import ConflictError, ImmutableError, NotFoundError, ValidationError
from ..application.service import CapacityService
from ..infrastructure.db import quick_check


class CapacityHTTPHandler(BaseHTTPRequestHandler):
    server_version = "CapacityService/1.0"
    service: CapacityService  # 由工厂注入到类上

    # -- 工具 -------------------------------------------------------------
    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError("请求体不是合法 JSON: %s" % exc)
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, (ValidationError, ValueError, TypeError, KeyError)):
            status, name = 400, "ValidationError"
            message = str(exc) or ("缺少字段: %s" % exc if isinstance(exc, KeyError)
                                   else "请求参数非法")
            self._send(status, {"error": name, "message": message})
            return
        status = {
            ValidationError: 400,
            NotFoundError: 404,
            ConflictError: 409,
            ImmutableError: 409,
        }.get(type(exc), 500)
        if status == 500:
            raise exc
        self._send(status, {"error": type(exc).__name__, "message": str(exc)})

    # -- 路由 -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)

            if path == "/health":
                ok = quick_check(self.service.repo.conn)
                self._send(200, {"status": "ok" if ok else "corrupt"})
            elif path == "/capacity":
                self._send(200, self.service.query_capacity(
                    qs["start"][0], qs["end"][0],
                    scope=qs.get("scope", [None])[0],
                    target_id=qs.get("target_id", [None])[0],
                ))
            elif path == "/replays":
                self._send(200, {"replays": self.service.replays()})
            else:
                m = re.fullmatch(r"/snapshots/([^/]+)", path)
                if m:
                    self._send(200, self.service.get_snapshot(m.group(1)))
                    return
                m = re.fullmatch(r"/snapshots/([^/]+)/diff/([^/]+)", path)
                if m:
                    self._send(200, self.service.diff_snapshots(m.group(1), m.group(2)))
                    return
                self._send(404, {"error": "NotFound", "message": path})
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            body = self._body()
            svc = self.service

            if path == "/topology":
                result = svc.upsert_topology(
                    body.get("circuits", []), body.get("guns", []))
            elif path == "/imports":
                result = svc.batch_import(body)
            elif path == "/alarms":
                result = svc.record_alarm(
                    event_ref=body["event_ref"],
                    gun_id=body["gun_id"],
                    factor=float(body["factor"]),
                    started_at=body["started_at"],
                    ended_at=body.get("ended_at"),
                    seq=int(body.get("seq", 0)),
                )
            elif path == "/maintenances":
                result = svc.create_maintenance(
                    window_ref=body["window_ref"],
                    scope=body["scope"],
                    target_id=body["target_id"],
                    planned_start=body["planned_start"],
                    planned_end=body["planned_end"],
                    factor=float(body.get("factor", 0.0)),
                )
            elif path == "/maintenances/events":
                result = svc.update_maintenance(
                    window_ref=body["window_ref"],
                    op_seq=int(body["op_seq"]),
                    status=body.get("status"),
                    started_at=body.get("started_at"),
                    ended_at=body.get("ended_at"),
                    factor=body.get("factor"),
                    expected_version=body.get("expected_version"),
                )
            elif path == "/curves":
                result = svc.import_curve_samples(
                    body["gun_id"], body.get("samples", []),
                    replay_now=body.get("replay_now", True),
                )
            elif path == "/rules":
                result = svc.upgrade_rule(
                    code=body["code"],
                    version=int(body["version"]),
                    params=body.get("params", {}),
                    effective_from=body["effective_from"],
                    replay_to=body.get("replay_to"),
                )
            elif path == "/replays":
                result = svc.replay(
                    body["start"], body.get("end"),
                    reason=body.get("reason", "manual"),
                    ref=body.get("ref"),
                )
            elif path == "/snapshots":
                result = svc.sign_snapshot(
                    snapshot_id=body["snapshot_id"],
                    start=body["start"],
                    end=body["end"],
                    signer=body.get("signer", ""),
                )
            else:
                self._send(404, {"error": "NotFound", "message": path})
                return
            self._send(200, result)
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def log_message(self, fmt, *args):  # 安静日志
        if self.server is not None and getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)


def build_server(db_path: str, host: str = "127.0.0.1", port: int = 8080,
                 clock=None) -> ThreadingHTTPServer:
    from ..infrastructure.db import connect, init_db
    from ..infrastructure.repository import Repository
    from ..ports import SystemClock

    # 每个工作线程持有自己的 SQLite 连接；WAL 允许并发写在库级排队而不串事务
    local = threading.local()
    connections: list = []
    conns_lock = threading.Lock()

    def thread_service() -> CapacityService:
        svc = getattr(local, "service", None)
        if svc is None:
            conn = connect(db_path)
            init_db(conn)
            with conns_lock:
                connections.append(conn)
            svc = CapacityService(Repository(conn), clock or SystemClock())
            local.service = svc
        return svc

    handler = type("BoundHandler", (CapacityHTTPHandler,),
                   {"service": property(lambda self: thread_service())})
    server = ThreadingHTTPServer((host, port), handler)
    server._connections = connections
    return server
