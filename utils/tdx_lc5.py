# -*- coding: utf-8 -*-
"""通达信本地 .lc5 五分钟线解析器（fail-closed）。

结构合约（TDX 标准 lc5，32 字节/记录，小端）::

    date(H) time(H) open(f32) high(f32) low(f32) close(f32)
    amount(f32, 元) volume(U32, 股) reserved(U32)

单位合约（config/minute_units.json: minute-lots-yuan-v1）::

    - 正式分钟表 volume 单位 = 手（lots，1 手 = 100 股），amount = 元；
    - lc5 股票 volume 单位 = 股 → 换算 ``shares // 100``，且必须校验
      ``shares % 100 == 0``（整除校验是单位错配的 fail-closed 探测：若源
      已是手或口径变化，这里会立刻炸而不是静默写脏数据）；
    - 指数（000xxx.SH / 399xxx.SZ）不换算（TDX 指数 volume 无手概念）。

时间口径：lc5 的 date/time 为北京墙面时间、5 分钟 bar 以**结束时刻**标记
（第一根 09:35、最后一根 15:00），与正式表既有行的存储口径一致。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence

__all__ = [
    "LC5_RECORD_SIZE",
    "LC5Record",
    "Lc5FormatError",
    "MORNING_SESSION_START",
    "AFTERNOON_SESSION_START",
    "STANDARD_5M_BAR_ENDS",
    "is_index_code",
    "shares_to_lots",
    "decode_lc5_datetime",
    "parse_lc5_file",
    "validate_code_day_bars",
    "aggregate_5m",
]

LC5_RECORD = struct.Struct("<HHfffffII")
LC5_RECORD_SIZE = LC5_RECORD.size  # 32

MORNING_SESSION_START = dtime(9, 30)
AFTERNOON_SESSION_START = dtime(13, 0)


class Lc5FormatError(ValueError):
    """lc5 文件/记录不合约：一律 fail-closed，禁止静默跳过。"""


@dataclass(frozen=True)
class LC5Record:
    code: str
    datetime: datetime  # 北京墙面时间，bar 结束时刻
    open: float
    high: float
    low: float
    close: float
    volume: int  # 已换算为正式表单位：股票=手，指数=原值
    amount: float  # 元


def _standard_bar_ends() -> frozenset:
    ends = []
    t = datetime(2000, 1, 1, 9, 35)
    while t.time() <= dtime(11, 30):
        ends.append(t.time().replace(second=0, microsecond=0))
        t += timedelta(minutes=5)
    t = datetime(2000, 1, 1, 13, 5)
    while t.time() <= dtime(15, 0):
        ends.append(t.time().replace(minute=t.minute))
        t += timedelta(minutes=5)
    return frozenset(ends)


#: 标准 5 分钟 bar 结束时刻全集（上午 09:35–11:30 共 24 根 + 下午 13:05–15:00 共 24 根）
STANDARD_5M_BAR_ENDS = _standard_bar_ends()
assert len(STANDARD_5M_BAR_ENDS) == 48


def is_index_code(code: str) -> bool:
    """TDX 指数代码：000xxx.SH（上证指数族）与 399xxx.SZ（深证指数族）。"""
    code = str(code or "").strip().upper()
    return code.startswith("000") and code.endswith(".SH") or code.startswith("399") and code.endswith(".SZ")


def shares_to_lots(code: str, shares: int) -> int:
    """lc5 股（股票）→ 正式表手；指数不换算。非整百 fail-closed。"""
    if shares < 0:
        raise Lc5FormatError(f"{code}: 负 volume={shares} 不合约")
    if is_index_code(code):
        return int(shares)
    if shares % 100 != 0:
        raise Lc5FormatError(
            f"{code}: volume={shares} 股不是 100 的整数倍，疑似单位错配（fail-closed）")
    return shares // 100


def decode_lc5_datetime(raw_date: int, raw_time: int) -> Optional[datetime]:
    """lc5 date/time 压缩字段 → 北京墙面时间；非法值返回 None（由调用方 fail-closed）。"""
    year = raw_date // 2048 + 2004
    remainder = raw_date % 2048
    month, day = remainder // 100, remainder % 100
    hour, minute = raw_time // 60, raw_time % 60
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def parse_lc5_file(path: Path, code: str, days: Sequence[str]) -> Dict[str, List[LC5Record]]:
    """解析单个 lc5 文件，只保留 ``days``（ISO 日期集合）内的记录。

    fail-closed：文件尺寸非 32 倍数、日期解码失败、非正价格、量整除校验失败
    都抛 :class:`Lc5FormatError`——不静默丢行。
    """
    data = Path(path).read_bytes()
    if len(data) % LC5_RECORD_SIZE != 0:
        raise Lc5FormatError(f"{code}: 文件大小 {len(data)} 不是 {LC5_RECORD_SIZE} 的倍数")
    day_set = {str(d) for d in days}
    by_day: Dict[str, List[LC5Record]] = {d: [] for d in sorted(day_set)}
    for offset in range(0, len(data), LC5_RECORD_SIZE):
        raw_date, raw_time, o, h, l, c, amount, shares, _reserved = LC5_RECORD.unpack_from(data, offset)
        dt = decode_lc5_datetime(raw_date, raw_time)
        if dt is None:
            raise Lc5FormatError(f"{code}: offset={offset} 日期字段解码失败 ({raw_date},{raw_time})")
        key = dt.strftime("%Y-%m-%d")
        if key not in day_set:
            continue
        if o <= 0 or h <= 0 or l <= 0 or c <= 0:
            raise Lc5FormatError(f"{code}: {dt} 非正价格 o={o} h={h} l={l} c={c}")
        by_day[key].append(
            LC5Record(
                code=code,
                datetime=dt,
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=shares_to_lots(code, int(shares)),
                amount=float(amount or 0.0),
            )
        )
    return by_day


def validate_code_day_bars(code: str, day: str, bars: Sequence[LC5Record]) -> None:
    """一个 (code, day) 必须恰好 48 根、时间戳与标准 5m 网格一一对应、无重复。"""
    stamps = [b.datetime for b in bars]
    if len(stamps) != 48:
        raise Lc5FormatError(f"{code} {day}: 槽数 {len(stamps)} != 48")
    seen = set()
    for dt in stamps:
        if dt.strftime("%Y-%m-%d") != day:
            raise Lc5FormatError(f"{code} {day}: 混入他日 bar {dt}")
        key = dt.time().replace(second=0, microsecond=0)
        if key in seen:
            raise Lc5FormatError(f"{code} {day}: 重复时间戳 {dt}")
        if key not in STANDARD_5M_BAR_ENDS:
            raise Lc5FormatError(f"{code} {day}: 时间戳 {dt} 不在标准 5m 网格")
        seen.add(key)
    if len(seen) != 48:
        raise Lc5FormatError(f"{code} {day}: 网格槽位缺失（{len(seen)}/48）")


def _session_start_of(dt: datetime) -> datetime:
    base = dt.replace(hour=MORNING_SESSION_START.hour, minute=MORNING_SESSION_START.minute, second=0, microsecond=0)
    if dt.hour >= 12:
        base = dt.replace(hour=AFTERNOON_SESSION_START.hour, minute=AFTERNOON_SESSION_START.minute, second=0, microsecond=0)
    return base


def aggregate_5m(bars: Sequence[LC5Record], minutes: int) -> List[LC5Record]:
    """按正式表既有口径从 5m 聚合高周期（15/30/60）。

    网格（与库内既有行反推一致）：上午桶界 09:30+k*M（<=11:30），下午桶界
    13:00+k*M（<=15:00）；每根 5m bar 归入同会话内不早于它的最小桶界；
    O=首 bar open、H=max、L=min、C=末 bar close、量额求和。
    """
    if minutes % 5 or minutes <= 5:
        raise ValueError(f"minutes 必须是 >5 的 5 的倍数: {minutes}")
    groups: Dict[datetime, List[LC5Record]] = {}
    for bar in sorted(bars, key=lambda b: b.datetime):
        session_start = _session_start_of(bar.datetime)
        offset = int((bar.datetime - session_start).total_seconds() // 60)
        if offset <= 0:
            bucket = session_start + timedelta(minutes=minutes)
        else:
            bucket = session_start + timedelta(minutes=((offset + minutes - 1) // minutes) * minutes)
        groups.setdefault(bucket, []).append(bar)
    out: List[LC5Record] = []
    for bucket in sorted(groups):
        items = groups[bucket]
        out.append(
            LC5Record(
                code=items[0].code,
                datetime=bucket,
                open=items[0].open,
                high=max(b.high for b in items),
                low=min(b.low for b in items),
                close=items[-1].close,
                volume=sum(b.volume for b in items),
                amount=round(sum(b.amount for b in items), 4),
            )
        )
    return out
