"""应用引导：一个函数组装数据库、仓储、时钟与服务。"""
from __future__ import annotations

from pathlib import Path

from .application.service import CapacityService
from .infrastructure.db import connect, init_db, quick_check
from .infrastructure.repository import Repository
from .ports import Clock, SystemClock


def build_service(db_path: str | Path, clock: Clock | None = None,
                  check_integrity: bool = True) -> CapacityService:
    conn = connect(db_path)
    init_db(conn)
    if check_integrity and not quick_check(conn):
        raise RuntimeError("SQLite 完整性检查失败: %s" % db_path)
    return CapacityService(Repository(conn), clock or SystemClock())
