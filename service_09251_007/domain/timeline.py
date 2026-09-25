"""时间片工具：统一使用 15 分钟半开区间 [start, end)，时区无关的朴素时间。"""
from __future__ import annotations

from datetime import datetime, timedelta

SLICE_MINUTES = 15
SLICE = timedelta(minutes=SLICE_MINUTES)
_EPOCH = datetime(1970, 1, 1)


def parse_dt(value: str | datetime) -> datetime:
    """解析 ISO8601；拒绝带时区的时间，跨午夜逻辑统一按朴素本地时间处理。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        raise ValueError("时间必须是不带时区的本地时间: %r" % value)
    return dt


def iso(dt: datetime) -> str:
    return dt.isoformat()


def floor_slice(dt: datetime) -> datetime:
    seconds = (dt - _EPOCH).total_seconds()
    return _EPOCH + timedelta(seconds=(seconds // SLICE.total_seconds()) * SLICE.total_seconds())


def ceil_slice(dt: datetime) -> datetime:
    floored = floor_slice(dt)
    return floored if floored == dt else floored + SLICE


def iter_slices(start: datetime, end: datetime):
    """遍历落在 [start, end) 内的全部时间片起点；start/end 会被取整到片边界。"""
    cur = floor_slice(start)
    stop = ceil_slice(end)
    while cur < stop:
        yield cur
        cur += SLICE


def slices_for_interval(start: datetime, end: datetime | None, horizon_end: datetime):
    """事件覆盖的时间片序列；end 为空表示持续中，用 horizon_end 封口。"""
    yield from iter_slices(start, end if end is not None else horizon_end)
