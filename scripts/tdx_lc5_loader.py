# -*- coding: utf-8 -*-
"""解析 TDX lc5（5分钟K线）文件并写入 CH（盘后补充全市场分钟数据）。

通达信 lc5 格式：每根 bar 32 字节，小端序
  offset | 类型   | 字段     | 乘数
  ------|--------|---------|-------
  0     | int32  | date    | YYYYMMDD
  4     | int32  | time    | HHMMSS
  8     | int32  | open    | 1/1000
  12    | int32  | high    | 1/1000
  16    | int32  | low     | 1/1000
  20    | int32  | close   | 1/1000
  24    | int32  | volume  | 手
  28    | int32  | amount  | 元 (实际文件里可能=元÷10)

使用场景（R57 盘后补充）：
  每天 17:00 后运行，读取 TDX vipdoc/*/fzline/*.lc5 写入 stock.kline_minute_5
  同时聚合写入 15/30/60min

配置文件：TDX_BASE = "D:/TDX"   # 通达信安装目录
"""

import csv
import hashlib
import logging
import os
import struct
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, date as date_type, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

BEIJING_TZ = timezone(timedelta(hours=8))
CH_URL = "http://127.0.0.1:8123/"
CH_DB = "stock"
BATCH_SIZE = 500  # INSERT 分批写入行数

# 只写入 A 股 + 维护的 10 个指数
MAINTAINED_INDICES = frozenset({
    '000001.SH','399001.SZ','399006.SZ','399106.SZ',
    '000680.SH','000300.SH','000016.SH','000905.SH',
    '000852.SH','000688.SH',
})

# 2026-11-13：创业板新增 302 号段（302132.SZ 首例，官网包口径含它）——补入，
# 否则日线/分钟链对该号段全部缺漏（pkg classify 深市按 '30' 前缀放行）。
def is_a_share_stock(code: str) -> bool:
    base = code.upper().split('.')[0]
    mkt = code.upper().split('.')[1] if '.' in code else ''
    if len(base) != 6 or not base.isdigit():
        return False
    return (mkt == 'SZ' and base.startswith(('000','001','002','003','300','301','302'))
            or mkt == 'SH' and base.startswith(('600','601','603','605','688','689'))
            or mkt == 'BJ' and base.startswith('92'))


def should_write(code: str) -> bool:
    return is_a_share_stock(code) or code in MAINTAINED_INDICES

# 通达信 vipdoc 目录配置
TDX_BASE = os.environ.get("TDX_BASE", "D:/TDX")  # 可被环境变量覆盖
VIPDOC = os.path.join(TDX_BASE, "vipdoc")
# 也检查申万宏源（备选数据源）
SW_BASE = "D:/申万宏源"

MARKET_PREFIX = {"sh": "6", "sz": "0", "sz_3": "3", "bj": "92", "bj_8": "8"}
# sh lc5 -> 上证所有股（含688科创）
# sz lc5 -> 深证所有股（00, 30开头）
# bj lc5 -> 北交所

log = logging.getLogger("tdx_lc5_loader")

BAR_SIZE = 32  # lc5 每根 bar 字节数
BAR_STRUCT = struct.Struct("<IIIIIIII")  # date, time, open, high, low, close, volume, amount


def q(sql: str) -> List[List[str]]:
    """CH 查询"""
    req = urllib.request.Request(CH_URL, data=sql.encode("utf-8"), headers={"Content-Type": "text/plain"})
    r = urllib.request.urlopen(req, timeout=120)
    t = r.read().decode().strip()
    return [l.split("\t") for l in t.split("\n")] if t else []


def tdx_to_ch_code(tdx_code: str) -> str:
    """将TDX文件代码转为CH代码格式
    sh600519 -> 600519    (上证)
    sz000001 -> 000001    (深证)
    sz300999 -> 300999    (创业板)
    bj920000 -> 920000    (北交所)
    bj810011 -> 810011    (北交所老代码)
    """
    tdx_code = tdx_code.replace(".lc5", "").replace(".day", "").replace(".min", "")
    if tdx_code.startswith("sh"):
        raw = tdx_code[2:]
        suffix = ".SH"
    elif tdx_code.startswith("sz"):
        raw = tdx_code[2:]
        suffix = ".SZ"
    elif tdx_code.startswith("bj"):
        raw = tdx_code[2:]
        suffix = ".BJ"
    else:
        return tdx_code  # 未知

    return raw + suffix


def load_fzline(filepath: str, run_date: date_type) -> List[dict]:
    """读取单个 lc5 文件，返回今天的 bar 列表"""
    basename = os.path.basename(filepath)
    code_name = basename.replace(".lc5", "")
    ch_code = tdx_to_ch_code(basename)

    # 只写 A 股 + 维护的 10 个指数
    if not should_write(ch_code):
        return []

    bars = []
    with open(filepath, "rb") as f:
        data = f.read()
    
    n_bars = len(data) // BAR_SIZE
    for i in range(n_bars):
        off = i * BAR_SIZE
        raw = data[off:off + BAR_SIZE]
        if len(raw) < BAR_SIZE:
            break
        
        d, t, o_raw, h_raw, l_raw, c_raw, v, amt_raw = BAR_STRUCT.unpack(raw)
        # 日期检查
        year = d // 10000
        month = (d // 100) % 100
        day = d % 100
        
        try:
            bar_date = date_type(year, month, day)
        except (ValueError, OverflowError):
            continue
        
        if bar_date != run_date:
            continue
        
        # 时间
        tm_h = t // 10000
        tm_m = (t // 100) % 100
        tm_s = t % 100  # 通常为 0
        
        # lc5 价格单位 1/1000
        o = o_raw / 1000.0
        h = h_raw / 1000.0
        l = l_raw / 1000.0
        c = c_raw / 1000.0
        
        # 成交量（手）已在文件中直接存储
        vol_shou = v
        
        # 成交额（元，有些文件版本*10）
        amount = amt_raw  # 单位不明，但用于索引聚合时不用
        
        if vol_shou < 0 or o <= 0:
            continue
        
        dt_str = f"{bar_date.isoformat()} {tm_h:02d}:{tm_m:02d}:{tm_s:02d}"
        
        id_src = f"{ch_code}_{dt_str}"
        row_id = abs(hash(id_src)) % (10 ** 15)
        
        bars.append({
            "code": ch_code,
            "datetime": dt_str,
            "open": round(o, 2),
            "high": round(h, 2),
            "low": round(l, 2),
            "close": round(c, 2),
            "volume": vol_shou,
            "amount": round(amount, 2),
            "id": row_id,
        })
    
    return bars


def scan_fzline_files(vipdoc_root: str, run_date: date_type) -> List[Tuple[str, str, int]]:
    """扫描所有 lc5 文件，返回 [(code, filepath, mtime), ...]"""
    files = []
    for market_dir in ["sh", "sz", "bj"]:
        fzline_dir = os.path.join(vipdoc_root, market_dir, "fzline")
        if not os.path.isdir(fzline_dir):
            continue
        
        for fname in os.listdir(fzline_dir):
            if fname.endswith(".lc5"):
                fpath = os.path.join(fzline_dir, fname)
                if not os.path.isfile(fpath):
                    continue
                files.append((fname.replace(".lc5", ""), fpath, os.path.getmtime(fpath)))
    
    # 也尝试申万宏源
    sw_dir = os.path.join(SW_BASE, "vipdoc")
    if os.path.isdir(sw_dir):
        for market_dir in ["sh", "sz", "bj"]:
            fzline_dir = os.path.join(sw_dir, market_dir, "fzline")
            if not os.path.isdir(fzline_dir):
                continue
            for fname in os.listdir(fzline_dir):
                if fname.endswith(".lc5"):
                    fpath = os.path.join(fzline_dir, fname)
                    if not os.path.isfile(fpath):
                        continue
                    # 避免重复（以 TDX 为主）
                    if not any(fname in f[0] or f[0] in fname for f in files):
                        files.append((fname.replace(".lc5", ""), fpath, os.path.getmtime(fpath)))
    
    return files


def insert_batch(rows: List[dict]) -> int:
    """批量 INSERT INTO kline_minute_5"""
    if not rows: return 0
    n = 0
    for i in range(0, len(rows), BATCH_SIZE):
        chunk = rows[i:i + BATCH_SIZE]
        vals = []
        for r in chunk:
            vals.append(f"('{r['code']}', '{r['datetime']}', "
                        f"{r['open']}, {r['high']}, {r['low']}, {r['close']}, "
                        f"{r['volume']}, {r['amount']}, '{r['datetime'][:10]}', {r['id']})")
        sql = ("INSERT INTO stock.kline_minute_5 "
               "(code, datetime, open, high, low, close, volume, amount, created_at, id) VALUES "
               + ",".join(vals))
        try:
            q(sql)
            n += len(chunk)
        except Exception as e:
            log.warning("批量写入失败(%d行): %s", len(chunk), e)
    return n


def aggregate_and_insert(tbl: str, step: int, run_date_str: str):
    """从 kline_minute_5 聚合写入更高时间粒度"""
    log.info("聚合 %s (step=%d)...", tbl, step)
    
    # 清空今天
    q(f"ALTER TABLE stock.{tbl} DELETE WHERE datetime >= '{run_date_str}' AND datetime < '2026-11-13'")
    
    # 读取所有今天5min数据（含指数）
    rows = q(f"SELECT code, datetime, open, high, low, close, volume "
             f"FROM stock.kline_minute_5 WHERE datetime >= '{run_date_str}' AND datetime < '2026-11-13' "
             f"ORDER BY code, datetime")
    
    if not rows:
        log.info("  无数据，跳过")
        return
    
    # 按code分组
    buckets = defaultdict(list)
    for row in rows:
        buckets[row[0]].append(row)
    
    all_vals = []
    for code, bars in buckets.items():
        for i in range(0, len(bars), step):
            chunk = bars[i:i + step]
            if len(chunk) < 2:
                continue
            dt = chunk[0][1]
            o = float(chunk[0][2])
            h = max(float(r[3]) for r in chunk)
            l = min(float(r[4]) for r in chunk)
            c = float(chunk[-1][5])
            v = sum(int(r[6]) for r in chunk if r[6] and r[6].isdigit())
            all_vals.append(f"('{code}', '{dt}', {o}, {h}, {l}, {c}, {v})")
    
    for i in range(0, len(all_vals), BATCH_SIZE):
        chunk_vals = all_vals[i:i + BATCH_SIZE]
        sql = f"INSERT INTO stock.{tbl}(code, datetime, open, high, low, close, volume) VALUES {','.join(chunk_vals)}"
        try:
            q(sql)
        except Exception as e:
            log.warning("  %s INSERT 错误: %s", tbl, e)
    
    # 验证
    r = q(f"SELECT count(), count(DISTINCT code) FROM stock.{tbl} WHERE datetime >= '{run_date_str}' AND datetime < '2026-11-13'")
    if r and r[0]:
        log.info("  %s: %s条 %s只", tbl, r[0][0], r[0][1])


def main():
    run_date = datetime.now(BEIJING_TZ).date()
    run_date_str = run_date.isoformat()
    
    log.info("TDX lc5 盘后补写开始（日期=%s TDX=%s）", run_date_str, TDX_BASE)
    
    # 扫描文件
    log.info("扫描 lc5 文件...")
    files = scan_fzline_files(VIPDOC, run_date)
    log.info("找到 %d 个 lc5 文件", len(files))
    
    # 清空今天 5 min 数据（由腾讯写入的将被 TDX 覆盖——因为 TDX 更准确）
    log.info("清空今天已有 5min 数据...")
    q(f"ALTER TABLE stock.kline_minute_5 DELETE WHERE datetime >= '{run_date_str}' AND datetime < '2026-11-13'")
    
    total_bars = 0
    total_codes = 0
    t_start = datetime.now()
    
    for idx, (code_name, fpath, _) in enumerate(files):
        try:
            bars = load_fzline(fpath, run_date)
        except Exception as e:
            log.warning("  [%d/%d] %s 解析失败: %s", idx + 1, len(files), code_name, e)
            continue
        
        if not bars:
            continue
        
        n = insert_batch(bars)
        if n > 0:
            total_codes += 1
            total_bars += n
        else:
            # 如果不该写入的代码（非A股/非维护指数），跳过
            pass
        
        if (idx + 1) % 500 == 0:
            elapsed = (datetime.now() - t_start).total_seconds()
            log.info("[%d/%d] 累计: %d只 %d根 bar (%d分钟)", idx + 1, len(files), total_codes, total_bars, int(elapsed))
    
    elapsed_total = (datetime.now() - t_start).total_seconds()
    log.info("=" * 50)
    log.info("TDX 写入完成: %d只股票, %d根 bar, 耗时 %.1fmin", total_codes, total_bars, elapsed_total / 60)
    
    # 验证
    r = q(f"SELECT count(), count(DISTINCT code) FROM stock.kline_minute_5 WHERE datetime >= '{run_date_str}' AND datetime < '2026-11-13'")
    if r and r[0]:
        log.info("CH 5min 验证: %s条 %s只", r[0][0], r[0][1])
    
    # 聚合 15/30/60min
    log.info("聚合更高粒度...")
    for tbl, step in [("kline_minute_15", 3), ("kline_minute_30", 6), ("kline_minute_60", 12)]:
        aggregate_and_insert(tbl, step, run_date_str)
    
    log.info("全部完成！")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    main()