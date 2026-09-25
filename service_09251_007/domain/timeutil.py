"""时间工具：统一使用 UTC epoch 秒，时间片按固定粒度对齐。

所有对外接口使用 ISO 8601 字符串（必须带时区），内部一律转换为
epoch 秒整数，避免夏令时与本地时区歧义；跨午夜窗口因此只是普通的
[start, end) 区间。
"""
from __future__ import annotations

from datetime import datetime, timezone

SLICE_SECONDS = 15 * 60  # 时间片粒度：15 分钟


def parse_iso8601(value: str) -> int:
    """把带时区的 ISO 8601 字符串解析为 epoch 秒。

    >>> parse_iso8601("1970-01-01T00:00:00Z")
    0
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"时间必须是非空字符串: {value!r}")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析的时间: {value!r}") from exc
    if dt.tzinfo is None:
        raise ValueError(f"时间必须携带时区: {value!r}")
    return int(dt.timestamp())


def format_iso8601(epoch_seconds: int) -> str:
    """把 epoch 秒格式化为 UTC ISO 8601 字符串。"""
    return datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def align_down(ts: int, slice_seconds: int = SLICE_SECONDS) -> int:
    """向下对齐到时间片起点。"""
    return ts - (ts % slice_seconds)


def align_up(ts: int, slice_seconds: int = SLICE_SECONDS) -> int:
    """向上对齐到时间片起点（已对齐则保持不变）。"""
    remainder = ts % slice_seconds
    return ts if remainder == 0 else ts + (slice_seconds - remainder)


def slice_starts(start: int, end: int, slice_seconds: int = SLICE_SECONDS):
    """生成 [start, end) 内所有时间片起点，start 会先向下对齐。"""
    current = align_down(start, slice_seconds)
    while current < end:
        yield current
        current += slice_seconds
