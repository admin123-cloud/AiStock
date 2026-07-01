from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.probe_mainline_sector_stock_chain import (  # noqa: E402
    _fetch_index_features,
    _fetch_l2_sectors,
    _fetch_sector_history,
    _fetch_sector_members,
    _market_flags,
    _quote_list,
    _sector_rank_for_year,
)
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


OUT_ROOT = ROOT / "reports" / "mainline_sector_watchlist"


def _safe_float(value: Any) -> Any:
    try:
        x = float(value)
    except Exception:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, 6)


def _pct_text(value: Any) -> str:
    x = _safe_float(value)
    return "" if x is None else f"{x:.2%}"


def _latest_trade_date() -> str:
    df = clickhouse_query_df(
        """
        SELECT max(k.trade_date) AS trade_date
        FROM kline_daily k
        JOIN trade_calendar c
          ON c.trade_date = k.trade_date
         AND c.market = 'SH'
         AND c.is_trading = 1
        WHERE k.code LIKE '%.SH' OR k.code LIKE '%.SZ'
        """
    )
    if df.empty or pd.isna(df.iloc[0]["trade_date"]):
        raise RuntimeError("无法从 ClickHouse kline_daily 获取最新交易日")
    return str(pd.to_datetime(df.iloc[0]["trade_date"]).date())


def _fetch_closes_for_dates(codes: list[str], dates: list[str]) -> pd.DataFrame:
    if not codes or not dates:
        return pd.DataFrame(columns=["code", "trade_date", "close"])
    sql = f"""
    SELECT code, trade_date, close
    FROM kline_daily
    WHERE code IN ({_quote_list(codes)})
      AND trade_date IN ({_quote_list(dates)})
    """
    df = clickhouse_query_df(sql)
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna(subset=["trade_date", "close"]).copy()


def _build_rows(
    year: int,
    trade_date: str,
    top_sector_pct: float,
    laggard_rank_min: float,
    require_h20_positive: bool,
    require_h20_top50: bool,
    require_current_from_h20_positive: bool,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    sectors = _fetch_l2_sectors()
    hist = _fetch_sector_history(sectors)
    rank, meta = _sector_rank_for_year(hist, year)
    if rank is None or meta is None:
        raise RuntimeError(f"无法计算 {year} 年 L2 板块早期强度")

    sector_count = len(rank)
    top_n = max(5, min(15, int(round(sector_count * top_sector_pct))))
    top_rank = rank.sort_values("sector_rank").head(top_n).copy()
    top_rank["sector_bucket"] = "top"

    members = _fetch_sector_members(top_rank[["sector_code", "sector_name"]])
    codes = sorted(members["stock_code"].dropna().astype(str).unique().tolist())
    h20_date = str(meta["horizon_dates"]["h20"])
    dates = sorted({meta["first"], meta["anchor"], h20_date, trade_date})
    closes = _fetch_closes_for_dates(codes, dates)
    if closes.empty:
        raise RuntimeError("无法读取候选成分股关键日期收盘价")

    piv = closes.pivot_table(
        index="code",
        columns=closes["trade_date"].dt.strftime("%Y-%m-%d"),
        values="close",
        aggfunc="last",
    )

    base = members.merge(top_rank, on=["sector_code", "sector_name"], how="inner")
    rows: list[dict[str, Any]] = []
    for r in base.itertuples(index=False):
        code = str(r.stock_code)
        if code not in piv.index:
            continue
        vals = piv.loc[code]
        first_close = vals.get(meta["first"])
        anchor_close = vals.get(meta["anchor"])
        h20_close = vals.get(h20_date)
        trade_close = vals.get(trade_date)
        if any(pd.isna(x) or float(x) <= 0 for x in [first_close, anchor_close, h20_close, trade_close]):
            continue
        first_close = float(first_close)
        anchor_close = float(anchor_close)
        h20_close = float(h20_close)
        trade_close = float(trade_close)
        rows.append(
            {
                "trade_date": trade_date,
                "year": year,
                "anchor_date": meta["anchor"],
                "h20_date": h20_date,
                "sector_code": str(r.sector_code),
                "sector_name": str(r.sector_name),
                "sector_rank": int(r.sector_rank),
                "sector_early_ret": float(r.sector_early_ret),
                "stock_code": code,
                "stock_name": str(r.stock_name),
                "stock_early_ret": anchor_close / first_close - 1.0,
                "stock_h20_ret": h20_close / anchor_close - 1.0,
                "stock_current_from_anchor": trade_close / anchor_close - 1.0,
                "stock_current_from_h20": trade_close / h20_close - 1.0,
                "close": trade_close,
            }
        )

    out = pd.DataFrame(rows)
    if out.empty:
        return out, meta
    out["stock_early_rank_pct"] = out.groupby(["sector_code"])["stock_early_ret"].rank(ascending=False, pct=True)
    out["stock_h20_rank_pct"] = out.groupby(["sector_code"])["stock_h20_ret"].rank(ascending=False, pct=True)
    mask = out["stock_early_rank_pct"] >= laggard_rank_min
    if require_h20_positive:
        mask &= out["stock_h20_ret"] > 0
    if require_h20_top50:
        mask &= out["stock_h20_rank_pct"] <= 0.50
    if require_current_from_h20_positive:
        mask &= out["stock_current_from_h20"] > 0
    out = out[mask].copy()
    if out.empty:
        return out, meta
    out["candidate_score"] = (
        out["sector_early_ret"].rank(ascending=True, pct=True) * 0.35
        + out["stock_h20_ret"].rank(ascending=True, pct=True) * 0.35
        + out["stock_current_from_h20"].rank(ascending=True, pct=True) * 0.20
        + out["stock_early_rank_pct"] * 0.10
    )
    out = out.sort_values(["candidate_score", "sector_rank", "stock_h20_ret"], ascending=[False, True, False]).reset_index(drop=True)
    return out, meta


def _write_outputs(rows: pd.DataFrame, meta: dict[str, Any], market: dict[str, Any], args: argparse.Namespace) -> Path:
    out_dir = OUT_ROOT / str(args.trade_date)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "method": "mainline_l2_sector_laggard_h20_confirmation_watchlist",
        "trade_date": args.trade_date,
        "year": args.year,
        "parameters": {
            "top_sector_pct": args.top_sector_pct,
            "laggard_rank_min": args.laggard_rank_min,
            "require_h20_positive": args.require_h20_positive,
            "require_h20_top50": args.require_h20_top50,
            "require_current_from_h20_positive": args.require_current_from_h20_positive,
            "limit": args.limit,
        },
        "anchor": meta,
        "market": market,
        "count": int(len(rows)),
        "candidates": rows.head(args.limit).to_dict(orient="records") if not rows.empty else [],
    }
    (out_dir / "watchlist.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if not rows.empty:
        rows.to_csv(out_dir / "watchlist.csv", index=False, encoding="utf-8-sig")

    lines = [
        "# 主线板块资金方向观察池",
        "",
        f"- 交易日：{args.trade_date}",
        f"- 年份锚点：{meta['first']} -> {meta['anchor']}，20日确认日：{meta['horizon_dates']['h20']}",
        f"- 市场环境：中证1000 MA20={market.get('csi1000_above_ma20')}，上证 MA20={market.get('sse_above_ma20')}",
        f"- 候选数量：{len(rows)}",
        "",
        "## 说明",
        "",
        "- 这是研究观察池，不是正式买点，不接自动下单。",
        "- 逻辑：强 L2 板块中，先找锚点前相对落后的成分股，再要求 20 个交易日后出现转强确认。",
        "- 当前仍使用 TDXQuant 当前成分映射，存在非 point-in-time 回看偏差。",
        "",
        "## 候选前排",
        "",
        "| 排名 | 代码 | 名称 | 板块 | 板块排名 | 早期落后排名% | 20日强度排名% | 锚点后收益 | 20日后收益 | 分数 |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for i, r in rows.head(args.limit).iterrows():
        lines.append(
            f"| {i + 1} | {r['stock_code']} | {r['stock_name']} | {r['sector_name']} | {int(r['sector_rank'])} | "
            f"{_pct_text(r['stock_early_rank_pct'])} | {_pct_text(r['stock_h20_rank_pct'])} | "
            f"{_pct_text(r['stock_current_from_anchor'])} | {_pct_text(r['stock_current_from_h20'])} | {float(r['candidate_score']):.4f} |"
        )
    (out_dir / "watchlist.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Build mainline L2 sector capital-direction watchlist.")
    parser.add_argument("--trade-date", default="", help="默认使用 ClickHouse kline_daily 最新交易日")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--top-sector-pct", type=float, default=0.10)
    parser.add_argument("--laggard-rank-min", type=float, default=0.70)
    parser.add_argument("--require-h20-positive", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--require-h20-top50", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--require-current-from-h20-positive", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--limit", type=int, default=80)
    args = parser.parse_args()

    args.trade_date = args.trade_date or _latest_trade_date()
    rows, meta = _build_rows(
        year=args.year,
        trade_date=args.trade_date,
        top_sector_pct=args.top_sector_pct,
        laggard_rank_min=args.laggard_rank_min,
        require_h20_positive=args.require_h20_positive,
        require_h20_top50=args.require_h20_top50,
        require_current_from_h20_positive=args.require_current_from_h20_positive,
    )
    index_features = _fetch_index_features(args.trade_date)
    market = _market_flags(index_features, args.trade_date)
    out_dir = _write_outputs(rows, meta, market, args)
    print(json.dumps({"trade_date": args.trade_date, "count": int(len(rows)), "out_dir": str(out_dir)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
