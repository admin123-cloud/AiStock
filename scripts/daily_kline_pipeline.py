# -*- coding: utf-8 -*-
"""日缎 K 线管道（2026-11-02 起的未来交易日，收盘后运行）。

设计要点（与 tests/test_daily_kline_pipeline.py 一一对应）：
- 默认模式（显式 --trade-date 且不带 --stage-write/--formal-write）**零数据库访问**：
  只跑离线门禁（OHLC 包络 / 量额非负 / NEEQ 安全码 / 重复键），依赖正式表只读的门禁显式 skipped。
- 有三种情况会连数据库（读一律只读 SELECT，SETTINGS readonly=1）：
  1) 调用方省略 --trade-date → 用正式表最近有行日期 + trade_calendar 日历判断解析交易日；
  2) --stage-write → 先跑全部门禁（含 DB 门禁），全过后才建 stage 表、插入、读回验证；
  3) --formal-write → 先跑全部门禁（含 DB 门禁），全过后 acquire
     DbWriteLock(scope='kline_daily_daily_append')，锁内重新执行缺失键反连接
     （只插缺失 (code,trade_date)；重叠键值不一致 fail-closed 不写），插入正式契约列
     （id=cityHash64(code,trade_date)、created_at=批次时间戳作回滚 marker），读回验证
     （行数/抽样逐字段/日 digest/id 服务端一致性），不符则按 created_at marker 回滚删净。
- DB 写目标仅限硬编码白名单：--stage-write 只能写 stock.kline_daily_pkg_stage_20261101；
  --formal-write 只能写 stock.kline_daily；两者互斥（同时传直接报错）。
- 血缘字段（source_url/zip_sha256）只进报告，绝不进正式表。
- R17（2026-11-09）：index 类中 code ∈ 冻结大盘指数集（FROZEN_INDEX_CODES）的行
  纳入写入，与个股同门禁/id/only-missing-keys/读回验证/回滚 marker；指数 volume 包内
  原始即「手」，不做 ÷100（个股才是 股÷100→手）。其余 index（含 597 清单内的行业指数）
  与 tdx_internal_index 仍排除；excluded_by_category 为适配层原始普查口径（含被重新
  纳入的冻结码），最终去向以 row_counts.index_inclusion 为准；包内缺码在 missing_codes
  如实报告，绝不硬造。
- R18（2026-11-09）：FROZEN_INDEX_CODES 移除 000985.SH 中证全指（11→10 码）——官网
  包从不含该码（R17 实证）、历史 K 线停在 10-21，Owner 裁决下线；该码回归排除类。
- 任一门禁失败 → 退出码非 0 且不写任何表；报告（含血缘/行数/分类计数/拒绝清单）总是落盘；
  --formal-write 另有 JSONL journal（原子分段写）记录锁/反连接/插入/读回/回滚全程。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import socket
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.db_write_lock import DbWriteLock, LockError, LockHeldError
from utils.market_sources import tdx_official_package as tdx
from utils.market_sources import tencent_daily
from utils.security_market_policy import is_neeq_security_code

from scripts import tdx_lday_loader as lday

__all__ = [
    "PIPELINE_VERSION", "STAGE_TABLE", "STAGE_TABLE_WHITELIST", "KLINE_DAILY_TABLE",
    "FORMAL_WRITE_TABLE_WHITELIST", "FORMAL_COLUMNS", "FORMAL_APPEND_LOCK_SCOPE",
    "GATE_NAMES", "assert_stage_table_allowed", "assert_formal_table_allowed",
    "city_hash64", "clickhouse_int_hash64", "hash128to64", "formal_row_id",
    "default_output_path", "default_formal_journal_path",
    "resolve_latest_completed_trade_date", "run_pipeline", "main", "parse_args",
    "FROZEN_INDEX_CODES", "extract_frozen_index_rows", "frozen_index_rows_to_contract",
]

PIPELINE_VERSION = "20261109.2"
STAGE_TABLE_WHITELIST = ("stock.kline_daily_pkg_stage_20261101",)
STAGE_TABLE = STAGE_TABLE_WHITELIST[0]
KLINE_DAILY_TABLE = "stock.kline_daily"          # 正式表：默认只读；--formal-write 白名单写入
FORMAL_WRITE_TABLE_WHITELIST = (KLINE_DAILY_TABLE,)
FORMAL_APPEND_LOCK_SCOPE = "kline_daily_daily_append"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
# 官网约 16:10（北京时间）发布当日盘后包；之前的当日会话不算"已完成"
PACKAGE_PUBLISH_CUTOFF = time(16, 10)
DEFAULT_CROSSCHECK_SAMPLE = 20
# 与正式表已有重叠键比对容差（同日重复拉取一致性）：价 1%、量额 3%
PRICE_TOLERANCE = 0.01
QUANTITY_TOLERANCE = 0.03
# 各板块行数与前一交易日对比的异常阈值（超出即门禁失败）
MAX_BOARD_COUNT_REL_CHANGE = 0.10
MAX_BOARD_COUNT_ABS_CHANGE = 500
# 腾讯交叉验证：样本里价值不符即失败；全部样本都取不到数据也视为失败
CROSSCHECK_SAMPLE_CAP = 100

# ── --source local（Owner 2026-11-13 裁决：日线走 TDX 客户端本地 lday 文件）──
DEFAULT_TDX_VIPDOC_ROOT = r"D:/TDX/vipdoc"
# 时序门禁：mtime≥16:00 且含当日记录的 lday 文件数下限（下载完成证据的体量下限；
# 全市场 universe≈5600，停牌/数据缺失余量后取 5000）
DEFAULT_MIN_FRESH_LDAY_FILES = 5000
# 覆盖率门禁：写入后正式表该日行数下限（与官网包口径 5581、lc5 覆盖对齐）
DEFAULT_COVERAGE_MIN = 5500

GATE_NAMES = (
    "row_count_vs_prev_day",
    "ohlc_envelope",
    "non_negative_volume_amount",
    "neeq_policy",
    "duplicate_keys",
    "formal_overlap_consistency",
    "tencent_crosscheck",
)

READONLY_SETTINGS = {"readonly": 1, "max_execution_time": 300}

# stage 表列（含血缘列）；插入列序即此元组
STAGE_COLUMNS = (
    "code", "trade_date", "open", "high", "low", "close", "volume", "amount",
    "prev_close", "name", "market", "source_url", "zip_sha256", "fetched_at",
    "pipeline_version", "created_at",
)
DIGEST_FIELDS = ("code", "open", "high", "low", "close", "volume", "amount")

# 读回验证查询的列（不含 trade_date/created_at；时间列不参与逐值比对）
STAGE_READBACK_COLUMNS = DIGEST_FIELDS + ("prev_close", "name", "market",
                                          "source_url", "zip_sha256", "pipeline_version")

# 正式表 stock.kline_daily 契约列（与迁移候选/存量接入一致）；插入列序即此元组。
# 血缘列（source_url/zip_sha256/...）绝不入正式表；id=cityHash64(code,trade_date)；
# created_at=批次时间戳（同批同行同值，兼作回滚 marker）。
FORMAL_COLUMNS = (
    "code", "trade_date", "open", "high", "low", "close", "volume", "amount",
    "amplitude", "change_pct", "change_amount", "turnover_rate", "created_at", "id",
)
FORMAL_READBACK_COLUMNS = ("code", "open", "high", "low", "close", "volume", "amount",
                           "amplitude", "change_pct", "change_amount", "turnover_rate", "id")

STAGE_DDL = f"""CREATE TABLE IF NOT EXISTS {STAGE_TABLE}
(
    `code` String,
    `trade_date` Date,
    `open` Float64,
    `high` Float64,
    `low` Float64,
    `close` Float64,
    `volume` Float64 COMMENT '手 (lots) = 包内股数/100',
    `amount` Float64 COMMENT '元 (CNY)',
    `prev_close` Float64,
    `name` String,
    `market` LowCardinality(String),
    `source_url` String COMMENT '血缘: 盘后包下载地址',
    `zip_sha256` String COMMENT '血缘: 盘后包 zip sha256',
    `fetched_at` DateTime COMMENT '血缘: 抓取时间(北京时间)',
    `pipeline_version` String COMMENT '血缘: 管道版本',
    `created_at` DateTime
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(trade_date)
ORDER BY (trade_date, code)"""

_DATE_DASH = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_COMPACT = re.compile(r"^\d{8}$")


# ── 小工具 ───────────────────────────────────────────────────────────
def _to_iso_day(value: object) -> str:
    """'2026-11-02' / '20261102' / date/datetime → '2026-11-02'；其他抛 ValueError。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if _DATE_DASH.match(text):
        return text
    if _DATE_COMPACT.match(text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    raise ValueError(f"无法识别的交易日写法 {value!r}（支持 YYYY-MM-DD / YYYYMMDD）")


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, path)


def _rel_diff(new: float, old: float) -> float:
    """相对差 |new-old| / max(|old|, |new|, 1)；避免除零。"""
    denominator = max(abs(old), abs(new), 1.0)
    return abs(new - old) / denominator


def assert_stage_table_allowed(table: str) -> None:
    """stage 写入目标表名必须在硬编码白名单里；其他任何表名直接抛错。"""
    if table not in STAGE_TABLE_WHITELIST:
        raise ValueError(
            f"stage 写入目标表 {table!r} 不在硬编码白名单 {STAGE_TABLE_WHITELIST} 里；"
            f"正式表 {KLINE_DAILY_TABLE} 绝对禁止写入"
        )


def assert_formal_table_allowed(table: str) -> None:
    """formal 写入目标表名必须在硬编码白名单里；其他任何表名直接抛错。"""
    if table not in FORMAL_WRITE_TABLE_WHITELIST:
        raise ValueError(
            f"formal 写入目标表 {table!r} 不在硬编码白名单 {FORMAL_WRITE_TABLE_WHITELIST} 里；"
            f"仅允许正式表 {KLINE_DAILY_TABLE}"
        )


def default_output_path(trade_date: str, data_root: Optional[Path] = None) -> Path:
    """默认报告路径：<data_root>/runtime/daily_kline_coverage/daily_pipeline/<date>/report.json。"""
    root = Path(data_root) if data_root is not None else _default_data_root()
    return root / "runtime" / "daily_kline_coverage" / "daily_pipeline" / _to_iso_day(trade_date) / "report.json"


def default_formal_journal_path(trade_date: str, data_root: Optional[Path] = None) -> Path:
    """--formal-write 默认 journal 路径：<data_root>/runtime/daily_kline_coverage/daily_pipeline/<date>/formal_write_journal.jsonl。"""
    root = Path(data_root) if data_root is not None else _default_data_root()
    return root / "runtime" / "daily_kline_coverage" / "daily_pipeline" / _to_iso_day(trade_date) / "formal_write_journal.jsonl"


# ── ClickHouse cityHash64 的纯 Python 复刻（仅本模块 id 计算，禁他用） ──
# 语义（ClickHouse 24.8，contrib/cityhash102 = CityHash v1.0.2）：
#   cityHash64(str, pod, ...)：String 用 CityHash64 v1.0.2；数值/Date 用
#   intHash64(bit_cast<UInt64>(value))（MurmurHash3 fmix64 终结器 + 种子
#   0x4CF2D2BAAE6DA887）；多参按左折叠 Hash128to64(running, next) 合并。
# 正确性锚点：tests/test_daily_kline_pipeline.py 用 ClickHouse 官方文档
# groupBitXor(cityHash64(*)) 金样本（users 表 = 11639977218258521182）校验；
# 生产路径另有锁内 cityHash64 服务端探针双保险（不符即 fail-closed 不写）。
_U64_MASK = (1 << 64) - 1
_CITY_K0 = 0xc3a5c85c97cb3127
_CITY_K1 = 0xb492b66fbe98f273
_CITY_K2 = 0x9ae16a3b2f90404f
_CITY_K3 = 0xc949d7c7509e6557
_INT_HASH64_SEED = 0x4CF2D2BAAE6DA887
_EPOCH_DAY = date(1970, 1, 1)


def _u64(value: int) -> int:
    return value & _U64_MASK


def _fetch64(data: bytes, offset: int = 0) -> int:
    return int.from_bytes(data[offset:offset + 8], "little")


def _fetch32(data: bytes, offset: int = 0) -> int:
    return int.from_bytes(data[offset:offset + 4], "little")


def _rotate(value: int, shift: int) -> int:
    if shift == 0:
        return _u64(value)
    return _u64((value >> shift) | (value << (64 - shift)))


def _shift_mix(value: int) -> int:
    return _u64(value ^ (value >> 47))


def hash128to64(low: int, high: int) -> int:
    """CityHash Hash128to64：128 位折叠到 64 位（ClickHouse 多参合并器）。"""
    k_mul = 0x9DDFEA08EB382D69
    a = _u64((low ^ high) * k_mul)
    a ^= a >> 47
    b = _u64((high ^ a) * k_mul)
    b ^= b >> 47
    return _u64(b * k_mul)


def _hash_len16(u: int, v: int) -> int:
    return hash128to64(u, v)


def _hash_len0to16(data: bytes, length: int) -> int:
    if length > 8:
        a = _fetch64(data)
        b = _fetch64(data, length - 8)
        return _hash_len16(a, _rotate(_u64(b + length), length)) ^ b
    if length >= 4:
        a = _fetch32(data)
        return _hash_len16(_u64(length + (a << 3)), _fetch32(data, length - 4))
    if length > 0:
        a, b, c = data[0], data[length >> 1], data[length - 1]
        y = _u64(a + (b << 8))
        z = _u64(length + (c << 2))
        return _u64(_shift_mix(_u64(y * _CITY_K2 ^ z * _CITY_K3)) * _CITY_K2)
    return _CITY_K2


def _hash_len17to32(data: bytes, length: int) -> int:
    a = _u64(_fetch64(data) * _CITY_K1)
    b = _fetch64(data, 8)
    c = _u64(_fetch64(data, length - 8) * _CITY_K2)
    d = _u64(_fetch64(data, length - 16) * _CITY_K0)
    return _hash_len16(_u64(_rotate(_u64(a - b), 43) + _rotate(c, 30) + d),
                       _u64(a + _rotate(b ^ _CITY_K3, 20) - c + length))


def _weak_hash_len32_with_seeds(w: int, x: int, y: int, z: int, a: int, b: int) -> tuple[int, int]:
    a = _u64(a + w)
    b = _rotate(_u64(b + a + z), 21)
    c = a
    a = _u64(a + x)
    a = _u64(a + y)
    b = _u64(b + _rotate(a, 44))
    return _u64(a + z), _u64(b + c)


def _weak_hash_len32_with_seeds_bytes(data: bytes, offset: int, a: int, b: int) -> tuple[int, int]:
    return _weak_hash_len32_with_seeds(
        _fetch64(data, offset), _fetch64(data, offset + 8),
        _fetch64(data, offset + 16), _fetch64(data, offset + 24), a, b)


def _hash_len33to64(data: bytes, length: int) -> int:
    z = _fetch64(data, 24)
    a = _u64(_fetch64(data) + _u64((length + _fetch64(data, length - 16)) * _CITY_K0))
    b = _rotate(_u64(a + z), 52)
    c = _rotate(a, 37)
    a = _u64(a + _fetch64(data, 8))
    c = _u64(c + _rotate(a, 7))
    a = _u64(a + _fetch64(data, 16))
    vf = _u64(a + z)
    vs = _u64(b + _rotate(a, 31) + c)
    a = _u64(_fetch64(data, 16) + _fetch64(data, length - 32))
    z = _fetch64(data, length - 8)
    b = _rotate(_u64(a + z), 52)
    c = _rotate(a, 37)
    a = _u64(a + _fetch64(data, length - 24))
    c = _u64(c + _rotate(a, 7))
    a = _u64(a + _fetch64(data, length - 16))
    wf = _u64(a + z)
    ws = _u64(b + _rotate(a, 31) + c)
    r = _shift_mix(_u64((vf + ws) * _CITY_K2 + (wf + vs) * _CITY_K0))
    return _u64(_shift_mix(_u64(r * _CITY_K0 + vs)) * _CITY_K2)


def city_hash64(data: bytes) -> int:
    """Google CityHash v1.0.2 的 CityHash64（ClickHouse 24.8 cityHash64 的字符串分支）。"""
    length = len(data)
    if length <= 32:
        if length <= 16:
            return _hash_len0to16(data, length)
        return _hash_len17to32(data, length)
    if length <= 64:
        return _hash_len33to64(data, length)
    x = _fetch64(data)
    y = _fetch64(data, length - 16) ^ _CITY_K1
    z = _fetch64(data, length - 56) ^ _CITY_K0
    v1, v2 = _weak_hash_len32_with_seeds_bytes(data, length - 64, length, y)
    w1, w2 = _weak_hash_len32_with_seeds_bytes(data, length - 32, _u64(length * _CITY_K1), _CITY_K0)
    z = _u64(z + _u64(_shift_mix(v2) * _CITY_K1))
    x = _u64(_rotate(_u64(z + x), 39) * _CITY_K1)
    y = _u64(_rotate(y, 33) * _CITY_K1)
    remaining = (length - 1) & ~63
    offset = 0
    while remaining:
        x = _u64(_rotate(_u64(x + y + v1 + _fetch64(data, offset + 16)), 37) * _CITY_K1)
        y = _u64(_rotate(_u64(y + v2 + _fetch64(data, offset + 48)), 42) * _CITY_K1)
        x ^= w2
        y ^= v1
        z = _rotate(z ^ w1, 33)
        v1, v2 = _weak_hash_len32_with_seeds_bytes(data, offset, _u64(v2 * _CITY_K1), _u64(x + w1))
        w1, w2 = _weak_hash_len32_with_seeds_bytes(data, offset + 32, _u64(z + w2), y)
        z, x = x, z
        offset += 64
        remaining -= 64
    return _hash_len16(_u64(_hash_len16(v1, w1) + _u64(_shift_mix(y) * _CITY_K1) + z),
                       _u64(_hash_len16(v2, w2) + x))


def clickhouse_int_hash64(value: int) -> int:
    """ClickHouse IntHash64Impl（cityHash64 的 POD/数值/Date 分支）：fmix64(value ^ seed)。"""
    x = _u64(value) ^ _INT_HASH64_SEED
    x ^= x >> 33
    x = _u64(x * 0xFF51AFD7ED558CCD)
    x ^= x >> 33
    x = _u64(x * 0xC4CEB9FE1A85EC53)
    x ^= x >> 33
    return _u64(x)


def formal_row_id(code: str, trade_date: str) -> int:
    """正式表确定性 id = ClickHouse `cityHash64(code, toDate(trade_date))`。

    String 分支 → CityHash64 v1.0.2；Date 分支 → intHash64(自纪元天数)；
    两参按 Hash128to64(字符串哈希, 日期哈希) 合并（与迁移统一重生的 id 完全一致）。
    """
    day = _to_iso_day(trade_date)
    days = (date.fromisoformat(day) - _EPOCH_DAY).days
    return hash128to64(city_hash64(str(code).encode("utf-8")), clickhouse_int_hash64(days))


def _default_data_root() -> Path:
    try:
        from utils.paths import data_root as _data_root_fn
        return _data_root_fn()
    except Exception:
        return Path(r"F:\AiStock\data\data")


# ── CLI ──────────────────────────────────────────────────────────────
def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"应为非负整数: {text!r}") from exc
    if value < 0:
        raise argparse.ArgumentTypeError(f"应为非负整数: {text!r}")
    return value


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", default=None,
                        help="交易日（YYYY-MM-DD 或 YYYYMMDD）；省略时用正式表只读+日历解析最近已完成交易日")
    parser.add_argument("--source", choices=["pkg", "local"], default="pkg",
                        help="主数据源：pkg=通达信官网盘后包（默认，保留为回退源）；"
                             "local=客户端本地 vipdoc lday 文件（Owner 2026-11-13 起主源）")
    parser.add_argument("--tdx-root", default=DEFAULT_TDX_VIPDOC_ROOT, type=Path,
                        help=f"local 源的 vipdoc 根目录（默认 {DEFAULT_TDX_VIPDOC_ROOT}）")
    parser.add_argument("--min-fresh-files", type=_non_negative_int,
                        default=DEFAULT_MIN_FRESH_LDAY_FILES,
                        help="local 源时序门禁：mtime≥16:00 且含当日记录的文件数下限")
    parser.add_argument("--coverage-min", type=_non_negative_int,
                        default=DEFAULT_COVERAGE_MIN,
                        help="local 源覆盖率门禁：写入后正式表该日行数下限")
    parser.add_argument("--crosscheck-sample", type=_non_negative_int, default=DEFAULT_CROSSCHECK_SAMPLE,
                        help=f"腾讯交叉验证样本数（默认 {DEFAULT_CROSSCHECK_SAMPLE}，0=关闭）")
    parser.add_argument("--stage-write", action="store_true", default=False,
                        help="显式开关：全部门禁通过后写 stage 白名单表（默认绝不写任何表）")
    parser.add_argument("--formal-write", action="store_true", default=False,
                        help="显式开关：全部门禁通过后持 DbWriteLock 向正式表 stock.kline_daily 只插缺失键"
                             "（与 --stage-write 互斥）")
    parser.add_argument("--output", default=None, type=Path, help="报告路径（默认 "
                        "<data_root>/runtime/daily_kline_coverage/daily_pipeline/<date>/report.json）")
    args = parser.parse_args(argv)
    if args.stage_write and args.formal_write:
        parser.error("--formal-write 与 --stage-write 互斥，同一次运行只能选择一种写入模式")
    return args


# ── 离线门禁 ─────────────────────────────────────────────────────────
def check_ohlc_envelope(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """OHLC 包络：价格有限且非负，low <= open/close <= high。violation 明细进拒绝清单。"""
    violations = []
    for row in rows:
        open_, high, low, close = row["open"], row["high"], row["low"], row["close"]
        problems = []
        for name, value in (("open", open_), ("high", high), ("low", low), ("close", close)):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                problems.append(f"{name} 非有限数:{value!r}")
            elif value < 0:
                problems.append(f"{name} 为负:{value!r}")
        if not problems:
            if low > min(open_, close):
                problems.append(f"low {low} > min(open,close) {min(open_, close)}")
            if high < max(open_, close):
                problems.append(f"high {high} < max(open,close) {max(open_, close)}")
        if problems:
            violations.append({"code": row.get("code"), "date": row.get("date"), "problems": problems})
    return {
        "status": "fail" if violations else "pass",
        "checked": len(rows),
        "violations": violations[:200],
        "violation_count": len(violations),
    }


def check_non_negative_volume_amount(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """量额非负（volume 手 / amount 元；过滤后 volume>0，此处防御量纲破坏）。"""
    violations = []
    for row in rows:
        problems = []
        for field in ("volume", "amount"):
            value = row.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                problems.append(f"{field} 非有限数:{value!r}")
            elif value < 0:
                problems.append(f"{field} 为负:{value!r}")
        if problems:
            violations.append({"code": row.get("code"), "date": row.get("date"), "problems": problems})
    return {
        "status": "fail" if violations else "pass",
        "checked": len(rows),
        "violations": violations[:200],
        "violation_count": len(violations),
    }


def check_neeq_policy(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """NEEQ 安全码策略拒收：股票池出现三/老三板代码即门禁失败（防御纵深）。"""
    blocked = sorted({str(row["code"]) for row in rows if is_neeq_security_code(row.get("code"))})
    return {
        "status": "fail" if blocked else "pass",
        "checked": len(rows),
        "blocked_codes": blocked[:200],
        "blocked_count": len(blocked),
        "policy": "utils.security_market_policy.is_neeq_security_code",
    }


def check_duplicate_keys(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """重复键零容忍：(code, date) 出现多次即失败。"""
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (str(row.get("code")), str(row.get("date")))
        counts[key] = counts.get(key, 0) + 1
    duplicates = [{"code": code, "date": day, "count": count}
                  for (code, day), count in sorted(counts.items()) if count > 1]
    return {
        "status": "fail" if duplicates else "pass",
        "checked": len(rows),
        "duplicates": duplicates[:200],
        "duplicate_count": len(duplicates),
    }


# ── R17（2026-11-09）：冻结大盘指数纳入写入（同源 TDX 官网包）；R18 同日移除 000985.SH ──
# Owner 批准的冻结清单（任务书 task_briefs/20261109_daily_index_bars_r17.md；R18=
# 20261109_index_000985_removal_r18.md 移除 000985.SH 中证全指，11→10 码），禁止扩大
# 集合（597 清单含大量行业指数，写入会改变前端展示面）。指数 volume 包内
# 原始即「手」（2026-10-21/11-09 双日实测：与 kline_daily QMT 末代行、腾讯日线手数
# 逐码一致，比例=1.0000），不做 ÷100 —— 个股路径才是 股÷100→手，量纲不同（这也是
# tdx_official_package.normalize_to_contract 拒收指数行的原因）。包内缺码如实上报
# （官网包从不含 000985.SH 中证全指——R18 起该码也不在清单内），绝不硬造。
FROZEN_INDEX_CODES = frozenset({
    "000001.SH", "399001.SZ", "399106.SZ", "000300.SH", "000016.SH",
    "000905.SH", "000852.SH", "399006.SZ", "000680.SH", "000688.SH",
})


def extract_frozen_index_rows(raw_rows: Sequence[Mapping[str, Any]]) -> tuple[list, dict]:
    """从 parse_zip_bytes 的全证券行里提取冻结大盘指数行（R17；R18 起为 10 码）。

    仅 category=='index' 且全码 ∈ FROZEN_INDEX_CODES 且 volume>0 的行被提取；其余
    index（含 597 清单内的行业指数）与 tdx_internal_index 仍排除（filter_stock_universe
    照常逐类计数）。返回 (index_rows, report)：report 含 kept/kept_codes/zero_volume/
    missing_codes，进 row_counts.index_inclusion 供报告与只读回验；缺码不是错误。
    """
    kept: list[Mapping[str, Any]] = []
    kept_codes: set[str] = set()
    zero_volume = 0
    for row in raw_rows:
        market, code = row.get("market"), row.get("code")
        if tdx.classify_code(market, code) != "index":
            continue
        full_code = f"{str(code)}.{str(market).upper()}"
        if full_code not in FROZEN_INDEX_CODES:
            continue
        volume = row["volume"]
        if not isinstance(volume, (int, float)) or isinstance(volume, bool):
            raise RuntimeError(f"指数行 {full_code} 的 volume 不是数值: {volume!r}")
        if volume > 0:
            kept.append(row)
            kept_codes.add(full_code)
        else:
            zero_volume += 1               # 防御量纲破坏：有价无量的指数不写，如实计数
    report = {
        "frozen_code_count": len(FROZEN_INDEX_CODES),
        "kept": len(kept),
        "kept_codes": sorted(kept_codes),
        "zero_volume": zero_volume,
        "missing_codes": sorted(FROZEN_INDEX_CODES - kept_codes),
    }
    return kept, report


def frozen_index_rows_to_contract(rows: Sequence[Mapping[str, Any]]) -> list[dict]:
    """冻结清单内指数行 → 契约行（字段与 normalize_to_contract 输出同构）。

    fail-closed：收到清单外指数行（行业指数/其他类别）直接抛错，写路径只接受冻结
    清单（R18 起 10 码）。volume 保持包内原始「手」（volume_divisor=1）；amount 元（2 位小数）。
    """
    out_rows: list[dict[str, Any]] = []
    for row in rows:
        full_code = f'{row["code"]}.{row["market"].upper()}'
        if full_code not in FROZEN_INDEX_CODES:
            raise RuntimeError(f"frozen_index_rows_to_contract 收到冻结清单外的指数行 {full_code}，"
                               f"写路径只接受冻结大盘指数集（R18 起 10 码）")
        raw_volume = row["volume"]
        if not isinstance(raw_volume, (int, float)) or isinstance(raw_volume, bool):
            raise RuntimeError(f"指数行 {full_code} 的 volume 不是数值: {raw_volume!r}")
        out_rows.append({
            "code": full_code,
            "market": row["market"],
            "date": row["date"],
            "name": row["name"],
            "prev_close": row["prev_close"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": round(float(raw_volume), 2),
            "volume_unit": "lots",
            "volume_raw": raw_volume,
            "volume_raw_unit": "lots",
            "volume_divisor": 1,
            "amount": round(float(row["amount"]), 2),
            "amount_unit": "CNY",
            "amount_available": True,
        })
    return out_rows


# ── 报告与执行 ───────────────────────────────────────────────────────
def _skipped_gate(reason: str, **extra: Any) -> dict[str, Any]:
    result = {"status": "skipped", "reason": reason}
    result.update(extra)
    return result


def _collect_rejections(gates: Mapping[str, Any]) -> list[dict[str, Any]]:
    """拒绝清单：所有门禁的违例条目汇总（NEEQ / 包络 / 量额 / 重复键 / 重叠不符 / 交叉不符）。"""
    rejections: list[dict[str, Any]] = []
    for gate_name, gate in gates.items():
        if gate.get("status") != "fail":
            continue
        for key in ("violations", "duplicates", "blocked_codes", "new_codes", "disappeared_codes",
                    "mismatches", "sample_mismatches"):
            for item in gate.get(key) or []:
                rejections.append({"gate": gate_name, "item": item})
    return rejections


def run_pipeline(options: Mapping[str, Any]) -> dict[str, Any]:
    """执行一次日缎管道。options 键见 tests；不认识的键忽略。

    返回 {exit_code, report, report_path, rows}；任何异常都转为 status=error / 退出码 2。
    """
    opts = dict(options)
    now = opts.get("now") or datetime.now(BEIJING_TZ)
    fetcher = opts.get("fetch_package")
    min_priced = opts.get("min_priced", None)
    client_factory = opts.get("client_factory")
    crosscheck_fetch = opts.get("crosscheck_fetch")

    report: dict[str, Any] = {
        "pipeline_version": PIPELINE_VERSION,
        "status": "error",
        "trade_date": None,
        "source": opts.get("source") or "pkg",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_access": False,
        "lineage": None,
        "row_counts": None,
        "units": {"volume": "lots", "amount": "CNY"},
        "gates": {name: _skipped_gate("未执行") for name in GATE_NAMES},
        "rejections": [],
        "stage_write": {"performed": False},
        "formal_write": {"performed": False},
        "crosscheck": None,
        "error": None,
    }
    db: Optional[Any] = None

    def engage_db() -> Any:
        nonlocal db
        if db is None:
            factory = client_factory or _default_client_factory()
            db = factory()
            report["db_access"] = True
        return db

    try:
        trade_date = _resolve_trade_date(opts, engage_db, report)
        report["trade_date"] = trade_date
        output_path = Path(opts["output"]) if opts.get("output") else default_output_path(trade_date)

        source_mode = str(opts.get("source") or "pkg")
        local_gates: dict[str, Any] = {}
        if source_mode == "local":
            # ── Owner 2026-11-13 裁决主源：客户端本地 vipdoc lday（官网包保留为回退）──
            # local 模式默认接入 DB（只读）：量纲对照/前日对比/重叠一致性门禁需要
            engage_db()
            tdx_root = str(opts.get("tdx_root") or DEFAULT_TDX_VIPDOC_ROOT)
            scan = lday.scan_lday_universe(tdx_root, trade_date)
            timing = scan["timing"]
            rows = scan["rows"]
            fetched_at = datetime.now(timezone.utc).isoformat()
            lineage = {
                "source": "tdx_local_lday",
                "url": f"file://{tdx_root}/{{sh,sz,bj}}/lday/*.day",
                "zip_sha256": scan["digest"],
                "fetched_at": fetched_at,
                "row_count": len(rows),
                "raw_row_count": int(timing["universe_files"]),
                "tdx_root": tdx_root,
                "fresh_deadline": scan["fresh_deadline"],
            }
            report["source"] = "local"
            report["row_counts"] = {
                "kept": len(rows),
                "total_rows": int(timing["universe_files"]),
                "timing": timing,
                "index_inclusion": {
                    "frozen_code_count": len(lday._import_universe_rules()[1]),
                    "kept": sum(1 for r in rows if r.get("is_index")),
                    "kept_codes": sorted(r["code"] for r in rows if r.get("is_index")),
                    "zero_volume": int(timing["zero_volume"]),
                },
            }
            # 门禁（fail-closed）A：时序证据——mtime≥16:00 且含当日记录的文件数体量
            min_fresh = int(opts.get("min_fresh_files") or DEFAULT_MIN_FRESH_LDAY_FILES)
            fresh = int(timing["fresh"])
            timing_ok = fresh > 0 and fresh >= min_fresh
            local_gates["lday_timing"] = {
                "status": "pass" if timing_ok else "fail",
                "fresh_files": fresh,
                "min_fresh_files": min_fresh,
                "has_today": int(timing["has_today"]),
                "stale_mtime_excluded": int(timing["stale_mtime_excluded"]),
                "no_today_record": int(timing["no_today_record"]),
                "zero_volume": int(timing["zero_volume"]),
                "read_errors": timing["read_errors"][:20],
                "fresh_deadline": scan["fresh_deadline"],
                "reason": None if timing_ok else (
                    f"lday 盘后下载完成证据不足（fresh={fresh} < {min_fresh}），"
                    f"fail-closed 拒绝写入并报警——防止拿昨日/盘中文件写今日"),
            }
            # 门禁 B：vwap 反推量纲抽样（vol 单位：股÷100→手 / 指数原值）
            local_gates["lday_vwap_unit"] = lday.vwap_unit_check(rows)
        else:
            # 1) 拉盘后包（适配层，注入 fetcher 时不联网）
            fetch_result = tdx.fetch_daily_package(trade_date, fetcher=fetcher)
            lineage = {
                "source": "tdx_official_package",
                "url": fetch_result["url"],
                "zip_sha256": fetch_result["zip_sha256"],
                "fetched_at": fetch_result["fetched_at"],
                "size_bytes": fetch_result["size"],
                "attempts": fetch_result["attempts"],
            }

            # 2) 解析 → 股票池过滤 → 归一化（volume 股→手、amount 元）
            #    R17：清单内冻结 11 码大盘指数一并纳入（指数 volume 包内原始即手，不 ÷100）
            parse_kwargs = {} if min_priced is None else {"min_priced": min_priced}
            raw_rows = tdx.parse_zip_bytes(fetch_result["content"], trade_date, **parse_kwargs)
            kept, filter_report = tdx.filter_stock_universe(raw_rows)
            index_raw_rows, index_report = extract_frozen_index_rows(raw_rows)
            contract = tdx.normalize_to_contract(
                kept, lineage={"url": fetch_result["url"], "fetched_at": fetch_result["fetched_at"],
                               "zip_sha256": fetch_result["zip_sha256"]})
            rows = contract["rows"] + frozen_index_rows_to_contract(index_raw_rows)
            lineage["row_count"] = len(rows)
            lineage["raw_row_count"] = contract["lineage"]["raw_row_count"]
            report["lineage"] = lineage
            filter_report["index_inclusion"] = index_report
            report["row_counts"] = filter_report
        report["lineage"] = report.get("lineage") or lineage

        # 3) 门禁：离线四门禁 + （DB 已接入时的）正式表对比门禁 + 可选腾讯交叉
        # --stage-write / --formal-write 要求全部门禁（含 DB 门禁）通过才允许写 → 先接入 DB
        stage_write_requested = bool(opts.get("stage_write"))
        formal_write_requested = bool(opts.get("formal_write"))
        if stage_write_requested or formal_write_requested:
            engage_db()
        gates = report["gates"]
        gates["ohlc_envelope"] = check_ohlc_envelope(rows)
        gates["non_negative_volume_amount"] = check_non_negative_volume_amount(rows)
        gates["neeq_policy"] = check_neeq_policy(rows)
        gates["duplicate_keys"] = check_duplicate_keys(rows)
        db_engaged = report["db_access"]
        if db_engaged:
            gates["row_count_vs_prev_day"] = _run_prev_day_gate(db, rows, trade_date)
            gates["formal_overlap_consistency"] = check_formal_overlap(
                rows, formal_rows=_read_formal_overlap_rows(db, trade_date))
        else:
            gates["row_count_vs_prev_day"] = _skipped_gate(
                "默认模式零数据库访问：未接入 DB/正式表，无法与前一交易日对比（--stage-write 或省略 --trade-date 可启用）")
            gates["formal_overlap_consistency"] = _skipped_gate(
                "默认模式零数据库访问：未接入 DB/正式表，无法与已有重叠键比对（--stage-write 或省略 --trade-date 可启用）")
        # 腾讯交叉样本验证（网络源，不依赖 DB；默认模式也可启用）
        sample_n = int(opts.get("crosscheck_sample") or 0)
        if sample_n <= 0:
            gates["tencent_crosscheck"] = _skipped_gate("crosscheck_sample=0，腾讯交叉验证关闭")
            report["crosscheck"] = {"sample_size": 0, "sampled_codes": []}
        else:
            sample_codes = select_crosscheck_codes(rows, sample_n, trade_date)
            report["crosscheck"] = {"sample_size": len(sample_codes), "sampled_codes": sample_codes}
            if not sample_codes:
                gates["tencent_crosscheck"] = _skipped_gate("包内没有可交叉验证的沪深代码")
            else:
                fetch = crosscheck_fetch or _default_crosscheck_fetch
                gates["tencent_crosscheck"] = check_tencent_crosscheck(rows, sample_codes, fetch, trade_date)

        # local（lday）源附加门禁：时序证据 + vwap 量纲（B3/B5，fail-closed）
        if local_gates:
            gates.update(local_gates)
        # B5 全量对照报告落盘：local 行 vs 正式表该日已有行（官网包对照，价1%/量额3%）
        if source_mode == "local" and db_engaged:
            formal_rows_for_stats = _read_formal_overlap_rows(db, trade_date)
            if formal_rows_for_stats:
                stats = _local_vs_formal_stats(rows, formal_rows_for_stats, trade_date)
                stats["formal_overlap_gate"] = gates["formal_overlap_consistency"]["status"]
                stats["report_path"] = str(_write_lday_runtime_json(
                    f"local_vs_pkg_{trade_date.replace('-', '')}", stats,
                    runtime_dir=opts.get("runtime_dir")))
                report["local_vs_formal"] = {
                    key: value for key, value in stats.items()
                    if key not in ("violations", "worst_rel_diff_by_field")}
                report["local_vs_formal"]["worst_rel_diff_by_field"] = stats["worst_rel_diff_by_field"]
            else:
                report["local_vs_formal"] = {
                    "compared": 0,
                    "note": "正式表该日尚无行（首写场景），全量对照不适用；量纲由 vwap 门禁裁决",
                }

        report["rejections"] = _collect_rejections(gates)
        failed = [name for name, gate in gates.items() if gate.get("status") == "fail"]
        report["failed_gates"] = failed
        # --stage-write：仅当全部门禁通过才建表/插入/读回验证；任何失败都不写 stage
        if stage_write_requested:
            if failed:
                report["stage_write"] = {"performed": False, "rolled_back": False,
                                         "table": STAGE_TABLE,
                                         "reason": f"门禁未全部通过 {failed}，不写 stage"}
            else:
                report["stage_write"] = _stage_write_flow(db, rows, trade_date, lineage)
                if not report["stage_write"].get("performed"):
                    failed = failed + ["stage_write_readback_verification"]
                    report["failed_gates"] = failed
        # --formal-write：仅当全部门禁通过才持锁反连接+插入正式表+读回验证（不符即回滚）
        if formal_write_requested:
            if failed:
                report["formal_write"] = {"performed": False, "rolled_back": False,
                                          "table": KLINE_DAILY_TABLE,
                                          "reason": f"门禁未全部通过 {failed}，不写正式表"}
            else:
                report["formal_write"] = _formal_write_flow(db, rows, trade_date, lineage,
                                                            output_path, opts)
                if not report["formal_write"].get("performed"):
                    failed = failed + ["formal_write"]
                    report["failed_gates"] = failed
        # B4 覆盖率门禁（local 源）：写入后正式表该日行数 ≥ coverage_min，
        # 与 lc5（fzline）宇宙覆盖缺口一并落 runtime 报告；不足即整体失败报警
        # （调度器据此回退官网包——exact-key 幂等，回退不会重复写）。
        if source_mode == "local" and report["db_access"]:
            coverage = _local_coverage_report(db, rows, trade_date, opts)
            report["local_coverage"] = coverage
            if formal_write_requested:
                coverage_ok = int(coverage["postwrite_row_count"]) >= int(coverage["coverage_min"])
                gates["postwrite_coverage"] = {
                    "status": "pass" if coverage_ok else "fail",
                    "postwrite_row_count": coverage["postwrite_row_count"],
                    "coverage_min": coverage["coverage_min"],
                    "lc5_not_in_daily_count": coverage["lc5_not_in_daily_count"],
                    "report_path": coverage["report_path"],
                    "reason": None if coverage_ok else (
                        f"写入后该日行数 {coverage['postwrite_row_count']} < "
                        f"{coverage['coverage_min']}，覆盖率门禁失败报警"),
                }
                if not coverage_ok:
                    report["rejections"].append({"gate": "postwrite_coverage",
                                                 "item": gates["postwrite_coverage"]})
                    failed = failed + ["postwrite_coverage"]
                    report["failed_gates"] = failed
        report["status"] = "failed" if failed else "passed"

        _atomic_json(output_path, report)
        return {"exit_code": 0 if not failed else 1, "report": report,
                "report_path": output_path, "rows": rows}
    except Exception as exc:  # noqa: BLE001 - 管道顶层必须把一切异常转为报告
        report["status"] = "error"
        report["error"] = f"{type(exc).__name__}: {exc}"
        output_path = _safe_output_path(opts)
        try:
            _atomic_json(output_path, report)
        except Exception:  # noqa: BLE001 - 报告写不进去也不能掩盖原始错误
            pass
        return {"exit_code": 2, "report": report, "report_path": output_path, "rows": []}


def _default_client_factory() -> Callable[[], Any]:
    def factory() -> Any:
        from utils.market_warehouse import clickhouse_client
        return clickhouse_client()
    return factory


def _resolve_trade_date(opts: Mapping[str, Any], engage_db: Callable[[], Any],
                        report: dict[str, Any]) -> str:
    explicit = opts.get("trade_date")
    if explicit:
        return _to_iso_day(explicit)
    # 省略 --trade-date：正式表只读查最近有行日期 + 日历判断（必须有 DB）
    resolution = resolve_latest_completed_trade_date(engage_db(), now=opts.get("now"))
    report["trade_date_resolution"] = resolution
    return resolution["trade_date"]


def _safe_output_path(opts: Mapping[str, Any]) -> Path:
    if opts.get("output"):
        return Path(opts["output"])
    if opts.get("trade_date"):
        return default_output_path(opts["trade_date"])
    stamp = datetime.now(BEIJING_TZ).strftime("%Y%m%d_%H%M%S")
    return _default_data_root() / "runtime" / "daily_kline_coverage" / "daily_pipeline" / "_unresolved" / f"report_{stamp}.json"


def _read_only_rows(client: Any, sql: str) -> list[dict[str, Any]]:
    """正式表/系统表只读查询：一律 SETTINGS readonly=1，返回 dict 行。"""
    result = client.query(sql, settings=dict(READONLY_SETTINGS))
    names = list(result.column_names)
    return [dict(zip(names, row)) for row in result.result_rows]


def _read_calendar_sessions(client: Any, upto_day: str, limit: int = 5) -> list[str]:
    """日历里 <= upto_day 的最近几个交易日（降序 ISO 列表）。"""
    rows = _read_only_rows(
        client,
        "SELECT trade_date FROM stock.trade_calendar FINAL "
        f"WHERE market='SH' AND is_trading=1 AND trade_date <= toDate('{upto_day}') "
        f"ORDER BY trade_date DESC LIMIT {int(limit)}")
    return [str(_to_iso_day(row["trade_date"])) for row in rows]


def resolve_latest_completed_trade_date(client: Any, now: Optional[datetime] = None) -> dict[str, Any]:
    """正式表只读查最近有行日期 + trade_calendar 日历判断 → 最近已完成交易日。

    - 北京时间 >= 16:10（盘后包发布时刻）当日会话才算"已完成"，否则回退前一自然日；
    - 日历最近会话 target 与正式表最近有行日期 cross-check：正式表超前于日历 → 报错；
    - target == 正式表最近有行日期 → 同日重复拉取场景（重叠一致性门禁接管）。
    """
    moment = now or datetime.now(BEIJING_TZ)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BEIJING_TZ)
    else:
        moment = moment.astimezone(BEIJING_TZ)
    candidate = moment.date() if moment.time() >= PACKAGE_PUBLISH_CUTOFF else moment.date() - timedelta(days=1)
    candidate_iso = candidate.isoformat()

    window_start = (candidate - timedelta(days=45)).isoformat()
    latest_rows = _read_only_rows(
        client,
        f"SELECT max(trade_date) AS latest FROM {KLINE_DAILY_TABLE} "
        f"WHERE trade_date >= toDate('{window_start}')")
    formal_latest = latest_rows[0]["latest"] if latest_rows else None
    formal_latest_iso = _to_iso_day(formal_latest) if formal_latest else None

    sessions_desc = _read_calendar_sessions(client, candidate_iso)
    if not sessions_desc:
        raise RuntimeError(
            f"trade_calendar 里找不到 <= {candidate_iso} 的交易日，无法解析最近已完成交易日；请显式传 --trade-date")
    target = sessions_desc[0]
    if formal_latest_iso and formal_latest_iso > target:
        raise RuntimeError(
            f"正式表最近有行日期 {formal_latest_iso} 超前于日历最近已完成交易日 {target}，数据状态可疑，拒绝猜测")
    return {
        "trade_date": target,
        "candidate_day": candidate_iso,
        "formal_latest": formal_latest_iso,
        "calendar_recent": sorted(sessions_desc),
        "beijing_now": moment.isoformat(),
        "publish_cutoff": PACKAGE_PUBLISH_CUTOFF.isoformat(timespec="minutes"),
        "same_day_repull": formal_latest_iso == target,
    }


def _read_prev_day_rows(client: Any, prev_date: str) -> list[dict[str, Any]]:
    """前一交易日正式表（FINAL 去重）的 code → 板块映射行。"""
    return _read_only_rows(
        client,
        "SELECT code, CASE WHEN code LIKE '%.SH' THEN 'sh' WHEN code LIKE '%.SZ' THEN 'sz' "
        "WHEN code LIKE '%.BJ' THEN 'bj' ELSE 'other' END AS market "
        f"FROM {KLINE_DAILY_TABLE} FINAL WHERE trade_date = toDate('{prev_date}')")


def _read_formal_overlap_rows(client: Any, trade_date: str) -> list[dict[str, Any]]:
    """正式表同日已有行（同日重复拉取场景的重叠键）。"""
    return _read_only_rows(
        client,
        f"SELECT code, open, high, low, close, volume, amount FROM {KLINE_DAILY_TABLE} FINAL "
        f"WHERE trade_date = toDate('{trade_date}') ORDER BY code")


def _read_stocks_master_best_effort(client: Any, codes: Sequence[str]) -> tuple[dict[str, dict[str, Any]], bool]:
    """股票主表 best-effort 查询（解释新股/退市）；主表不可用时返回 ({}, False)，解释降级 unknown。"""
    wanted = sorted(set(codes))
    if not wanted:
        return {}, True
    try:
        quoted = ",".join("'" + code.replace("'", "") + "'" for code in wanted)
        rows = _read_only_rows(
            client,
            "SELECT code, list_date, delist_date, quit, listing_status FROM stock.stocks FINAL "
            f"WHERE code IN ({quoted})")
        return {str(row["code"]): row for row in rows}, True
    except Exception:  # noqa: BLE001 - 解释信息 best-effort，绝不阻塞门禁
        return {}, False


def check_row_count_vs_prev_day(rows: Sequence[Mapping[str, Any]], *, prev_codes: Sequence[str],
                                prev_date: str, target_date: str,
                                stocks_by_code: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """各板块行数与前一交易日对比；新增/消失代码给清单与解释（无法区分时标 unknown）。"""
    markets = ("sh", "sz", "bj")
    counts = {m: 0 for m in markets}
    for row in rows:
        market = str(row.get("market") or "").lower()
        if market not in counts:
            market = str(row.get("code", "")).rsplit(".", 1)[-1].lower()
        if market in counts:
            counts[market] += 1
    prev_counts = {m: 0 for m in markets}
    for code in prev_codes:
        suffix = str(code).rsplit(".", 1)[-1].lower()
        if suffix in prev_counts:
            prev_counts[suffix] += 1

    if not prev_codes:
        return {
            "status": "fail",
            "reason": f"前一交易日 {prev_date} 正式表无行，无法对比各板块行数",
            "prev_date": prev_date, "target_date": target_date,
            "counts": counts, "prev_counts": prev_counts,
            "new_codes": [], "disappeared_codes": [], "violations": [],
        }

    def _explain_new(code: str) -> str:
        info = stocks_by_code.get(code)
        if info and info.get("list_date"):
            try:
                if _to_iso_day(info["list_date"]) == target_date:
                    return "new_listing"
            except ValueError:
                pass
        return "unknown"

    def _explain_disappeared(code: str) -> str:
        info = stocks_by_code.get(code)
        if info:
            delist = info.get("delist_date")
            if delist:
                try:
                    if _to_iso_day(delist) <= target_date:
                        return "delisted"
                except ValueError:
                    pass
            if int(info.get("quit") or 0) == 1:
                return "delisted_or_quit"
        return "unknown"

    current_codes = {str(row["code"]) for row in rows}
    previous_codes = set(map(str, prev_codes))
    new_codes = [{"code": code, "explanation": _explain_new(code)}
                 for code in sorted(current_codes - previous_codes)]
    disappeared_codes = [{"code": code, "explanation": _explain_disappeared(code)}
                         for code in sorted(previous_codes - current_codes)]

    violations = []
    for market in markets:
        prev, current = prev_counts[market], counts[market]
        limit = max(MAX_BOARD_COUNT_REL_CHANGE * prev, MAX_BOARD_COUNT_ABS_CHANGE)
        if prev == 0 and current > 0:
            violations.append({"market": market, "prev": prev, "current": current,
                               "reason": "前一交易日该板块无行而今日有行"})
        elif abs(current - prev) > limit:
            violations.append({"market": market, "prev": prev, "current": current,
                               "change": current - prev, "threshold": limit})
    return {
        "status": "fail" if violations else "pass",
        "prev_date": prev_date, "target_date": target_date,
        "counts": counts, "prev_counts": prev_counts,
        "new_codes": new_codes, "disappeared_codes": disappeared_codes,
        "violations": violations,
        "thresholds": {"max_relative_change": MAX_BOARD_COUNT_REL_CHANGE,
                       "max_absolute_change": MAX_BOARD_COUNT_ABS_CHANGE},
    }


def check_formal_overlap(rows: Sequence[Mapping[str, Any]], *, formal_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """与正式表已有重叠键值一致性：价 1% / 量额 3%（同日重复拉取场景）。"""
    price_fields = ("open", "high", "low", "close")
    quantity_fields = ("volume", "amount")
    formal_by_code = {str(row["code"]): row for row in formal_rows}
    mismatches = []
    overlap_count = 0
    for row in rows:
        code = str(row["code"])
        formal = formal_by_code.get(code)
        if formal is None:
            continue
        overlap_count += 1
        for field in price_fields:
            rel = _rel_diff(float(row[field]), float(formal[field]))
            if rel > PRICE_TOLERANCE:
                mismatches.append({"code": code, "field": field, "pkg_value": row[field],
                                   "formal_value": formal[field], "rel_diff": round(rel, 6),
                                   "tolerance": PRICE_TOLERANCE})
        for field in quantity_fields:
            rel = _rel_diff(float(row[field]), float(formal[field]))
            if rel > QUANTITY_TOLERANCE:
                mismatches.append({"code": code, "field": field, "pkg_value": row[field],
                                   "formal_value": formal[field], "rel_diff": round(rel, 6),
                                   "tolerance": QUANTITY_TOLERANCE})
    return {
        "status": "fail" if mismatches else "pass",
        "overlap_count": overlap_count,
        "mismatches": mismatches[:200],
        "mismatch_count": len(mismatches),
        "tolerances": {"price": PRICE_TOLERANCE, "quantity": QUANTITY_TOLERANCE},
    }


def select_crosscheck_codes(rows: Sequence[Mapping[str, Any]], sample_n: int,
                            trade_date: str) -> list[str]:
    """确定性抽样腾讯交叉验证代码：只沪/深（腾讯不支持北交所），seed=交易日。

    R17：冻结清单内指数不入样 —— 腾讯指数量口径与官网包存在系统性差异
    （399006.SZ 实测 raw/tencent=1.0439，超 3% 容差；而 kline_daily QMT 末代行
    与官网包逐码 1.0000，差异在腾讯侧而非包侧），入样会产生已知假阴性阻断当日
    写入。个股抽样池与既有行为完全一致。
    """
    eligible = sorted({str(row["code"]) for row in rows
                       if str(row.get("market") or str(row.get("code", "")).rsplit(".", 1)[-1]).lower()
                       in ("sh", "sz")
                       and str(row["code"]) not in FROZEN_INDEX_CODES})
    if not eligible or sample_n <= 0:
        return []
    k = min(int(sample_n), CROSSCHECK_SAMPLE_CAP, len(eligible))
    return sorted(random.Random(f"{trade_date}:tencent-crosscheck").sample(eligible, k))


def check_tencent_crosscheck(rows: Sequence[Mapping[str, Any]], sample_codes: Sequence[str],
                             fetch_row: Callable[[str, str], Optional[Mapping[str, Any]]],
                             trade_date: str) -> dict[str, Any]:
    """腾讯交叉样本验证：close 价 1% / volume 量 3%（腾讯无成交额，只交叉价量）。

    fetch_row(code, trade_date) → {close, volume, ...}（手）或 None（当日无行）；
    抛错计为 error。任一价值不符 → fail；全部样本取不到数据（error）→ fail。
    """
    rows_by_code = {str(row["code"]): row for row in rows}
    results, mismatches = [], []
    error_count = no_row_count = 0
    for code in sample_codes:
        pkg = rows_by_code.get(code)
        if pkg is None:
            continue
        try:
            fetched = fetch_row(code, trade_date)
        except Exception as exc:  # noqa: BLE001 - 单样本失败不中断其余样本
            error_count += 1
            results.append({"code": code, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
            continue
        if not fetched:
            no_row_count += 1
            results.append({"code": code, "status": "no_row"})
            continue
        close_rel = _rel_diff(float(pkg["close"]), float(fetched["close"]))
        volume_rel = _rel_diff(float(pkg["volume"]), float(fetched["volume"]))
        entry = {"code": code, "status": "match",
                 "pkg_close": pkg["close"], "tencent_close": fetched["close"],
                 "close_rel_diff": round(close_rel, 6),
                 "pkg_volume": pkg["volume"], "tencent_volume": fetched["volume"],
                 "volume_rel_diff": round(volume_rel, 6)}
        for field, rel, tolerance in (("close", close_rel, PRICE_TOLERANCE),
                                      ("volume", volume_rel, QUANTITY_TOLERANCE)):
            if rel > tolerance:
                entry["status"] = "mismatch"
                mismatches.append({"code": code, "field": field,
                                   "pkg_value": pkg[field], "tencent_value": fetched[field],
                                   "rel_diff": round(rel, 6), "tolerance": tolerance})
        results.append(entry)
    sample_size = len(sample_codes)
    unavailable = sample_size > 0 and error_count == sample_size
    return {
        "status": "fail" if (mismatches or unavailable) else "pass",
        "sample_size": sample_size,
        "sampled_codes": list(sample_codes),
        "results": results,
        "mismatches": mismatches[:200],
        "mismatch_count": len(mismatches),
        "error_count": error_count,
        "no_row_count": no_row_count,
        "tolerances": {"price": PRICE_TOLERANCE, "quantity": QUANTITY_TOLERANCE},
        **({} if not unavailable else {"reason": "腾讯交叉验证全部样本取数失败"}),
    }


def _stage_rows_for_insert(rows: Sequence[Mapping[str, Any]], trade_date: str,
                           lineage: Mapping[str, Any]) -> list[tuple]:
    """契约行 → stage 表插入元组（列序 = STAGE_COLUMNS；volume 手 / amount 元；血缘列逐行携带）。"""
    fetched_at = datetime.fromisoformat(str(lineage["fetched_at"]))
    fetched_bj = fetched_at.astimezone(BEIJING_TZ).replace(tzinfo=None, microsecond=0)
    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    day = date.fromisoformat(_to_iso_day(trade_date))
    out = []
    for row in rows:
        out.append((
            str(row["code"]), day, float(row["open"]), float(row["high"]), float(row["low"]),
            float(row["close"]), float(row["volume"]), float(row["amount"]),
            float(row.get("prev_close") or 0.0), str(row.get("name") or ""),
            str(row.get("market") or ""), str(lineage["url"]), str(lineage["zip_sha256"]),
            fetched_bj, PIPELINE_VERSION, created_at,
        ))
    return out


def _digest_line(row: Mapping[str, Any]) -> str:
    return (f'{row["code"]}|{float(row["open"]):.4f}|{float(row["high"]):.4f}|'
            f'{float(row["low"]):.4f}|{float(row["close"]):.4f}|'
            f'{float(row["volume"]):.2f}|{float(row["amount"]):.2f}')


def _stage_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    """逐日 digest：按 code 排序的规范化行 sha256（读回与插入侧同算法比对）。"""
    lines = sorted(_digest_line(row) for row in rows)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _stage_count(client: Any, trade_date: str) -> int:
    rows = _read_only_rows(
        client, f"SELECT count() AS row_count FROM {STAGE_TABLE} "
                f"WHERE trade_date = toDate('{trade_date}')")
    return int(rows[0]["row_count"]) if rows else 0


def _sample_indexes(length: int, cap: int = 5) -> list[int]:
    if length <= 0:
        return []
    if length <= cap:
        return list(range(length))
    step = (length - 1) / (cap - 1)
    return sorted({round(i * step) for i in range(cap)})


def _verify_stage_readback(client: Any, stage_rows: Sequence[tuple],
                           trade_date: str) -> dict[str, Any]:
    """读回验证：行数 + 逐日 digest + 抽样值（确定性抽样，逐字段比对）。"""
    expected_rows = [dict(zip(STAGE_COLUMNS, item)) for item in stage_rows]
    row_count = _stage_count(client, trade_date)
    readback_rows = _read_only_rows(
        client,
        f"SELECT {', '.join(STAGE_READBACK_COLUMNS)} FROM {STAGE_TABLE} "
        f"WHERE trade_date = toDate('{trade_date}') ORDER BY code")
    expected_digest = _stage_digest(expected_rows)
    readback_digest = _stage_digest(readback_rows)
    readback_by_code = {str(row["code"]): row for row in readback_rows}
    expected_codes = sorted(row["code"] for row in expected_rows)
    sample_mismatches = []
    for index in _sample_indexes(len(expected_codes)):
        code = expected_codes[index]
        want = next(row for row in expected_rows if row["code"] == code)
        got = readback_by_code.get(code)
        if got is None:
            sample_mismatches.append({"code": code, "problems": ["读回缺行"]})
            continue
        problems = []
        for field, fmt in (("code", None), ("open", ".4f"), ("high", ".4f"), ("low", ".4f"),
                           ("close", ".4f"), ("volume", ".2f"), ("amount", ".2f")):
            left, right = want[field], got[field]
            if fmt is not None:
                left, right = format(float(left), fmt), format(float(right), fmt)
            if str(left) != str(right):
                problems.append(f"{field}: 插入 {want[field]!r} != 读回 {got[field]!r}")
        for field in ("name", "market", "source_url", "zip_sha256", "pipeline_version"):
            if str(want.get(field)) != str(got.get(field)):
                problems.append(f"{field}: 插入 {want.get(field)!r} != 读回 {got.get(field)!r}")
        if problems:
            sample_mismatches.append({"code": code, "problems": problems})
    count_ok = row_count == len(stage_rows)
    digest_ok = readback_digest == expected_digest
    ok = count_ok and digest_ok and not sample_mismatches
    return {
        "ok": ok,
        "row_count": row_count,
        "expected_row_count": len(stage_rows),
        "row_count_ok": count_ok,
        "digest_match": digest_ok,
        "per_day_digest": {trade_date: readback_digest},
        "expected_digest": expected_digest,
        "readback_digest": readback_digest,
        "sample_checked": min(len(expected_codes), 5),
        "sample_mismatches": sample_mismatches[:50],
        "summary": ("" if ok else
                    f"row_count {row_count}/{len(stage_rows)}, digest_match {digest_ok}, "
                    f"sample_mismatches {len(sample_mismatches)}"),
    }


def _rollback_stage_date(client: Any, trade_date: str) -> dict[str, Any]:
    """回滚：删除该日 stage 行（mutation 同步等待），并验证删干净。"""
    assert_stage_table_allowed(STAGE_TABLE)
    sql = f"ALTER TABLE {STAGE_TABLE} DELETE WHERE trade_date = toDate('{trade_date}')"
    client.command(sql, settings={"mutations_sync": 2})
    remaining = _stage_count(client, trade_date)
    if remaining:
        raise RuntimeError(f"回滚后 {STAGE_TABLE} {trade_date} 仍有 {remaining} 行未删除")
    return {"sql": sql, "deleted_date": trade_date, "remaining_rows": remaining}


def _stage_write_flow(client: Any, rows: Sequence[Mapping[str, Any]], trade_date: str,
                      lineage: Mapping[str, Any]) -> dict[str, Any]:
    """--stage-write 主流程：白名单断言 → 残留检查 → 建表 → 插入 → 读回验证（不符即回滚）。"""
    assert_stage_table_allowed(STAGE_TABLE)
    result: dict[str, Any] = {"performed": False, "rolled_back": False, "table": STAGE_TABLE}
    existing = _stage_count(client, trade_date)
    if existing:
        result["reason"] = (f"stage 表 {STAGE_TABLE} 已有 {existing} 行 {trade_date} 数据，"
                            f"拒绝覆盖（请人工确认清理后重跑）")
        return result
    try:
        client.command(STAGE_DDL)
        result["ddl_issued"] = True
        stage_rows = _stage_rows_for_insert(rows, trade_date, lineage)
        assert_stage_table_allowed(STAGE_TABLE)
        client.insert(STAGE_TABLE, stage_rows, column_names=list(STAGE_COLUMNS))
        result["inserted_rows"] = len(stage_rows)
        readback = _verify_stage_readback(client, stage_rows, trade_date)
        result["readback"] = readback
        if not readback["ok"]:
            result["rolled_back"] = True
            result["rollback"] = _rollback_stage_date(client, trade_date)
            result["reason"] = f"stage 读回验证不一致，已回滚删除该日 stage 行（{readback['summary']}）"
            return result
        result["performed"] = True
        return result
    except Exception as exc:  # noqa: BLE001 - 写路径异常也必须回滚，绝不留半截
        rollback_info = None
        try:
            rollback_info = _rollback_stage_date(client, trade_date)
            result["rolled_back"] = True
        except Exception:  # noqa: BLE001 - 回滚失败要如实报告原始异常
            pass
        if rollback_info is not None:
            result["rollback"] = rollback_info
        result["reason"] = f"stage 写入异常已回滚: {type(exc).__name__}: {exc}"
        return result


def _default_crosscheck_fetch(code: str, trade_date: str) -> Optional[dict[str, Any]]:
    """默认交叉取数：腾讯日线（不复权），取当日那一根；北交所由选样排除。"""
    result = tencent_daily.fetch_daily_kline(code, start=trade_date, end=trade_date, count=10, adjust="")
    for row in result["rows"]:
        if row["date"] == trade_date:
            return {"code": code, "date": row["date"], "close": row["close"],
                    "volume": row["volume"], "volume_unit": row["volume_unit"]}
    return None


# ── --formal-write：锁内缺失键反连接 + 契约列插入 + 读回验证 + marker 回滚 ──
class _FormalJournal:
    """追加式 JSONL journal；每条记录都是一次原子分段写（段文件 fsync 后 O_APPEND + fsync）。

    与 scripts/execute_kline_daily_schema_migration.py 的 JsonlJournal 同模式：
    序列化失败绝不污染已有记录，journal 里永远不会有半截行。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sequence = 0

    def write(self, event: Mapping[str, Any]) -> dict[str, Any]:
        self._sequence += 1
        record = {"seq": self._sequence, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  **dict(event)}
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n"
        segment = self.path.with_name(f"{self.path.name}.seg.{os.getpid()}.{self._sequence}.tmp")
        try:
            with segment.open("w", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            try:
                os.write(descriptor, line.encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if segment.exists():
                segment.unlink()
        return record


def _formal_rows_for_insert(rows: Sequence[Mapping[str, Any]], trade_date: str,
                            batch_created_at: datetime) -> list[tuple]:
    """契约行 → 正式表插入元组（列序 = FORMAL_COLUMNS）。

    id = cityHash64(code, trade_date)（与迁移统一重生的 id 同源）；created_at = 批次时间戳
    （同批同行同值，兼作回滚 marker）；amplitude/change_pct/change_amount 由 prev_close 派生
    （无 prev_close → 0）；盘后包无流通股本 → turnover_rate 恒 0；血缘列绝不进正式表。
    """
    day = date.fromisoformat(_to_iso_day(trade_date))
    out = []
    for row in rows:
        code = str(row["code"])
        open_, high, low, close = (float(row["open"]), float(row["high"]),
                                   float(row["low"]), float(row["close"]))
        volume, amount = float(row["volume"]), float(row["amount"])
        prev_close = float(row.get("prev_close") or 0.0)
        if prev_close > 0:
            amplitude = (high - low) / prev_close * 100.0
            change_pct = (close - prev_close) / prev_close * 100.0
            change_amount = close - prev_close
        else:
            amplitude = change_pct = change_amount = 0.0
        out.append((
            code, day, open_, high, low, close, volume, amount,
            amplitude, change_pct, change_amount, 0.0,
            batch_created_at, formal_row_id(code, trade_date),
        ))
    return out


def _verify_cityhash_probe(client: Any, codes: Sequence[str], trade_date: str) -> dict[str, Any]:
    """锁内探针：让 ClickHouse 现算 cityHash64(code, toDate(day))，与本模块实现比对。

    任何一码不符 → fail-closed（探针失败在插入之前，绝不带病写 id）。
    """
    probes, mismatches = [], []
    for code in codes:
        sql = ("SELECT cityHash64('" + str(code).replace("'", "") + "', "
               f"toDate('{_to_iso_day(trade_date)}')) AS probe_id")
        rows = _read_only_rows(client, sql)
        server_value = int(rows[0]["probe_id"]) if rows else None
        local_value = formal_row_id(str(code), trade_date)
        probes.append({"code": str(code), "server": server_value, "local": local_value})
        if server_value != local_value:
            mismatches.append(probes[-1])
    return {"ok": not mismatches, "probed": probes, "mismatches": mismatches}


def _formal_day_count(client: Any, trade_date: str) -> int:
    rows = _read_only_rows(
        client, f"SELECT count() AS row_count FROM {KLINE_DAILY_TABLE} FINAL "
                f"WHERE trade_date = toDate('{trade_date}')")
    return int(rows[0]["row_count"]) if rows else 0


def _formal_bad_id_count(client: Any, trade_date: str, batch_created_at: datetime) -> int:
    stamp = batch_created_at.strftime("%Y-%m-%d %H:%M:%S")
    rows = _read_only_rows(
        client, f"SELECT count() AS bad_id_rows FROM {KLINE_DAILY_TABLE} FINAL "
                f"WHERE trade_date = toDate('{trade_date}') "
                f"AND created_at = toDateTime('{stamp}') "
                "AND id != cityHash64(code, trade_date)")
    return int(rows[0]["bad_id_rows"]) if rows else 0


def _verify_formal_readback(client: Any, insert_rows: Sequence[tuple],
                            existing_rows: Sequence[Mapping[str, Any]],
                            trade_date: str, batch_created_at: datetime) -> dict[str, Any]:
    """读回验证：该日行数（缺失数 + 已有重叠数）+ 逐日 digest + 抽样逐字段 + id 服务端一致。"""
    stamp = batch_created_at.strftime("%Y-%m-%d %H:%M:%S")
    expected_rows = [dict(zip(FORMAL_COLUMNS, item)) for item in insert_rows]
    day_rows = _read_formal_overlap_rows(client, trade_date)          # 插入后整日 7 列视图
    row_count = _formal_day_count(client, trade_date)
    expected_digest = _stage_digest(list(existing_rows) + expected_rows)
    readback_digest = _stage_digest(day_rows)

    sampled = _read_only_rows(
        client,
        f"SELECT {', '.join(FORMAL_READBACK_COLUMNS)} FROM {KLINE_DAILY_TABLE} FINAL "
        f"WHERE trade_date = toDate('{trade_date}') AND created_at = toDateTime('{stamp}') "
        "ORDER BY code")
    readback_by_code = {str(row["code"]): row for row in sampled}
    expected_codes = sorted(row["code"] for row in expected_rows)
    sample_mismatches = []
    for index in _sample_indexes(len(expected_codes)):
        code = expected_codes[index]
        want = next(row for row in expected_rows if row["code"] == code)
        got = readback_by_code.get(code)
        if got is None:
            sample_mismatches.append({"code": code, "problems": ["读回缺行（本批次 marker 无此键）"]})
            continue
        problems = []
        for field, fmt in (("code", None), ("open", ".4f"), ("high", ".4f"), ("low", ".4f"),
                           ("close", ".4f"), ("volume", ".2f"), ("amount", ".2f"),
                           ("amplitude", ".6f"), ("change_pct", ".6f"),
                           ("change_amount", ".6f"), ("turnover_rate", ".6f")):
            left, right = want[field], got[field]
            if fmt is not None:
                left, right = format(float(left), fmt), format(float(right), fmt)
            if str(left) != str(right):
                problems.append(f"{field}: 插入 {want[field]!r} != 读回 {got[field]!r}")
        if int(want["id"]) != int(got["id"]):
            problems.append(f"id: 插入 {want['id']!r} != 读回 {got['id']!r}")
        if problems:
            sample_mismatches.append({"code": code, "problems": problems})

    bad_id_rows = _formal_bad_id_count(client, trade_date, batch_created_at)
    expected_total = len(existing_rows) + len(insert_rows)
    count_ok = row_count == expected_total
    digest_ok = readback_digest == expected_digest
    ok = count_ok and digest_ok and not sample_mismatches and bad_id_rows == 0
    return {
        "ok": ok,
        "row_count": row_count,
        "expected_row_count": expected_total,
        "row_count_ok": count_ok,
        "digest_match": digest_ok,
        "per_day_digest": {trade_date: readback_digest},
        "expected_digest": expected_digest,
        "readback_digest": readback_digest,
        "sample_checked": min(len(expected_codes), 5),
        "sample_mismatches": sample_mismatches[:50],
        "bad_id_rows": bad_id_rows,
        "batch_created_at": stamp,
        "summary": ("" if ok else
                    f"row_count {row_count}/{expected_total}, digest_match {digest_ok}, "
                    f"sample_mismatches {len(sample_mismatches)}, bad_id_rows {bad_id_rows}"),
    }


def _rollback_formal_batch(client: Any, trade_date: str, batch_ids: Sequence[int],
                           chunk_size: int = 500) -> dict[str, Any]:
    """回滚：按本批次精确 id 列表同步删除并逐块验证删净。

    id 为插入时确定性生成的 UInt64 列表：即使 created_at 批次 marker 的时区
    语义再生变故，回滚仍精确可靠；绝不触碰其他批次/其他写入者的行。
    mutations_sync=2 逐块等待 mutation 完成。
    """
    assert_formal_table_allowed(KLINE_DAILY_TABLE)
    ids = [int(value) for value in batch_ids]
    sqls: list[str] = []
    remaining_total = 0
    for start in range(0, len(ids), chunk_size):
        chunk = [str(value) for value in ids[start:start + chunk_size]]
        quoted = ", ".join(chunk)
        sql = (f"ALTER TABLE {KLINE_DAILY_TABLE} DELETE "
               f"WHERE trade_date = toDate('{trade_date}') AND id IN ({quoted})")
        client.command(sql, settings={"mutations_sync": 2})
        sqls.append(sql)
        rows = _read_only_rows(
            client, f"SELECT count() AS row_count FROM {KLINE_DAILY_TABLE} "
                    f"WHERE trade_date = toDate('{trade_date}') AND id IN ({quoted})")
        remaining_total += int(rows[0]["row_count"]) if rows else 0
    if remaining_total:
        raise RuntimeError(
            f"回滚后 {KLINE_DAILY_TABLE} {trade_date} 仍有 {remaining_total} 行本批次 id 行未删除")
    return {"sql": sqls[0] if len(sqls) == 1 else sqls,
            "deleted": {"trade_date": trade_date, "id_count": len(ids)},
            "remaining_rows": remaining_total, "mutations_sync": 2}


def _formal_write_flow(client: Any, rows: Sequence[Mapping[str, Any]], trade_date: str,
                       lineage: Mapping[str, Any], output_path: Path,
                       opts: Mapping[str, Any]) -> dict[str, Any]:
    """--formal-write 主流程（仅在全部门禁通过后进入）：

    白名单断言 → acquire DbWriteLock(scope=kline_daily_daily_append) → 锁内
    cityHash64 探针 → 锁内重新执行缺失键反连接（重叠值不一致 fail-closed 不写）→
    只插缺失键（契约列 + id + 批次 created_at marker）→ 读回验证 → 不符按 marker 回滚删净
    → 释放锁。全程写 JSONL journal（原子分段写）。
    """
    assert_formal_table_allowed(KLINE_DAILY_TABLE)
    journal_path = Path(opts.get("formal_journal_path")
                        or (Path(output_path).parent / "formal_write_journal.jsonl"))
    journal = _FormalJournal(journal_path)
    result: dict[str, Any] = {
        "performed": False, "rolled_back": False, "table": KLINE_DAILY_TABLE,
        "scope": FORMAL_APPEND_LOCK_SCOPE, "journal_path": str(journal_path),
        "lineage": {"source_url": lineage["url"], "zip_sha256": lineage["zip_sha256"]},
    }
    journal.write({"event": "formal_write_start", "trade_date": trade_date,
                   "table": KLINE_DAILY_TABLE, "pipeline_version": PIPELINE_VERSION,
                   "row_count": len(rows)})
    lock = DbWriteLock(client, journal=journal.write)
    owner = f"daily_kline_pipeline@{socket.gethostname()}:{os.getpid()}"
    lock_info: Optional[dict[str, Any]] = None
    acquired = False
    try:
        try:
            lock_info = lock.acquire(FORMAL_APPEND_LOCK_SCOPE, owner,
                                     purpose=f"daily append {trade_date} (tdx pkg)")
            acquired = True
        except LockHeldError as exc:
            result["lock"] = {"held": True, "table": exc.table,
                              "holder": exc.holder, "scope": exc.scope}
            result["reason"] = (f"写锁被占用 {exc.table}（scope={exc.scope}），"
                                f"持有者 {exc.holder}；fail-closed 拒绝写入")
            journal.write({"event": "formal_write_lock_held", "table": exc.table,
                           "holder": exc.holder})
            return result
        except LockError as exc:
            result["lock"] = {"held": None, "error": f"{type(exc).__name__}: {exc}"}
            result["reason"] = f"写锁获取失败: {type(exc).__name__}: {exc}"
            journal.write({"event": "formal_write_lock_error",
                           "error": f"{type(exc).__name__}: {exc}"})
            return result
        result["lock"] = {"held": False, "table": lock_info["table"], "owner": owner}
        journal.write({"event": "formal_write_lock_acquired", "table": lock_info["table"],
                       "owner": owner})
        try:
            # 1) 锁内反连接：重新读正式表该日已有键（门禁后可能有人并发写入）
            formal_rows = _read_formal_overlap_rows(client, trade_date)
            overlap_check = check_formal_overlap(rows, formal_rows=formal_rows)
            existing_codes = {str(row["code"]) for row in formal_rows}
            row_codes = {str(row["code"]) for row in rows}
            missing_rows = [row for row in rows if str(row["code"]) not in existing_codes]
            result["overlap_key_count"] = len(row_codes & existing_codes)
            result["missing_key_count"] = len(missing_rows)
            journal.write({"event": "formal_anti_join", "trade_date": trade_date,
                           "existing_keys": len(existing_codes),
                           "overlap_keys": result["overlap_key_count"],
                           "missing_keys": len(missing_rows),
                           "overlap_status": overlap_check["status"]})
            if overlap_check["status"] == "fail":
                result["overlap_check"] = overlap_check
                result["reason"] = (f"锁内反连接发现重叠键值不一致"
                                    f"（{overlap_check['mismatch_count']} 处超容差），fail-closed 拒绝写入")
                journal.write({"event": "formal_overlap_mismatch",
                               "mismatch_count": overlap_check["mismatch_count"]})
                return result
            result["overlap_check"] = overlap_check
            if not missing_rows:
                result["inserted_rows"] = 0
                result["batch_created_at"] = None
                result["readback"] = _verify_formal_readback(
                    client, [], formal_rows, trade_date,
                    datetime.now(timezone.utc).replace(microsecond=0))
                result["performed"] = bool(result["readback"]["ok"])
                if not result["performed"]:
                    result["reason"] = f"无缺失键但整日读回验证不符（{result['readback']['summary']}）"
                journal.write({"event": "formal_no_missing_keys",
                               "row_count": result["readback"]["row_count"]})
                return result

            # 2) cityHash64 探针（锁内、插入前）：ClickHouse 现算值与本实现必须一致
            probe_codes = sorted({str(row["code"]) for row in
                                  (missing_rows[:1] + missing_rows[len(missing_rows) // 2:len(missing_rows) // 2 + 1]
                                   + missing_rows[-1:])})
            probe = _verify_cityhash_probe(client, probe_codes, trade_date)
            result["cityhash_probe"] = probe
            journal.write({"event": "formal_cityhash_probe", "probed": len(probe["probed"]),
                           "ok": probe["ok"]})
            if not probe["ok"]:
                result["reason"] = (f"cityHash64 探针不符（本地实现与 ClickHouse 现算值不一致），"
                                    f"fail-closed 拒绝写入: {probe['mismatches'][:3]}")
                journal.write({"event": "formal_cityhash_probe_failed",
                               "mismatches": probe["mismatches"][:3]})
                return result

            # 3) 构造并插入契约列元组（批次时间戳 marker）
            #    clickhouse_connect 对 naive datetime 按“进程本地时区”调 .timestamp() 序列化，
            #    与 SQL 字符串 literal（服务器 UTC 解析）必然错位 —— 故 marker 一律用
            #    UTC-aware datetime，strftime 后与 toDateTime() literal 严格同墙钟。
            batch_created_at = datetime.now(timezone.utc).replace(microsecond=0)
            result["batch_created_at"] = batch_created_at.strftime("%Y-%m-%d %H:%M:%S")
            insert_rows = _formal_rows_for_insert(missing_rows, trade_date, batch_created_at)
            result["batch_ids"] = [row[-1] for row in insert_rows]
            result["insert_attempted"] = True
            assert_formal_table_allowed(KLINE_DAILY_TABLE)
            client.insert(KLINE_DAILY_TABLE, insert_rows, column_names=list(FORMAL_COLUMNS))
            result["inserted_rows"] = len(insert_rows)
            journal.write({"event": "formal_insert", "trade_date": trade_date,
                           "table": KLINE_DAILY_TABLE, "inserted_rows": len(insert_rows),
                           "batch_created_at": result["batch_created_at"]})

            # 4) 读回验证：行数（缺失 + 已有重叠）+ 日 digest + 抽样逐字段 + id 服务端一致
            readback = _verify_formal_readback(client, insert_rows, formal_rows,
                                               trade_date, batch_created_at)
            result["readback"] = readback
            if not readback["ok"]:
                result["rolled_back"] = True
                result["rollback"] = _rollback_formal_batch(
                    client, trade_date, result.get("batch_ids") or [])
                result["reason"] = (f"正式表读回验证不一致，已按批次 marker 回滚删净"
                                    f"（{readback['summary']}）")
                journal.write({"event": "formal_readback_mismatch", "summary": readback["summary"]})
                journal.write({"event": "formal_rollback_done",
                               "remaining_rows": result["rollback"]["remaining_rows"]})
                return result
            journal.write({"event": "formal_readback_ok",
                           "row_count": readback["row_count"],
                           "digest": readback["readback_digest"]})
            result["performed"] = True
            return result
        except Exception as exc:  # noqa: BLE001 - 写路径异常也必须按 marker 回滚，绝不留半截
            journal.write({"event": "formal_write_error",
                           "error": f"{type(exc).__name__}: {exc}"})
            if result.get("insert_attempted"):
                try:
                    result["rollback"] = _rollback_formal_batch(
                        client, trade_date, result.get("batch_ids") or [])
                    result["rolled_back"] = True
                    journal.write({"event": "formal_rollback_done",
                                   "remaining_rows": result["rollback"]["remaining_rows"]})
                except Exception as rollback_exc:  # noqa: BLE001 - 回滚失败要如实上报
                    result["rollback_error"] = f"{type(rollback_exc).__name__}: {rollback_exc}"
                    journal.write({"event": "formal_rollback_failed",
                                   "error": result["rollback_error"]})
            result["reason"] = f"formal 写入异常已回滚: {type(exc).__name__}: {exc}"
            return result
    finally:
        if acquired:
            try:
                lock.release(FORMAL_APPEND_LOCK_SCOPE, owner)
                result["lock_released"] = True
                journal.write({"event": "formal_write_lock_released",
                               "table": (lock_info or {}).get("table")})
            except Exception as exc:  # noqa: BLE001 - 释放失败如实入 journal，不掩盖结果
                result["lock_released"] = False
                journal.write({"event": "formal_write_lock_release_failed",
                               "error": f"{type(exc).__name__}: {exc}"})


# ── local 源（lday）辅助：对照统计 / 覆盖缺口 / runtime 报告落盘 ──────────

def _lday_runtime_dir(runtime_dir: Optional[Path] = None) -> Path:
    if runtime_dir is not None:
        return Path(runtime_dir)
    return _default_data_root() / "runtime" / "tdx_lday_daily"


def _write_lday_runtime_json(name: str, payload: Mapping[str, Any],
                             runtime_dir: Optional[Path] = None) -> Path:
    directory = _lday_runtime_dir(runtime_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    _atomic_json(path, dict(payload))
    return path


def _local_vs_formal_stats(rows: Sequence[Mapping[str, Any]],
                           formal_rows: Sequence[Mapping[str, Any]],
                           trade_date: str) -> dict[str, Any]:
    """B5 全量对照统计：local lday 行 vs 正式表该日行（价 1% / 量额 3% 容差）。

    与 formal_overlap_consistency 门禁同容差；本函数额外产出逐字段最差相对差
    与超差明细，落盘为对照报告（Owner 2026-11-13 B5：不一致停下报告，不得猜换算）。
    """
    formal_by_code = {str(row["code"]): row for row in formal_rows}
    fields = ("open", "high", "low", "close", "volume", "amount")
    compared = 0
    per_field_worst: dict[str, dict[str, Any]] = {}
    violations: list[dict[str, Any]] = []
    for row in rows:
        code = str(row["code"])
        formal = formal_by_code.get(code)
        if formal is None:
            continue
        compared += 1
        for field in fields:
            mine, theirs = float(row[field]), float(formal[field])
            rel = _rel_diff(mine, theirs)
            best = per_field_worst.get(field)
            if best is None or rel > best["rel_diff"]:
                per_field_worst[field] = {"code": code, "local": mine,
                                          "formal": theirs, "rel_diff": round(rel, 8)}
            tolerance = PRICE_TOLERANCE if field in ("open", "high", "low", "close") \
                else QUANTITY_TOLERANCE
            if rel > tolerance:
                violations.append({"code": code, "field": field, "local": mine,
                                   "formal": theirs, "rel_diff": round(rel, 6),
                                   "tolerance": tolerance})
    return {
        "trade_date": trade_date,
        "compared": compared,
        "tolerances": {"price": PRICE_TOLERANCE, "quantity": QUANTITY_TOLERANCE},
        "violation_count": len(violations),
        "violations": violations[:200],
        "worst_rel_diff_by_field": per_field_worst,
        "verdict": "consistent" if not violations else "INCONSISTENT_STOP_REPORT",
        "note": "local=客户端 vipdoc lday；formal=官网包已入库行；量纲由 vwap 反推+全量对照双重裁决",
    }


def _local_coverage_report(db: Any, rows: Sequence[Mapping[str, Any]],
                           trade_date: str, opts: Mapping[str, Any]) -> dict[str, Any]:
    """B4 覆盖率：写入后正式表该日行数 + 与 lc5（fzline）宇宙覆盖缺口，报告落 runtime。"""
    coverage_min = int(opts.get("coverage_min") or DEFAULT_COVERAGE_MIN)
    tdx_root = str(opts.get("tdx_root") or DEFAULT_TDX_VIPDOC_ROOT)
    postwrite_row_count = _formal_day_count(db, trade_date)
    lc5_codes = lday.scan_lc5_universe_files(tdx_root)
    daily_codes = {str(row["code"]) for row in rows}
    lc5_not_in_daily = sorted(lc5_codes - daily_codes)
    daily_not_in_lc5 = sorted(daily_codes - lc5_codes)
    payload: dict[str, Any] = {
        "trade_date": trade_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tdx_root": tdx_root,
        "postwrite_row_count": postwrite_row_count,
        "coverage_min": coverage_min,
        "coverage_ok": postwrite_row_count >= coverage_min,
        "daily_row_count": len(daily_codes),
        "lc5_universe_count": len(lc5_codes),
        "lc5_not_in_daily_count": len(lc5_not_in_daily),
        "lc5_not_in_daily": lc5_not_in_daily[:500],
        "daily_not_in_lc5_count": len(daily_not_in_lc5),
        "daily_not_in_lc5": daily_not_in_lc5[:500],
    }
    payload["report_path"] = str(_write_lday_runtime_json(
        f"coverage_gap_{trade_date.replace('-', '')}", payload,
        runtime_dir=opts.get("runtime_dir")))
    return payload


def _run_prev_day_gate(client: Any, rows: Sequence[Mapping[str, Any]], trade_date: str) -> dict[str, Any]:
    sessions_desc = _read_calendar_sessions(client, trade_date)
    earlier = [day for day in sessions_desc if day < trade_date]
    if not earlier:
        return {"status": "fail",
                "reason": f"日历里找不到 {trade_date} 之前的前一交易日，无法对比",
                "violations": []}
    prev_date = earlier[0]
    prev_rows = _read_prev_day_rows(client, prev_date)
    prev_codes = [str(row["code"]) for row in prev_rows]
    current_codes = {str(row["code"]) for row in rows}
    stocks_by_code, stocks_ok = _read_stocks_master_best_effort(
        client, sorted(current_codes - set(prev_codes)) + sorted(set(prev_codes) - current_codes))
    gate = check_row_count_vs_prev_day(rows, prev_codes=prev_codes, prev_date=prev_date,
                                       target_date=trade_date, stocks_by_code=stocks_by_code)
    gate["stocks_master_available"] = stocks_ok
    return gate


def main(argv: Optional[Sequence[str]] = None, injections: Optional[Mapping[str, Any]] = None) -> int:
    """CLI 入口。injections 供测试离线注入（fetch_package/client_factory/crosscheck_fetch/
    min_priced/now），生产调用不传。"""
    args = parse_args(argv)
    options: dict[str, Any] = {
        "trade_date": args.trade_date,
        "source": args.source,
        "tdx_root": str(args.tdx_root),
        "min_fresh_files": args.min_fresh_files,
        "coverage_min": args.coverage_min,
        "crosscheck_sample": args.crosscheck_sample,
        "stage_write": args.stage_write,
        "formal_write": args.formal_write,
        "output": args.output,
    }
    if injections:
        options.update(injections)
    result = run_pipeline(options)
    print(json.dumps(result["report"], ensure_ascii=False, sort_keys=True, indent=2, default=str))
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
