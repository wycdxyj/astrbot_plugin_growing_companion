"""时间感：当前时段、星期、距上次对话多久。可注入 now_fn，便于测试与加速。"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from .models import TimeSense

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def period_of(hour: int) -> str:
    if hour < 5:
        return "深夜"
    if hour < 7:
        return "凌晨"
    if hour < 9:
        return "早晨"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 17:
        return "下午"
    if hour < 19:
        return "傍晚"
    if hour < 23:
        return "晚上"
    return "深夜"


def absence_label(gap_s: float | None) -> str:
    if gap_s is None:
        return "第一次见面"
    h = gap_s / 3600
    if h < 0.1:
        return "刚刚还在聊"
    if h < 1:
        return "没过多久"
    if h < 6:
        return "隔了几个小时"
    if h < 20:
        return "隔了大半天"
    if h < 48:
        return "隔了一天多"
    if h < 24 * 7:
        return "好几天没聊了"
    return "很久很久没聊了"


class Clock:
    def __init__(self, tz_offset_hours: float = 8.0, now_fn: Callable[[], float] | None = None):
        self.tz = timezone(timedelta(hours=tz_offset_hours))
        self._now_fn = now_fn or time.time

    def now(self) -> float:
        return float(self._now_fn())

    def local(self, ts: float | None = None) -> datetime:
        return datetime.fromtimestamp(self.now() if ts is None else ts, tz=self.tz)

    def sense(self, last_contact_at: float | None, now: float | None = None) -> TimeSense:
        now = self.now() if now is None else now
        dt = self.local(now)
        gap = None if not last_contact_at else max(0.0, now - last_contact_at)
        return TimeSense(
            now=now, hour=dt.hour, weekday_cn=_WEEKDAYS[dt.weekday()], period=period_of(dt.hour),
            is_weekend=dt.weekday() >= 5, since_last_contact_s=gap, absence_label=absence_label(gap),
        )

    def hour_float(self, ts: float | None = None) -> float:
        dt = self.local(ts)
        return dt.hour + dt.minute / 60.0


def human_ago(seconds: float) -> str:
    """把时间差说成人话（也用于记忆的“约几天前”）。"""
    if seconds < 90:
        return "刚才"
    if seconds < 3600:
        return f"{int(seconds // 60)}分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)}小时前"
    d = seconds / 86400
    if d < 30:
        return f"{int(d)}天前"
    if d < 365:
        return f"{int(d // 30)}个月前"
    return f"{int(d // 365)}年前"
