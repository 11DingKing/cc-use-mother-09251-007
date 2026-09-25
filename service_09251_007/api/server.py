"""HTTP API：基于标准库 http.server 的 REST 边界。

路由一律返回 JSON；业务异常按 ServiceError 的状态码与错误码返回，
未捕获异常兜底为 500。服务层每个方法独立开连接，配合
ThreadingHTTPServer 可安全处理并发请求。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..application.services import CapacityService, ServiceError
from ..domain.timeutil import parse_iso8601

_JSON = "application/json; charset=utf-8"


def _parse_query_times(query: dict[str, list[str]]) -> tuple[int, int]:
    try:
        start = parse_iso8601(query["start"][0])
        end = parse_iso8601(query["end"][0])
    except (KeyError, IndexError, ValueError) as exc:
        raise ServiceError(400, "bad_request", f"查询参数 start/end 缺失或非法: {exc}")
    return start, end


class ApiHandler(BaseHTTPRequestHandler):
    """每个请求独立分发；service 由 server 实例注入。"""

    server: "ApiServer"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------- 基础工具

    def _send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", _JSON)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ServiceError(400, "bad_request", "请求体不能为空")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceError(400, "bad_request", f"请求体不是合法 JSON: {exc}")
        if not isinstance(payload, dict):
            raise ServiceError(400, "bad_request", "请求体必须是 JSON 对象")
        return payload

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 保持静默
        return

    def do_GET(self) -> None:  # noqa: N802 - 标准库约定
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    # ---------------------------------------------------------- 路由

    def _dispatch(self, method: str) -> None:
        service = self.server.service
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/api/v1/health":
                return self._send_json(200, {"status": "ok"})

            if method == "POST" and path == "/api/v1/devices":
                return self._send_json(200, service.import_devices(self._read_json()))

            if method == "POST" and path == "/api/v1/events/batch":
                return self._send_json(200, service.import_events(self._read_json()))

            if method == "GET" and path == "/api/v1/capacity":
                station = query.get("station_id", [None])[0]
                if not station:
                    raise ServiceError(400, "bad_request", "缺少查询参数 station_id")
                start, end = _parse_query_times(query)
                return self._send_json(200, service.query_capacity(station, start, end))

            if method == "POST" and path == "/api/v1/maintenance":
                return self._send_json(201, service.create_maintenance(self._read_json()))

            match = re.fullmatch(r"/api/v1/maintenance/([^/]+)", path)
            if method == "PUT" and match:
                return self._send_json(
                    200, service.update_maintenance(match.group(1), self._read_json())
                )

            if method == "POST" and path == "/api/v1/rules":
                return self._send_json(201, service.publish_rules(self._read_json()))

            if method == "POST" and path == "/api/v1/snapshots":
                return self._send_json(201, service.create_snapshot(self._read_json()))

            if method == "GET" and path == "/api/v1/snapshots/diff":
                from_id = query.get("from_id", [None])[0]
                to_id = query.get("to_id", [None])[0]
                if not from_id or not to_id:
                    raise ServiceError(400, "bad_request", "缺少查询参数 from_id/to_id")
                return self._send_json(200, service.diff_snapshots(from_id, to_id))

            match = re.fullmatch(r"/api/v1/snapshots/([^/]+)/verify", path)
            if method == "POST" and match:
                return self._send_json(200, service.verify_snapshot(match.group(1)))

            match = re.fullmatch(r"/api/v1/snapshots/([^/]+)", path)
            if method == "GET" and match:
                return self._send_json(200, service.get_snapshot(match.group(1)))

            if method == "POST" and path == "/api/v1/admin/rebuild":
                return self._send_json(200, service.rebuild())

            raise ServiceError(404, "not_found", f"路由不存在: {method} {path}")
        except ServiceError as exc:
            return self._send_json(exc.status, exc.to_dict())
        except BrokenPipeError:
            return
        except Exception as exc:  # 兜底：不泄露内部细节
            return self._send_json(
                500, {"error": {"code": "internal_error", "message": str(exc)}}
            )


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: CapacityService):
        super().__init__(address, ApiHandler)
        self.service = service


def create_server(service: CapacityService, host: str, port: int) -> ApiServer:
    """创建 HTTP 服务实例（port=0 时由系统分配端口，便于测试）。"""
    return ApiServer((host, port), service)


def main() -> None:
    from .. import config

    service = CapacityService(config.DB_PATH)
    server = create_server(service, config.HOST, config.PORT)
    host, port = server.server_address[:2]
    print(f"充电设备降级容量核算服务已启动: http://{host}:{port}/api/v1/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
