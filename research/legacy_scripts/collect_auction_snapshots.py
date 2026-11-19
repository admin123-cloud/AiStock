"""
R56 竞价快照采集脚本（盘前 9:20 / 9:24 两拍）

由调度器（pipeline_scheduler）调用或手动运行：
  python scripts/collect_auction_snapshots.py --snap snap1_920
  python scripts/collect_auction_snapshots.py --snap snap2_924

东财竞价接口（只读限频≥3s）：
  https://pushdata.eastmoney.com/api/qt/clist/get
  fs=m:0+t:6+f:!2,m:0+t:80+f:!2,m:1+t:2,m:1+t:23
  fields=f12,f14,f2,f3,f15,f20,f168,f170,f171,f172

快照落盘：F:/Aistock/data/data/runtime/l2_r56/auction_{snap}_{date}.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

# 项目路径
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.logger import get_logger
from utils.paths import runtime_path

logger = get_logger("collect_auction_snapshots")

BJ_TZ = ZoneInfo("Asia/Shanghai")
AUCTION_DIR = "l2_r56"

# 东财竞价行情接口
AUCTION_FS = "m:0+t:6+f:!2,m:0+t:80+f:!2,m:1+t:2,m:1+t:23"
AUCTION_FIELDS = "f12,f14,f2,f3,f15,f20,f168,f170,f171,f172"
AUCTION_URL = (
    "https://pushdata.eastmoney.com/api/qt/clist/get"
    f"?fs={AUCTION_FS}&po=1&pz=5000&pn=1&np=1&fltt=2"
    f"&fid=f3&fields={AUCTION_FIELDS}"
)


def fetch_auction_data() -> list[dict]:
    """从东财采集竞价快照。

    f12=代码, f14=名称, f2=最新价, f3=涨跌幅%,
    f15=最高价, f20=成交额, f168=竞买量, f170=竞卖量,
    f171=竞买均价, f172=竞卖均价
    """
    import requests

    logger.info(f"Fetching auction data from eastmoney...")
    resp = requests.get(
        AUCTION_URL,
        timeout=15,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    data = resp.json()
    items = ((data.get("data", {}) or {}).get("diff", []) or [])
    logger.info(f"Fetched {len(items)} auction items")

    results: list[dict] = []
    for item in items:
        try:
            code = str(item.get("f12") or "")
            name = str(item.get("f14") or "")
            price = _to_float(item.get("f2"))
            change_pct = _to_float(item.get("f3"))
            high = _to_float(item.get("f15"))
            amount = _to_float(item.get("f20"))
            bid_vol = int(item.get("f168") or 0)  # 竞买量
            ask_vol = int(item.get("f170") or 0)  # 竞卖量
            bid_avg_price = _to_float(item.get("f171"))
            ask_avg_price = _to_float(item.get("f172"))
        except (ValueError, TypeError):
            continue

        if not code:
            continue

        # gap_pct = change_pct at auction time
        gap_pct = change_pct
        auction_vol = bid_vol + ask_vol
        avg_price = bid_avg_price or ask_avg_price or price

        results.append({
            "code": code,
            "name": name,
            "price": price,
            "pre_close": _calc_pre_close(price, change_pct),
            "volume": 0,  # 竞价阶段无成交量
            "amount": amount or 0,
            "gap_pct": gap_pct,
            "auction_vol": auction_vol,
            "vol_ratio_30d": None,  # 需后续计算
        })

    return results


def _calc_pre_close(price: Optional[float], change_pct: Optional[float]) -> Optional[float]:
    """反推算昨收价。"""
    if price is None or change_pct is None or change_pct == 0:
        return price
    return round(price / (1 + change_pct / 100), 2)


def write_auction_csv(records: list[dict], snap_name: str) -> Path:
    """写竞价快照 CSV。"""
    snap_dir = runtime_path(AUCTION_DIR)
    snap_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(BJ_TZ)
    date_str = now.strftime("%Y-%m-%d")
    csv_path = snap_dir / f"auction_{snap_name}_{date_str}.csv"

    fieldnames = [
        "code", "name", "price", "pre_close", "volume",
        "amount", "gap_pct", "auction_vol", "vol_ratio_30d",
    ]

    with open(str(csv_path), "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)

    logger.info(f"Wrote {len(records)} records to {csv_path}")
    return csv_path


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def main():
    parser = argparse.ArgumentParser(description="竞价快照采集")
    parser.add_argument(
        "--snap",
        required=True,
        choices=["snap1_920", "snap2_924"],
        help="快照标识（snap1_920=9:20, snap2_924=9:24）",
    )
    args = parser.parse_args()

    # 限频保护
    logger.info(f"Starting auction snapshot: {args.snap}")
    time.sleep(1)

    records = fetch_auction_data()
    if not records:
        logger.error("No auction data fetched, aborting")
        sys.exit(1)

    csv_path = write_auction_csv(records, args.snap)
    print(f"Auction snapshot saved: {csv_path}")
    logger.info(f"Auction snapshot {args.snap} complete: {len(records)} stocks")


if __name__ == "__main__":
    main()