"""快照签名端口：HMAC-SHA256，密钥经环境变量或持久化注入。

密钥不写入源码目录：优先取环境变量 CAPACITY_SIGNING_KEY（hex），
否则由持久化层生成并保存在运行数据目录的 SQLite meta 表中，
保证服务重启后历史快照仍可验签。
"""
from __future__ import annotations

import hashlib
import hmac
import json


def canonical_json(payload: dict) -> bytes:
    """生成签名的规范化字节：键排序、无空白、UTF-8。"""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


class HmacSigner:
    """基于对称密钥的签名器。"""

    def __init__(self, key: bytes):
        if not key:
            raise ValueError("签名密钥不能为空")
        self._key = key

    def sign(self, payload: dict) -> str:
        return hmac.new(self._key, canonical_json(payload), hashlib.sha256).hexdigest()

    def verify(self, payload: dict, signature: str) -> bool:
        expected = self.sign(payload)
        return hmac.compare_digest(expected, signature)
