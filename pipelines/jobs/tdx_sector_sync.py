#!/usr/bin/env python3
"""通达信标准板块（行业/概念/风格/地区）主档、成分股与历史日线同步管道。

数据源（均为本机/容器只读挂载的通达信客户端产物，零外部网络依赖）：
  - <TDX_BASE>/T0002/hq_cache/tdxzs3.cfg          板块指数目录（type 2=标准行业, 4=概念, 5=风格, 3=地区）
  - <TDX_BASE>/T0002/hq_cache/tdxhy.cfg           全市场个股 -> 通达信行业编码映射（含沪深北）
  - <TDX_BASE>/T0002/hq_cache/infoharbor_block.dat 概念/风格板块 -> 成分股列表（按 880xxx 代码关联）
  - <TDX_BASE>/vipdoc/sh/lday/sh88*.day           通达信官方板块指数日线（2010 年至今完整 OHLC/量/额）

写入目标（ClickHouse）：
  - stock.sectors            板块主档（code 统一为 880xxx.SH / 881xxx.SH）
  - stock.sector_stocks      板块成分股关系（严格过滤 NEEQ 代码）
  - stock.sector_kline_daily 板块官方指数历史日线（近 N 日同步补齐成分股涨跌家数与涨跌停统计）

用法：
  # 只读预检（默认不写库）
  python -m pipelines.jobs.tdx_sector_sync --dry-run

  # 全量初始化（替换旧 SW2 数据并回补全部历史板块日线）
  python -m pipelines.jobs.tdx_sector_sync --formal-write --full-replace

  # 每日盘后增量同步（刷新主档/成分股 + 增量更新近 10 天板块日线）
  python -m pipelines.jobs.tdx_sector_sync --formal-write --recent-days 10
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from utils.logger import get_logger
from utils.market_warehouse import clickhouse_client

logger = get_logger("tdx_sector_sync")
BJ_TZ = ZoneInfo("Asia/Shanghai")

DEFAULT_TDX_BASE = r"D:/TDX"

# 通达信 tdxzs3.cfg 类型码映射（按用户裁决：采用通达信标准行业 type=2 + 概念/风格/地区）
TDX_TYPE_MAP: Dict[str, str] = {
    "2": "industry",   # 通达信标准行业（一/二级，145个）
    "4": "concept",    # 通达信概念板块（269个）
    "5": "style",      # 通达信风格板块（158个）
    "3": "region",     # 通达信地区板块（32个）
}

# 与 ClickHouse codex_no_neeq_* CHECK 约束完全一致的 NEEQ 过滤正则
_NEEQ_CODE_RE = re.compile(
    r"^(?:(?:400|420|430)[0-9]{3}|(?:43|83|87)[0-9]{4})(?:\.(?:SZ|SH|NQ|NEEQ|BJ))?$"
    r"|^88[0-9]{4}(?:\.(?:SZ|NQ|NEEQ))?$"
)
_VALID_STOCK_RE = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")


def resolve_tdx_base(override: Optional[Path] = None) -> Path:
    if override is not None:
        return override
    for env_key in ("TDX_BASE", "AISTOCK_TDX_BASE"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return Path(val)
    vipdoc = os.environ.get("AISTOCK_TDX_VIPDOC_ROOT", "").strip()
    if vipdoc:
        return Path(vipdoc).parent
    return Path(DEFAULT_TDX_BASE)


def _stable_id(text: str) -> int:
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF


def _normalize_stock_code(mkt: str, raw_code: str) -> Optional[str]:
    """将通达信市场码 + 6 位代码转换为标准证券码，并过滤非 A 股及 NEEQ 代码。"""
    code = raw_code.strip()
    if len(code) != 6 or not code.isdigit():
        return None
    if mkt == "0":
        suffix = ".SZ"
    elif mkt == "1":
        suffix = ".SH"
    elif mkt == "2":
        suffix = ".BJ"
    else:
        return None

    full = f"{code}{suffix}"
    if not _VALID_STOCK_RE.match(full):
        return None
    if _NEEQ_CODE_RE.match(full):
        return None
    # 北交所仅保留标准 920xxx.BJ
    if suffix == ".BJ" and not code.startswith("920"):
        return None
    # 沪深仅保留常见 A 股代码段（排除指数/债券/基金）
    if suffix == ".SH" and not code.startswith(("600", "601", "603", "605", "688", "689")):
        return None
    if suffix == ".SZ" and not code.startswith(("000", "001", "002", "003", "300", "301")):
        return None
    return full


def parse_tdx_sectors(tdx_base: Path) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """解析通达信标准板块主档与成分股映射。

    Returns:
        (sectors, memberships)
        sectors: [{code, name, type, parent_code, level, stock_count, tdx_raw_code, tdx_bcode}]
        memberships: [(sector_code, stock_code), ...] 去重有序列表
    """
    hq_cache = tdx_base / "T0002" / "hq_cache"
    zs3_path = hq_cache / "tdxzs3.cfg"
    hy_path = hq_cache / "tdxhy.cfg"
    ib_path = hq_cache / "infoharbor_block.dat"

    for p in (zs3_path, hy_path, ib_path):
        if not p.exists():
            raise FileNotFoundError(f"缺少通达信板块元数据文件: {p}")

    # 1. 解析 tdxzs3.cfg: 名称|代码|类型|数量|排序|编码
    raw_sectors: List[Dict[str, Any]] = []
    bcode_to_sector_code: Dict[str, str] = {}
    for line in zs3_path.read_text(encoding="gbk", errors="ignore").splitlines():
        parts = line.strip().split("|")
        if len(parts) < 6:
            continue
        name, raw_code, tdx_type, _, _, bcode = [x.strip() for x in parts[:6]]
        if tdx_type not in TDX_TYPE_MAP or not raw_code.isdigit() or len(raw_code) != 6:
            continue
        sector_type = TDX_TYPE_MAP[tdx_type]
        sector_code = f"{raw_code}.SH"
        if _NEEQ_CODE_RE.match(sector_code):
            continue

        level = 1
        if sector_type == "industry":
            # T0101 (len=5) 为一级标准行业；T010101 (len=7) 为二级标准行业
            level = 2 if len(bcode) >= 7 else 1
            bcode_to_sector_code[bcode] = sector_code

        raw_sectors.append({
            "code": sector_code,
            "name": name,
            "type": sector_type,
            "parent_code": None,
            "level": level,
            "tdx_raw_code": raw_code,
            "tdx_bcode": bcode,
        })

    # 回填标准行业二级板块的 parent_code（T010101 -> T0101）
    for sec in raw_sectors:
        if sec["type"] == "industry" and sec["level"] == 2 and len(sec["tdx_bcode"]) >= 7:
            parent_bcode = sec["tdx_bcode"][:5]
            sec["parent_code"] = bcode_to_sector_code.get(parent_bcode)

    # 2. 解析 tdxhy.cfg: 市场|代码|通达信行业码|...|研究行业码
    hy_by_bcode: Dict[str, set[str]] = defaultdict(set)
    for line in hy_path.read_text(encoding="gbk", errors="ignore").splitlines():
        parts = line.strip().split("|")
        if len(parts) < 3:
            continue
        mkt, scode, tdx_hy = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not tdx_hy:
            continue
        norm_code = _normalize_stock_code(mkt, scode)
        if norm_code:
            hy_by_bcode[tdx_hy].add(norm_code)

    # 3. 解析 infoharbor_block.dat: #GN_名称,数量,880xxx,...
    ib_by_raw_code: Dict[str, set[str]] = defaultdict(set)
    current_raw_code: Optional[str] = None
    for line in ib_path.read_text(encoding="gbk", errors="ignore").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            hdr = s[1:].split(",")
            current_raw_code = hdr[2].strip() if len(hdr) >= 3 and hdr[2].strip().isdigit() else None
        elif current_raw_code:
            for tok in s.split(","):
                if "#" not in tok:
                    continue
                mkt, scode = tok.split("#", 1)
                norm_code = _normalize_stock_code(mkt.strip(), scode.strip())
                if norm_code:
                    ib_by_raw_code[current_raw_code].add(norm_code)

    # 4. 组装成分股关系与 stock_count
    memberships_set: set[Tuple[str, str]] = set()
    for sec in raw_sectors:
        members: set[str] = set()
        if sec["type"] == "industry":
            bcode = sec["tdx_bcode"]
            for hy_code, stock_set in hy_by_bcode.items():
                if hy_code == bcode or hy_code.startswith(bcode):
                    members.update(stock_set)
        elif sec["type"] in ("concept", "style"):
            members.update(ib_by_raw_code.get(sec["tdx_raw_code"], set()))
        sec["stock_count"] = len(members)
        for stock_code in members:
            memberships_set.add((sec["code"], stock_code))

    memberships = sorted(memberships_set)
    return raw_sectors, memberships


def parse_sector_day_file(
    day_path: Path,
    sector_code: str,
    stock_count: int,
    *,
    since_date: Optional[date] = None,
) -> List[Tuple[Any, ...]]:
    """解析单个通达信 sh88xxxx.day 文件，返回 sector_kline_daily 待插入行。"""
    if not day_path.exists():
        return []
    raw = day_path.read_bytes()
    n_bars = len(raw) // 32
    if n_bars <= 0:
        return []

    rows: List[Tuple[Any, ...]] = []
    prev_close: Optional[float] = None
    for idx in range(n_bars):
        d_int, o_i, h_i, l_i, c_i, amt_f, vol_i, _ = struct.unpack_from("<IIIIIfII", raw, idx * 32)
        if d_int < 20000101 or d_int > 20991231:
            continue
        y, rem = divmod(d_int, 10000)
        m, d = divmod(rem, 100)
        try:
            trade_dt = date(y, m, d)
        except ValueError:
            continue

        o = round(o_i / 100.0, 4)
        h = round(h_i / 100.0, 4)
        l = round(l_i / 100.0, 4)
        c = round(c_i / 100.0, 4)
        if c <= 0:
            continue

        change_pct: Optional[float] = None
        if prev_close is not None and prev_close > 0:
            change_pct = round((c - prev_close) / prev_close * 100.0, 4)
        prev_close = c

        if since_date is not None and trade_dt < since_date:
            continue

        rows.append((
            sector_code,
            trade_dt,
            o,
            h,
            l,
            c,
            change_pct,
            int(stock_count),
            0,  # rise_count (近期交易日随后由 kline_daily 批量补齐)
            0,  # fall_count
            0,  # flat_count
            0,  # limit_up_count
            0,  # limit_down_count
            float(amt_f if amt_f == amt_f and amt_f >= 0 else 0.0),
            int(vol_i),
        ))
    return rows


def _compute_recent_breadth_map(
    client,
    since_date: date,
) -> Dict[Tuple[str, date], Tuple[int, int, int, int, int, int]]:
    """基于 stock.kline_daily FINAL × stock.sector_stocks 批量计算近 N 日各板块真实涨跌家数与涨跌停数。"""
    sql = """
        SELECT
            ss.sector_code AS sector_code,
            k.trade_date AS trade_date,
            toInt32(count()) AS stock_cnt,
            toInt32(countIf(k.change_pct > 0)) AS rise_cnt,
            toInt32(countIf(k.change_pct < 0)) AS fall_cnt,
            toInt32(countIf(k.change_pct = 0)) AS flat_cnt,
            toInt32(countIf(k.change_pct >= 9.8)) AS lu_cnt,
            toInt32(countIf(k.change_pct <= -9.8)) AS ld_cnt
        FROM stock.sector_stocks ss FINAL
        JOIN stock.kline_daily k FINAL ON k.code = ss.stock_code
        WHERE k.trade_date >= %(since_date)s
          AND k.open > 0
        GROUP BY ss.sector_code, k.trade_date
    """
    result = client.query(sql, parameters={"since_date": since_date})
    breadth: Dict[Tuple[str, date], Tuple[int, int, int, int, int, int]] = {}
    for row in result.result_rows:
        sec_code, tdate, scnt, rcnt, fcnt, flcnt, lucnt, ldcnt = row
        if isinstance(tdate, datetime):
            tdate = tdate.date()
        breadth[(str(sec_code), tdate)] = (
            int(scnt),
            int(rcnt),
            int(fcnt),
            int(flcnt),
            int(lucnt),
            int(ldcnt),
        )
    return breadth


def _insert_in_batches(client, table: str, columns: Sequence[str], rows: Sequence[Tuple[Any, ...]], batch_size: int = 50000) -> int:
    total = 0
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        client.insert(table, chunk, column_names=list(columns))
        total += len(chunk)
    return total


def run_sync(
    *,
    tdx_base: Path,
    formal_write: bool = False,
    full_replace: bool = False,
    recent_days: Optional[int] = None,
    breadth_days: int = 60,
) -> Dict[str, Any]:
    now_dt = datetime.now(BJ_TZ).replace(tzinfo=None)
    sectors, memberships = parse_tdx_sectors(tdx_base)
    lday_dir = tdx_base / "vipdoc" / "sh" / "lday"

    since_date: Optional[date] = None
    if recent_days is not None and recent_days > 0 and not full_replace:
        since_date = date.today() - timedelta(days=int(recent_days) * 2)

    by_type: Dict[str, int] = defaultdict(int)
    for sec in sectors:
        by_type[sec["type"]] += 1

    all_kline_rows: List[Tuple[Any, ...]] = []
    missing_day_files: List[str] = []
    min_trade_date: Optional[date] = None
    max_trade_date: Optional[date] = None

    for sec in sectors:
        day_path = lday_dir / f"sh{sec['tdx_raw_code']}.day"
        parsed = parse_sector_day_file(day_path, sec["code"], sec["stock_count"], since_date=since_date)
        if not parsed:
            missing_day_files.append(sec["code"])
            continue
        if min_trade_date is None or parsed[0][1] < min_trade_date:
            min_trade_date = parsed[0][1]
        if max_trade_date is None or parsed[-1][1] > max_trade_date:
            max_trade_date = parsed[-1][1]
        all_kline_rows.extend(parsed)

    summary: Dict[str, Any] = {
        "tdx_base": str(tdx_base),
        "mode": "formal_write" if formal_write else "dry_run",
        "full_replace": full_replace,
        "since_date": since_date.isoformat() if since_date else None,
        "sector_count": len(sectors),
        "sector_by_type": dict(by_type),
        "membership_count": len(memberships),
        "unique_member_stocks": len({s for _, s in memberships}),
        "kline_row_count": len(all_kline_rows),
        "missing_day_files_count": len(missing_day_files),
        "kline_date_range": [
            min_trade_date.isoformat() if min_trade_date else None,
            max_trade_date.isoformat() if max_trade_date else None,
        ],
    }

    if not formal_write:
        return summary

    client = clickhouse_client()

    # 1. 写入 stock.sectors 与 stock.sector_stocks（全量刷新确保与 TDX 当日分类一致）
    client.command("TRUNCATE TABLE stock.sectors")
    client.command("TRUNCATE TABLE stock.sector_stocks")

    sector_rows = [
        (
            sec["code"],
            sec["name"],
            sec["type"],
            sec["parent_code"],
            int(sec["level"]),
            int(sec["stock_count"]),
            now_dt,
            _stable_id(f"tdx-sector:{sec['code']}"),
        )
        for sec in sectors
    ]
    _insert_in_batches(
        client,
        "stock.sectors",
        ["code", "name", "type", "parent_code", "level", "stock_count", "created_at", "id"],
        sector_rows,
    )

    member_rows = [
        (
            sector_code,
            stock_code,
            1.0,
            now_dt,
            _stable_id(f"tdx-member:{sector_code}:{stock_code}"),
        )
        for sector_code, stock_code in memberships
    ]
    _insert_in_batches(
        client,
        "stock.sector_stocks",
        ["sector_code", "stock_code", "weight", "created_at", "id"],
        member_rows,
    )

    # 2. 用近 N 日 kline_daily 补充板块真实涨跌家数与涨跌停数
    breadth_since = (max_trade_date or date.today()) - timedelta(days=int(breadth_days) * 2)
    breadth_map = _compute_recent_breadth_map(client, breadth_since)
    enriched_kline_rows: List[Tuple[Any, ...]] = []
    breadth_hits = 0
    for row in all_kline_rows:
        key = (row[0], row[1])
        b = breadth_map.get(key)
        if b is not None:
            breadth_hits += 1
            enriched_kline_rows.append((
                row[0], row[1], row[2], row[3], row[4], row[5], row[6],
                b[0], b[1], b[2], b[3], b[4], b[5],
                row[13], row[14], now_dt,
            ))
        else:
            enriched_kline_rows.append((*row, now_dt))

    if full_replace:
        client.command("TRUNCATE TABLE stock.sector_kline_daily")

    inserted_kline = _insert_in_batches(
        client,
        "stock.sector_kline_daily",
        [
            "code", "trade_date", "open", "high", "low", "close", "change_pct",
            "stock_count", "rise_count", "fall_count", "flat_count",
            "limit_up_count", "limit_down_count", "total_amount", "total_volume",
            "created_at",
        ],
        enriched_kline_rows,
    )

    summary["write_result"] = {
        "sectors_inserted": len(sector_rows),
        "memberships_inserted": len(member_rows),
        "kline_inserted": inserted_kline,
        "breadth_enriched_rows": breadth_hits,
    }
    return summary


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tdx-base", type=Path, default=None, help="TDX 根目录（默认 D:/TDX 或环境变量 TDX_BASE）")
    parser.add_argument("--dry-run", action="store_true", default=False, help="仅解析校验，不写 ClickHouse")
    parser.add_argument("--formal-write", action="store_true", default=False, help="写入 ClickHouse 正式板块表")
    parser.add_argument("--full-replace", action="store_true", default=False, help="全量清空并重写 sector_kline_daily")
    parser.add_argument("--recent-days", type=int, default=None, help="增量模式：仅更新最近 N 个交易日的板块日线")
    parser.add_argument("--breadth-days", type=int, default=60, help="用 kline_daily 补充涨跌家数的最近交易日数（默认 60）")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    tdx_base = resolve_tdx_base(args.tdx_base)
    formal = bool(args.formal_write and not args.dry_run)
    result = run_sync(
        tdx_base=tdx_base,
        formal_write=formal,
        full_replace=bool(args.full_replace),
        recent_days=args.recent_days,
        breadth_days=args.breadth_days,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
