# -*- coding: utf-8 -*-
"""acct2 独立只读行情与宽度适配层。

职责：
  - 拉取 ETF/个股前复权日线并缓存到 runtime/acct2_managed/etf_cache/（不写 ClickHouse 正式库）。
  - 拉取腾讯实时盘口快照，转换为 LiveQuote。
  - 读取 runtime/t_tracker/regime_YYYYMMDD.json 获取市场宽度（above_ma20_pct）。
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from services.trading.acct2_managed_engine import (
    LiveQuote,
    MarketBar,
    MarketBreadth,
    normalize_code,
)


def to_tencent_symbol(code: str) -> str:
    """603881.SH -> sh603881, 159915.SZ -> sz159915。"""
    norm = normalize_code(code)
    digits, market = norm.split(".")
    return f"{market.lower()}{digits}"


def fetch_daily_bars_tencent(
    code: str,
    count: int = 45,
    cache_dir: Optional[Path] = None,
    trade_date: Optional[str] = None,
    timeout: float = 6.0,
) -> list[MarketBar]:
    """拉取前复权日线（优先读当日缓存，完全只读，不写正式库）。"""
    norm = normalize_code(code)
    today_tag = trade_date or datetime.now().strftime("%Y-%m-%d")
    cache_file = (
        cache_dir / f"{norm.replace('.', '_')}_{today_tag}.json" if cache_dir else None
    )
    if cache_file and cache_file.exists():
        try:
            raw_list = json.loads(cache_file.read_text(encoding="utf-8"))
            return [MarketBar(**row) for row in raw_list]
        except Exception:
            pass

    sym = to_tencent_symbol(norm)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{count},qfq"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    item = (payload.get("data") or {}).get(sym) or {}
    rows = item.get("qfqday") or item.get("day") or []
    bars: list[MarketBar] = []
    for row in rows:
        if len(row) < 6:
            continue
        bars.append(
            MarketBar(
                date=str(row[0]),
                open=float(row[1]),
                close=float(row[2]),
                high=float(row[3]),
                low=float(row[4]),
                volume=float(row[5]),
            )
        )

    if cache_file and bars:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps([b.__dict__ for b in bars], ensure_ascii=False),
            encoding="utf-8",
        )
    return bars


def fetch_realtime_quotes_tencent(
    codes: Iterable[str], timeout: float = 6.0
) -> dict[str, LiveQuote]:
    """批量拉取腾讯实时报价（返回 normalize_code -> LiveQuote）。"""
    norm_list = sorted({normalize_code(c) for c in codes if str(c).strip()})
    if not norm_list:
        return {}
    sym_to_norm = {to_tencent_symbol(c): c for c in norm_list}
    query = ",".join(sym_to_norm.keys())
    url = f"https://qt.gtimg.cn/q={query}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("gbk", errors="ignore")

    quotes: dict[str, LiveQuote] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        left, right = line.split("=", 1)
        sym = left.replace("v_", "").strip()
        norm = sym_to_norm.get(sym)
        if not norm:
            continue
        body = right.strip().strip(";").strip('"')
        parts = body.split("~")
        if len(parts) < 35:
            continue
        try:
            name = parts[1].strip()
            price = float(parts[3] or 0.0)
            prev_close = float(parts[4] or 0.0)
            open_px = float(parts[5] or 0.0)
            high_px = float(parts[33] or 0.0)
            low_px = float(parts[34] or 0.0)
            ts_raw = parts[30].strip() if len(parts) > 30 else ""
            # 粗算盘中均价（成交额万元/成交量手 -> 按价格口径兜底）
            vwap = None
            if len(parts) > 37 and parts[36] and parts[37]:
                vol_lots = float(parts[36] or 0.0)
                amt_wan = float(parts[37] or 0.0)
                if vol_lots > 0 and amt_wan > 0:
                    est = (amt_wan * 10000.0) / (vol_lots * 100.0)
                    if 0.5 * price <= est <= 1.5 * price:
                        vwap = round(est, 4)
            halted = price <= 0 or (open_px <= 0 and float(parts[6] or 0.0) <= 0)
            quotes[norm] = LiveQuote(
                code=norm,
                name=name,
                price=price if price > 0 else prev_close,
                prev_close=prev_close,
                open_price=open_px,
                high_price=high_px,
                low_price=low_px,
                vwap=vwap,
                ts=ts_raw,
                halted=halted,
            )
        except Exception:
            continue
    return quotes


def load_market_breadth(t_tracker_dir: Path, trade_date: str) -> MarketBreadth:
    """从 runtime/t_tracker/regime_YYYYMMDD.json 读取最新市场宽度。"""
    tag = trade_date.replace("-", "")
    exact = t_tracker_dir / f"regime_{tag}.json"
    target_file: Optional[Path] = exact if exact.exists() else None
    stale = False

    if target_file is None and t_tracker_dir.exists():
        candidates = sorted(t_tracker_dir.glob("regime_*.json"), reverse=True)
        if candidates:
            target_file = candidates[0]
            stale = True

    if target_file is None:
        return MarketBreadth(date=trade_date, above_ma20_pct=20.0, level="weak", stale=True)

    try:
        data = json.loads(target_file.read_text(encoding="utf-8"))
        br = data.get("breadth") or {}
        return MarketBreadth(
            date=str(br.get("date") or data.get("date") or trade_date),
            above_ma20_pct=float(br.get("above_ma20_pct") or 20.0),
            level=str(br.get("level") or "weak"),
            stale=stale,
        )
    except Exception:
        return MarketBreadth(date=trade_date, above_ma20_pct=20.0, level="weak", stale=True)
