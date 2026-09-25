"""运行配置：全部来自环境变量，运行数据不写入源码目录。"""
from __future__ import annotations

import os

# SQLite 数据库路径（运行数据，默认放在仓库外的 var/ 或环境指定位置）。
DB_PATH = os.environ.get("CAPACITY_DB_PATH", os.path.join("var", "capacity.db"))

# HTTP 监听地址。
HOST = os.environ.get("CAPACITY_HOST", "127.0.0.1")
PORT = int(os.environ.get("CAPACITY_PORT", "8092"))

# 快照签名密钥（hex）。缺省时由服务生成并持久化在数据库 meta 表。
SIGNING_KEY = os.environ.get("CAPACITY_SIGNING_KEY")
