"""命令行入口：python3 -m service_09251_007 --db data/capacity.db --port 8080"""
from __future__ import annotations

import argparse

from .interfaces.http_api import build_server


def main() -> None:
    parser = argparse.ArgumentParser(description="充电设备降级容量核算服务")
    parser.add_argument("--db", default="data/capacity.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server = build_server(args.db, args.host, args.port)
    print("容量核算服务监听 http://%s:%s (db=%s)" % (args.host, args.port, args.db))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
