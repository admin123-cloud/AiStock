from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_build_four_path_candidates import INDEX_CODE, _add_index_features  # noqa: E402
from scripts.backtest_g3_five_strategies_from_scratch_v1 import _md_table, _pct  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("index_mom5_overheat_audit_v1")
TRADES_PATH = report_path(
    "g2_g3_market_style_router_v1",
    "g3_final_with_g2_gap_supplement_closed_trades.csv",
)


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value) if math.isfinite(float(value)) else None
    if pd.isna(value):
        return None
    return str(value)


def _load_index(start_date: str, end_date: str) -> pd.DataFrame:
    df = clickhouse_query_df(
        """
        SELECT trade_date, open, high, low, close, volume, amount, turnover_rate
        FROM kline_daily
        WHERE code = %(index_code)s
          AND trade_date BETWEEN subtractDays(toDate(%(start_date)s), 260) AND toDate(%(end_date)s)
        ORDER BY trade_date
        """,
        {"index_code": INDEX_CODE, "start_date": start_date, "end_date": end_date},
    )
    if df.empty:
        raise RuntimeError(f"No index daily data for {INDEX_CODE}")
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close", "volume", "amount", "turnover_rate"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = (
        df.dropna(subset=["trade_date", "close"])
        .sort_values(["trade_date"])
        .drop_duplicates(subset=["trade_date"], keep="last")
        .reset_index(drop=True)
    )
    d = _add_index_features(df).copy()
    d["mom5"] = pd.to_numeric(d["close"], errors="coerce") / pd.to_numeric(d["close"], errors="coerce").shift(5) - 1.0
    d["mom10"] = pd.to_numeric(d["close"], errors="coerce") / pd.to_numeric(d["close"], errors="coerce").shift(10) - 1.0
    d = d[(d["trade_date"] >= start_date) & (d["trade_date"] <= end_date)].copy()
    for n in [5, 10, 20]:
        d[f"fwd_ret_{n}d"] = d["close"].shift(-n) / d["close"] - 1.0
        future = pd.concat([(d["close"].shift(-i) / d["close"] - 1.0).rename(i) for i in range(1, n + 1)], axis=1)
        d[f"fwd_max_dd_{n}d"] = future.min(axis=1)
        d[f"fwd_max_up_{n}d"] = future.max(axis=1)
    return d.replace([np.inf, -np.inf], np.nan)


def _load_breadth(start_date: str, end_date: str) -> pd.DataFrame:
    df = clickhouse_query_df(
        """
        SELECT
            trade_date,
            count() AS stock_count,
            avg(if(ret1 > 0, 1, 0)) AS up_rate,
            avg(if(ret1 <= -0.05, 1, 0)) AS big_down_rate,
            avg(if(ret1 <= -0.095, 1, 0)) AS limit_down_proxy_rate,
            quantile(0.5)(ret1) AS median_ret
        FROM (
            SELECT
                k.code AS code,
                k.trade_date AS trade_date,
                k.close / lagInFrame(k.close) OVER (
                    PARTITION BY k.code
                    ORDER BY k.trade_date
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) - 1 AS ret1
            FROM kline_daily AS k
            INNER JOIN stocks AS s ON s.code = k.code
            WHERE s.type = 'stock'
              AND COALESCE(s.st, 0) = 0
              AND COALESCE(s.quit, 0) = 0
              AND k.trade_date BETWEEN subtractDays(toDate(%(start_date)s), 20) AND toDate(%(end_date)s)
        )
        WHERE isFinite(ret1)
          AND trade_date BETWEEN toDate(%(start_date)s) AND toDate(%(end_date)s)
        GROUP BY trade_date
        ORDER BY trade_date
        """,
        {"start_date": start_date, "end_date": end_date},
    )
    if df.empty:
        raise RuntimeError("No market breadth data")
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["stock_count", "up_rate", "big_down_rate", "limit_down_proxy_rate", "median_ret"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for n in [5, 10, 20]:
        df[f"fwd_up_rate_chg_{n}d"] = df["up_rate"].shift(-n) - df["up_rate"]
        df[f"fwd_big_down_chg_{n}d"] = df["big_down_rate"].shift(-n) - df["big_down_rate"]
    return df


def _summarize_group(d: pd.DataFrame, group_col: str) -> pd.DataFrame:
    rows = []
    for key, g in d.groupby(group_col, observed=False, dropna=False):
        if g.empty:
            continue
        rows.append(
            {
                group_col: str(key),
                "days": int(len(g)),
                "avg_mom5": float(g["mom5"].mean()),
                "avg_up_rate": float(g["up_rate"].mean()),
                "avg_big_down": float(g["big_down_rate"].mean()),
                "fwd5_ret": float(g["fwd_ret_5d"].mean()),
                "fwd10_ret": float(g["fwd_ret_10d"].mean()),
                "fwd20_ret": float(g["fwd_ret_20d"].mean()),
                "fwd10_max_dd": float(g["fwd_max_dd_10d"].mean()),
                "fwd20_max_dd": float(g["fwd_max_dd_20d"].mean()),
                "hit_dd10_le3": float((g["fwd_max_dd_10d"] <= -0.03).mean()),
                "hit_dd20_le5": float((g["fwd_max_dd_20d"] <= -0.05).mean()),
                "fwd10_up_rate_chg": float(g["fwd_up_rate_chg_10d"].mean()),
                "fwd20_up_rate_chg": float(g["fwd_up_rate_chg_20d"].mean()),
            }
        )
    return pd.DataFrame(rows)


def _threshold_table(d: pd.DataFrame) -> pd.DataFrame:
    base_dd10 = float((d["fwd_max_dd_10d"] <= -0.03).mean())
    base_dd20 = float((d["fwd_max_dd_20d"] <= -0.05).mean())
    rows = []
    for thr in [-0.01, 0.0, 0.01, 0.02, 0.03, 0.04, 0.05]:
        g = d[pd.to_numeric(d["mom5"], errors="coerce") >= thr]
        rows.append(
            {
                "mom5_ge": thr,
                "days": int(len(g)),
                "coverage": float(len(g) / len(d)) if len(d) else 0.0,
                "avg_current_up_rate": float(g["up_rate"].mean()) if len(g) else np.nan,
                "avg_fwd10_ret": float(g["fwd_ret_10d"].mean()) if len(g) else np.nan,
                "avg_fwd20_ret": float(g["fwd_ret_20d"].mean()) if len(g) else np.nan,
                "hit_dd10_le3": float((g["fwd_max_dd_10d"] <= -0.03).mean()) if len(g) else np.nan,
                "lift_dd10": (float((g["fwd_max_dd_10d"] <= -0.03).mean()) / base_dd10) if len(g) and base_dd10 else np.nan,
                "hit_dd20_le5": float((g["fwd_max_dd_20d"] <= -0.05).mean()) if len(g) else np.nan,
                "lift_dd20": (float((g["fwd_max_dd_20d"] <= -0.05).mean()) / base_dd20) if len(g) and base_dd20 else np.nan,
                "avg_fwd10_up_rate_chg": float(g["fwd_up_rate_chg_10d"].mean()) if len(g) else np.nan,
            }
        )
    return pd.DataFrame(rows)


def _correlations(d: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "up_rate",
        "big_down_rate",
        "median_ret",
        "fwd_ret_5d",
        "fwd_ret_10d",
        "fwd_ret_20d",
        "fwd_max_dd_10d",
        "fwd_max_dd_20d",
        "fwd_up_rate_chg_10d",
        "fwd_big_down_chg_10d",
    ]
    rows = []
    for col in cols:
        sub = d[["mom5", col]].dropna()
        rows.append({"target": col, "corr_with_mom5": float(sub["mom5"].corr(sub[col])) if len(sub) >= 10 else np.nan})
    return pd.DataFrame(rows)


def _trade_bucket_table(market: pd.DataFrame) -> pd.DataFrame:
    if not TRADES_PATH.exists():
        return pd.DataFrame()
    trades = pd.read_csv(TRADES_PATH, encoding="utf-8-sig")
    if trades.empty or "context_date" not in trades.columns:
        return pd.DataFrame()
    trades["context_date"] = pd.to_datetime(trades["context_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    ctx = market[["trade_date", "mom5", "mom5_bucket"]].rename(columns={"trade_date": "context_date", "mom5": "index_mom5"})
    d = trades.merge(ctx, on="context_date", how="left")
    d["net_ret"] = pd.to_numeric(d.get("net_ret"), errors="coerce")
    d["realized_pnl"] = pd.to_numeric(d.get("realized_pnl"), errors="coerce")
    rows = []
    for key, g in d.dropna(subset=["index_mom5"]).groupby("mom5_bucket", observed=False):
        rows.append(
            {
                "mom5_bucket": str(key),
                "trades": int(len(g)),
                "avg_ret": float(g["net_ret"].mean()),
                "win_rate": float((g["net_ret"] > 0).mean()),
                "bad10_rate": float((g["net_ret"] <= -0.10).mean()),
                "sum_pnl": float(g["realized_pnl"].sum()),
                "mainwave_trades": int((g["route"].astype(str).eq("score120_core") | g["mode"].astype(str).eq("institutional_mainwave")).sum())
                if "route" in g.columns and "mode" in g.columns
                else 0,
            }
        )
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit index mom5 as an overheat proxy.")
    parser.add_argument("--start-date", default="2015-01-01")
    parser.add_argument("--end-date", default="2026-06-19")
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    index = _load_index(args.start_date, args.end_date)
    breadth = _load_breadth(args.start_date, args.end_date)
    market = index.merge(breadth, on="trade_date", how="left")
    market = market.dropna(subset=["mom5", "up_rate", "fwd_ret_10d", "fwd_max_dd_20d"]).copy()
    market["mom5_bucket"] = pd.cut(
        market["mom5"],
        bins=[-np.inf, -0.03, -0.01, 0.01, 0.03, 0.05, np.inf],
        labels=["<=-3%", "-3~-1%", "-1~1%", "1~3%", "3~5%", ">5%"],
        right=True,
    )
    market["mom5_quantile"] = pd.qcut(market["mom5"], 5, labels=["Q1最低", "Q2", "Q3", "Q4", "Q5最高"], duplicates="drop")

    bucket = _summarize_group(market, "mom5_bucket")
    quantile = _summarize_group(market, "mom5_quantile")
    threshold = _threshold_table(market)
    corr = _correlations(market)
    trade_bucket = _trade_bucket_table(market)

    market.to_csv(out_dir / "index_mom5_daily_audit.csv", index=False, encoding="utf-8-sig")
    bucket.to_csv(out_dir / "mom5_bucket_summary.csv", index=False, encoding="utf-8-sig")
    quantile.to_csv(out_dir / "mom5_quantile_summary.csv", index=False, encoding="utf-8-sig")
    threshold.to_csv(out_dir / "mom5_threshold_precision.csv", index=False, encoding="utf-8-sig")
    corr.to_csv(out_dir / "mom5_correlations.csv", index=False, encoding="utf-8-sig")
    if not trade_bucket.empty:
        trade_bucket.to_csv(out_dir / "g3_trade_return_by_index_mom5.csv", index=False, encoding="utf-8-sig")

    latest = market.sort_values("trade_date").tail(1).iloc[0].to_dict()
    summary = {
        "start_date": args.start_date,
        "end_date": args.end_date,
        "rows": int(len(market)),
        "latest": latest,
        "base_dd10_le3": float((market["fwd_max_dd_10d"] <= -0.03).mean()),
        "base_dd20_le5": float((market["fwd_max_dd_20d"] <= -0.05).mean()),
        "output_dir": str(out_dir),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")

    pct_cols = {
        "avg_mom5",
        "avg_up_rate",
        "avg_big_down",
        "fwd5_ret",
        "fwd10_ret",
        "fwd20_ret",
        "fwd10_max_dd",
        "fwd20_max_dd",
        "hit_dd10_le3",
        "hit_dd20_le5",
        "fwd10_up_rate_chg",
        "fwd20_up_rate_chg",
        "mom5_ge",
        "coverage",
        "avg_current_up_rate",
        "avg_fwd10_ret",
        "avg_fwd20_ret",
        "avg_fwd10_up_rate_chg",
        "avg_ret",
        "win_rate",
        "bad10_rate",
    }
    money_cols = {"sum_pnl"}
    report = [
        "# index_mom5 大盘过热审计 v1",
        "",
        "## 结论口径",
        "",
        "- `index_mom5` 是 5 个交易日的指数短线冲刺幅度，适合识别短线拥挤/追高温度。",
        "- 本报告用三类后验结果衡量“过热”：后续指数回撤、后续市场扩散降温、G3最终组合交易收益。",
        "- 它不直接替代 `index_mom60` 这种策略生命周期准入指标；更适合作为辅助观察或执行节奏指标。",
        "",
        "## mom5 分桶：后续指数与扩散",
        "",
        _md_table(bucket, pct_cols=pct_cols),
        "",
        "## mom5 阈值：对后续回撤的命中率",
        "",
        _md_table(threshold, pct_cols=pct_cols),
        "",
        "## mom5 五分位",
        "",
        _md_table(quantile, pct_cols=pct_cols),
        "",
        "## 相关性",
        "",
        _md_table(corr, pct_cols=set()),
        "",
        "## G3最终组合成交按 mom5 分桶",
        "",
        _md_table(trade_bucket, pct_cols=pct_cols, money_cols=money_cols) if not trade_bucket.empty else "_无 G3 成交数据_",
        "",
    ]
    (out_dir / "REPORT_CN.md").write_text("\n".join(report), encoding="utf-8", newline="\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
