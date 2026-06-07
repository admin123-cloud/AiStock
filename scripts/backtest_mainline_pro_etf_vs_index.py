from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

import akshare as ak
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df  # noqa: E402


OUT_DIR = ROOT / "reports" / "mainline_pro_etf_vs_index"
CACHE_DIR = ROOT / "data" / "runtime" / "mainline_pro_akshare_cache"
OUT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

START = "2006-03-01"
END = "2026-06-02"

VIDEO_KEYWORDS = [
    "能源",
    "煤炭",
    "银行",
    "低波",
    "半导体",
    "芯片",
    "人工智能",
    "信息",
    "信息安全",
    "信创",
    "游戏",
    "工业",
    "工业4.0",
    "通信",
    "计算机",
    "软件",
    "传媒",
    "互联网",
    "电子",
    "军工",
    "证券",
    "有色",
    "新能源",
    "医药",
    "创新药",
    "消费",
    "家电",
    "中证800",
    "中证500",
    "中证1000",
    "沪深300",
    "上证50",
    "创业板",
    "科创",
    "红利",
]

EXCLUDE_ETF_WORDS = ["债", "货币", "现金", "黄金", "豆粕", "原油", "纳指", "标普", "日经", "德国", "法国", "亚太"]
EXCLUDE_INDEX_WORDS = ["港", "HK", "USD", "美元", "日经", "标普", "纳斯达克", "中韩", "海外"]
FALLBACK_CSINDEX_CODES = [
    "000300",
    "000905",
    "000906",
    "000852",
    "000016",
    "000903",
    "000904",
    "000908",
    "000909",
    "000910",
    "000911",
    "000912",
    "000913",
    "000914",
    "000915",
    "000916",
    "000917",
    "000928",
    "000929",
    "000930",
    "000931",
    "000932",
    "000933",
    "000934",
    "000935",
    "000936",
    "000937",
    "000986",
    "000987",
    "000988",
    "000989",
    "000990",
    "000991",
    "000992",
    "000993",
    "000994",
    "000995",
    "000820",
    "000941",
    "399986",
    "399987",
    "000811",
    "000813",
    "000814",
]


def _set_network_env() -> None:
    os.environ["HTTP_PROXY"] = ""
    os.environ["HTTPS_PROXY"] = ""
    os.environ["NO_PROXY"] = "*"


def _safe_float(value: Any) -> Any:
    try:
        x = float(value)
    except Exception:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, 6)


def _pct(value: Any) -> str:
    x = _safe_float(value)
    return "" if x is None else f"{x:.2%}"


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8")


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path, index=False, encoding="utf-8")


def _fetch_etf_list() -> pd.DataFrame:
    cache = CACHE_DIR / "etf_category_sina.csv"
    if cache.exists():
        return _read_csv(cache)
    df = ak.fund_etf_category_sina(symbol="ETF基金")
    _write_csv(df, cache)
    return df


def _col(df: pd.DataFrame, name: str, fallback_idx: int) -> str:
    for c in df.columns:
        if name in str(c):
            return str(c)
    return str(df.columns[fallback_idx])


def _select_etf_candidates(limit: int = 52) -> pd.DataFrame:
    df = _fetch_etf_list().copy()
    code_col = _col(df, "代码", 0)
    name_col = _col(df, "名称", 1)
    amount_col = _col(df, "成交额", len(df.columns) - 1)
    df["code"] = df[code_col].astype(str)
    df["name"] = df[name_col].astype(str)
    df["amount"] = pd.to_numeric(df[amount_col], errors="coerce").fillna(0.0)
    text = df["name"]
    mask = text.apply(lambda x: any(k in x for k in VIDEO_KEYWORDS))
    mask &= ~text.apply(lambda x: any(k in x for k in EXCLUDE_ETF_WORDS))
    use = df[mask].sort_values("amount", ascending=False).head(limit).copy()
    return use[["code", "name", "amount"]]


def _fetch_etf_history(code: str) -> pd.DataFrame | None:
    cache = CACHE_DIR / "etf_hist" / f"{code}.csv"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        return _read_csv(cache)
    try:
        df = ak.fund_etf_hist_sina(symbol=code)
        if df.empty:
            return None
        _write_csv(df, cache)
        time.sleep(0.15)
        return df
    except Exception:
        return None


def _normalize_price(df: pd.DataFrame, code: str, name: str, source: str) -> pd.DataFrame:
    date_col = "date" if "date" in df.columns else _col(df, "日期", 0)
    close_col = "close" if "close" in df.columns else _col(df, "收盘", min(4, len(df.columns) - 1))
    out = pd.DataFrame(
        {
            "code": code,
            "name": name,
            "source": source,
            "date": pd.to_datetime(df[date_col], errors="coerce"),
            "close": pd.to_numeric(df[close_col], errors="coerce"),
        }
    )
    return out.dropna(subset=["date", "close"]).query("close > 0").sort_values("date")


def _load_true_etf_prices() -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    candidates = _select_etf_candidates(52)
    rows: list[pd.DataFrame] = []
    meta: list[dict[str, Any]] = []
    for r in candidates.itertuples(index=False):
        hist = _fetch_etf_history(str(r.code))
        ok = hist is not None and not hist.empty
        if ok:
            px = _normalize_price(hist, str(r.code), str(r.name), "true_etf_sina")
            rows.append(px)
            meta.append(
                {
                    "code": str(r.code),
                    "name": str(r.name),
                    "amount": _safe_float(r.amount),
                    "rows": int(len(px)),
                    "first_date": str(px["date"].min().date()),
                    "last_date": str(px["date"].max().date()),
                }
            )
        else:
            meta.append({"code": str(r.code), "name": str(r.name), "amount": _safe_float(r.amount), "rows": 0})
    return (pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()), meta


def _load_local_index_prices() -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    clauses = " OR ".join([f"s.name LIKE '%{k}%'" for k in VIDEO_KEYWORDS])
    sql = f"""
    SELECT
        k.code,
        any(s.name) AS name,
        min(k.trade_date) AS first_date,
        max(k.trade_date) AS last_date,
        count() AS rows
    FROM kline_daily k
    INNER JOIN stocks s ON s.code = k.code
    WHERE s.type = 'index'
      AND ({clauses})
    GROUP BY k.code
    ORDER BY rows DESC
    LIMIT 52
    """
    meta_df = clickhouse_query_df(sql)
    if meta_df.empty:
        return pd.DataFrame(), []
    codes = meta_df["code"].astype(str).tolist()
    quoted = ",".join("'" + c + "'" for c in codes)
    px_sql = f"""
    SELECT k.code, any(s.name) AS name, k.trade_date AS date, any(k.close) AS close
    FROM kline_daily k
    INNER JOIN stocks s ON s.code = k.code
    WHERE k.code IN ({quoted})
      AND k.trade_date >= '{START}'
      AND k.trade_date <= '{END}'
      AND k.close > 0
    GROUP BY k.code, k.trade_date
    ORDER BY k.code, k.trade_date
    """
    px = clickhouse_query_df(px_sql)
    if not px.empty:
        px["source"] = "local_index_clickhouse"
        px["date"] = pd.to_datetime(px["date"], errors="coerce")
        px["close"] = pd.to_numeric(px["close"], errors="coerce")
    return px, json.loads(meta_df.astype(str).to_json(orient="records", force_ascii=False))


def _fetch_csindex_spot() -> pd.DataFrame:
    cache = CACHE_DIR / "csindex_spot_filtered_source.csv"
    if cache.exists():
        return _read_csv(cache)
    frames: list[pd.DataFrame] = []
    for symbol in ["中证系列指数", "沪深重要指数", "上证系列指数", "深证系列指数"]:
        try:
            df = ak.stock_zh_index_spot_em(symbol=symbol)
            df["series"] = symbol
            frames.append(df)
            time.sleep(0.3)
        except Exception:
            continue
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not out.empty:
        _write_csv(out, cache)
    return out


def _select_csindex_candidates(limit: int = 52) -> pd.DataFrame:
    df = _fetch_csindex_spot().copy()
    if df.empty:
        return pd.DataFrame(
            {
                "code": FALLBACK_CSINDEX_CODES[:limit],
                "name": FALLBACK_CSINDEX_CODES[:limit],
                "amount": 0.0,
                "series": "fallback_manual_video_like",
            }
        )
    code_col = _col(df, "代码", 1 if len(df.columns) > 1 else 0)
    name_col = _col(df, "名称", 2 if len(df.columns) > 2 else 1)
    amount_col = _col(df, "成交额", 7 if len(df.columns) > 7 else len(df.columns) - 1)
    df["code"] = df[code_col].astype(str)
    df["name"] = df[name_col].astype(str)
    df["amount"] = pd.to_numeric(df[amount_col], errors="coerce").fillna(0.0)
    text = df["name"]
    mask = text.apply(lambda x: any(k in x for k in VIDEO_KEYWORDS))
    mask &= ~text.apply(lambda x: any(k in x for k in EXCLUDE_INDEX_WORDS))
    use = df[mask].drop_duplicates("code").sort_values("amount", ascending=False).head(limit).copy()
    return use[["code", "name", "amount", "series"]]


def _fetch_csindex_history(code: str) -> pd.DataFrame | None:
    cache = CACHE_DIR / "csindex_hist" / f"{code}.csv"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        return _read_csv(cache)
    try:
        df = ak.stock_zh_index_hist_csindex(symbol=code, start_date="20060301", end_date="20260602")
        if df.empty:
            return None
        _write_csv(df, cache)
        time.sleep(0.15)
        return df
    except Exception:
        return None


def _normalize_csindex_history(df: pd.DataFrame, code: str, fallback_name: str) -> pd.DataFrame:
    date_col = _col(df, "日期", 0)
    close_col = _col(df, "收盘", 9 if len(df.columns) > 9 else len(df.columns) - 1)
    name_col = _col(df, "简称", 3 if len(df.columns) > 3 else 2)
    name_series = df[name_col].astype(str) if name_col in df.columns else fallback_name
    out = pd.DataFrame(
        {
            "code": code,
            "name": name_series,
            "source": "csindex_backfilled_index",
            "date": pd.to_datetime(df[date_col], errors="coerce"),
            "close": pd.to_numeric(df[close_col], errors="coerce"),
        }
    )
    return out.dropna(subset=["date", "close"]).query("close > 0").sort_values("date")


def _load_csindex_prices(limit: int = 52) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    candidates = _select_csindex_candidates(limit)
    rows: list[pd.DataFrame] = []
    meta: list[dict[str, Any]] = []
    for r in candidates.itertuples(index=False):
        hist = _fetch_csindex_history(str(r.code))
        if hist is None or hist.empty:
            meta.append({"code": str(r.code), "name": str(r.name), "rows": 0, "amount": _safe_float(r.amount)})
            continue
        px = _normalize_csindex_history(hist, str(r.code), str(r.name))
        rows.append(px)
        meta.append(
            {
                "code": str(r.code),
                "name": str(px["name"].iloc[-1] if not px.empty else r.name),
                "amount": _safe_float(r.amount),
                "series": str(r.series),
                "rows": int(len(px)),
                "first_date": str(px["date"].min().date()),
                "last_date": str(px["date"].max().date()),
            }
        )
    return (pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()), meta


def _close_on_or_before(pivot: pd.DataFrame, date: pd.Timestamp) -> pd.Series | None:
    use = pivot[pivot.index <= date]
    if use.empty:
        return None
    return use.iloc[-1]


def _backtest(prices: pd.DataFrame, pool_name: str, anchor_month: int, anchor_day: int, top_n: int = 3) -> dict[str, Any]:
    if prices.empty:
        return {"pool": pool_name, "anchor": f"{anchor_month:02d}-{anchor_day:02d}", "error": "empty prices"}
    pivot = prices.pivot_table(index="date", columns="code", values="close", aggfunc="last").sort_index()
    names = prices.groupby("code")["name"].last().astype(str).to_dict()
    rets: list[float] = []
    yearly: list[dict[str, Any]] = []
    for year in range(2006, 2027):
        first = pd.Timestamp(f"{year}-01-10")
        anchor = pd.Timestamp(year=year, month=anchor_month, day=anchor_day)
        end = pd.Timestamp(f"{year}-12-31")
        c0 = _close_on_or_before(pivot, first)
        ca = _close_on_or_before(pivot, anchor)
        ce = _close_on_or_before(pivot, end)
        if c0 is None or ca is None or ce is None:
            continue
        frame = pd.DataFrame({"early": ca / c0 - 1.0, "hold": ce / ca - 1.0})
        frame = frame.replace([math.inf, -math.inf], pd.NA).dropna()
        if len(frame) < top_n:
            continue
        top = frame.nlargest(top_n, "early")
        ret = float(top["hold"].mean())
        rets.append(ret)
        yearly.append(
            {
                "year": year,
                "available_count": int(len(frame)),
                "ret": _safe_float(ret),
                "top": [
                    {
                        "code": str(code),
                        "name": names.get(str(code), ""),
                        "early": _safe_float(row.early),
                        "hold": _safe_float(row.hold),
                    }
                    for code, row in top.iterrows()
                ],
            }
        )
    equity = math.prod(1 + r for r in rets) if rets else None
    return {
        "pool": pool_name,
        "anchor": f"{anchor_month:02d}-{anchor_day:02d}",
        "top_n": top_n,
        "years": len(rets),
        "equity": _safe_float(equity),
        "total_return": _safe_float(None if equity is None else equity - 1.0),
        "avg_year_ret": _safe_float(pd.Series(rets).mean() if rets else None),
        "positive_years": int((pd.Series(rets) > 0).sum()) if rets else 0,
        "yearly": yearly,
    }


def _backtest_window(
    prices: pd.DataFrame,
    pool_name: str,
    anchor_month: int,
    anchor_day: int,
    rank_months: int,
    horizon: str,
    top_n: int = 3,
) -> dict[str, Any]:
    if prices.empty:
        return {"pool": pool_name, "anchor": f"{anchor_month:02d}-{anchor_day:02d}", "error": "empty prices"}
    pivot = prices.pivot_table(index="date", columns="code", values="close", aggfunc="last").sort_index()
    names = prices.groupby("code")["name"].last().astype(str).to_dict()
    rets: list[float] = []
    yearly: list[dict[str, Any]] = []
    for year in range(2007, 2026):
        anchor = pd.Timestamp(year=year, month=anchor_month, day=anchor_day)
        rank_start = anchor - pd.DateOffset(months=rank_months)
        if horizon == "year_end":
            end = pd.Timestamp(f"{year}-12-31")
        elif horizon == "next_anchor":
            end = pd.Timestamp(year=year + 1, month=anchor_month, day=anchor_day)
        else:
            raise ValueError(horizon)
        c0 = _close_on_or_before(pivot, rank_start)
        ca = _close_on_or_before(pivot, anchor)
        ce = _close_on_or_before(pivot, end)
        if c0 is None or ca is None or ce is None:
            continue
        frame = pd.DataFrame({"rank_ret": ca / c0 - 1.0, "hold": ce / ca - 1.0})
        frame = frame.replace([math.inf, -math.inf], pd.NA).dropna()
        if len(frame) < top_n:
            continue
        top = frame.nlargest(top_n, "rank_ret")
        ret = float(top["hold"].mean())
        rets.append(ret)
        yearly.append(
            {
                "year": year,
                "available_count": int(len(frame)),
                "ret": _safe_float(ret),
                "top": [
                    {
                        "code": str(code),
                        "name": names.get(str(code), ""),
                        "rank_ret": _safe_float(row.rank_ret),
                        "hold": _safe_float(row.hold),
                    }
                    for code, row in top.iterrows()
                ],
            }
        )
    equity = math.prod(1 + r for r in rets) if rets else None
    return {
        "pool": pool_name,
        "anchor": f"{anchor_month:02d}-{anchor_day:02d}",
        "rank_months": rank_months,
        "horizon": horizon,
        "top_n": top_n,
        "years": len(rets),
        "equity": _safe_float(equity),
        "total_return": _safe_float(None if equity is None else equity - 1.0),
        "avg_year_ret": _safe_float(pd.Series(rets).mean() if rets else None),
        "positive_years": int((pd.Series(rets) > 0).sum()) if rets else 0,
        "yearly": yearly,
    }


def _write_report(result: dict[str, Any]) -> None:
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 13pro ETF 与指数口径复核",
        "",
        "## 方法",
        "",
        "- 真实 ETF 池：从新浪 ETF 当前列表按视频关键词筛选 52 只，历史使用 `fund_etf_hist_sina`，只从 ETF 实际上市日期后参与年度排名。",
        "- 疑似指数池：从本地 ClickHouse 指数表按视频关键词筛选最多 52 只指数，使用指数历史数据参与年度排名。",
        "- 规则：每年年初到锚点日计算涨幅，选涨幅最大 Top3，锚点后等权持有到当年年末。",
        "- 锚点：分别测试 2月底、3月底、4月底。",
        "",
        "## 汇总",
        "",
        "| 池 | 锚点 | 年数 | 净值倍数 | 总收益 | 年均收益 | 正收益年数 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for bt in result["backtests"]:
        lines.append(
            f"| {bt['pool']} | {bt['anchor']} | {bt.get('years', 0)} | {bt.get('equity') or ''} | "
            f"{_pct(bt.get('total_return'))} | {_pct(bt.get('avg_year_ret'))} | {bt.get('positive_years', '')} |"
        )
    lines.extend(["", "## 排名窗口矩阵最佳 Top30", ""])
    lines.append("| 排名 | 池 | 锚点 | 排名窗口 | 持有 | 年数 | 净值倍数 | 总收益 | 年均收益 |")
    lines.append("|---:|---|---|---:|---|---:|---:|---:|---:|")
    for i, bt in enumerate(result.get("matrix_best_30", []), start=1):
        lines.append(
            f"| {i} | {bt['pool']} | {bt['anchor']} | {bt.get('rank_months')}M | {bt.get('horizon')} | "
            f"{bt.get('years', 0)} | {bt.get('equity') or ''} | {_pct(bt.get('total_return'))} | {_pct(bt.get('avg_year_ret'))} |"
        )
    lines.extend(["", "## 数据覆盖", ""])
    lines.append(f"- 真实 ETF 候选：{len(result['true_etf_meta'])} 只。")
    listed_after_2018 = [x for x in result["true_etf_meta"] if x.get("first_date", "9999") >= "2018-01-01"]
    lines.append(f"- 真实 ETF 中 2018 年后才有历史的：{len(listed_after_2018)} 只。")
    lines.append(f"- 本地疑似指数候选：{len(result['local_index_meta'])} 只。")
    lines.append(f"- 中证/沪深指数回填候选：{len(result['csindex_meta'])} 只。")
    lines.extend(["", "## 真实 ETF 前 20 只覆盖", ""])
    lines.append("| 代码 | 名称 | 起始 | 结束 | 行数 |")
    lines.append("|---|---|---|---|---:|")
    for item in result["true_etf_meta"][:20]:
        lines.append(
            f"| {item.get('code')} | {item.get('name')} | {item.get('first_date', '')} | "
            f"{item.get('last_date', '')} | {item.get('rows', 0)} |"
        )
    lines.extend(["", "## 关键判断", ""])
    lines.append("真实 ETF 池与视频 `2006-03-01` 起测天然不一致，因为大量主题 ETF 在 2020 年后才上市。")
    lines.append("如果指数池结果明显强于真实 ETF 池，视频收益更可能来自主题指数历史回填或当前幸存标的池，而不是严格可交易 ETF。")
    (OUT_DIR / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    _set_network_env()
    etf_prices, etf_meta = _load_true_etf_prices()
    index_prices, index_meta = _load_local_index_prices()
    csindex_prices, csindex_meta = _load_csindex_prices(52)
    csindex200_prices, csindex200_meta = _load_csindex_prices(200)
    backtests: list[dict[str, Any]] = []
    for anchor in [(2, 28), (3, 31), (4, 30)]:
        backtests.append(_backtest(etf_prices, "true_etf_listed_only", anchor[0], anchor[1]))
        backtests.append(_backtest(index_prices, "local_theme_index", anchor[0], anchor[1]))
        backtests.append(_backtest(csindex_prices, "csindex_backfilled_theme_index", anchor[0], anchor[1]))
        backtests.append(_backtest(csindex200_prices, "csindex_backfilled_keyword200", anchor[0], anchor[1]))
    matrix: list[dict[str, Any]] = []
    for pool_name, px in [
        ("true_etf_listed_only", etf_prices),
        ("local_theme_index", index_prices),
        ("csindex_backfilled_theme_index", csindex_prices),
        ("csindex_backfilled_keyword200", csindex200_prices),
    ]:
        for anchor in [(2, 28), (3, 31), (4, 30)]:
            for rank_months in [1, 2, 3, 6, 12]:
                for horizon in ["year_end", "next_anchor"]:
                    matrix.append(_backtest_window(px, pool_name, anchor[0], anchor[1], rank_months, horizon))
    result = {
        "method": "year_start_to_anchor_top3_hold_to_year_end",
        "start": START,
        "end": END,
        "true_etf_meta": etf_meta,
        "local_index_meta": index_meta,
        "csindex_meta": csindex_meta,
        "csindex200_meta": csindex200_meta,
        "backtests": backtests,
        "matrix_best_30": sorted(matrix, key=lambda x: float(x.get("equity") or 0.0), reverse=True)[:30],
        "matrix": matrix,
    }
    _write_report(result)
    print(json.dumps([{k: bt.get(k) for k in ["pool", "anchor", "years", "equity", "total_return"]} for bt in backtests], ensure_ascii=False, indent=2))
    print(str(OUT_DIR / "summary.md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
