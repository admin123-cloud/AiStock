from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.learn_profitable_wave_stock_styles_v1 import (  # noqa: E402
    _add_features,
    _code6,
    _load_stocks,
    _liquidity_bucket,
    _pct,
    _safe_float,
)
from scripts.research_main_wave_sector_score import _load_members  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path  # noqa: E402


LEARN_DIR = report_path("profitable_wave_stock_style_learning_v1")
OUT_DIR = report_path("current_wave_style_candidate_scan_v1")


def _latest_trade_date() -> str:
    df = clickhouse_query_df("SELECT max(trade_date) AS trade_date FROM kline_daily")
    if df.empty or pd.isna(df.iloc[0]["trade_date"]):
        raise RuntimeError("kline_daily has no latest trade_date")
    return pd.Timestamp(df.iloc[0]["trade_date"]).strftime("%Y-%m-%d")


def _load_daily(start_date: str, end_date: str) -> pd.DataFrame:
    df = clickhouse_query_df(
        """
        SELECT code, trade_date, open, high, low, close, volume, amount, change_pct, turnover_rate
        FROM kline_daily
        WHERE trade_date BETWEEN ? AND ?
          AND close > 0
          AND change_pct > -50
          AND change_pct < 50
        ORDER BY code, trade_date
        """,
        [start_date, end_date],
    )
    if df.empty:
        raise RuntimeError("kline_daily query returned no rows")
    df["code_raw"] = df["code"].astype(str)
    df["code6"] = df["code"].map(_code6)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    for col in ["open", "high", "low", "close", "volume", "amount", "change_pct", "turnover_rate"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["open", "high", "low", "close", "volume", "amount", "change_pct", "turnover_rate"]:
        df[col] = df[col].astype("float32")
    # Intraday maintenance can leave duplicate code/date rows. Keep the row with the larger amount.
    df = df.dropna(subset=["code6", "trade_date", "close"]).sort_values(["code6", "trade_date", "amount"])
    return df.groupby(["code6", "trade_date"], as_index=False).tail(1).sort_values(["code6", "trade_date"]).reset_index(drop=True)


def _load_learned_sector_focus() -> pd.DataFrame:
    path = LEARN_DIR / "institutional_winners.csv"
    if not path.exists():
        return pd.DataFrame(columns=["l2_sector_name", "learned_count", "learned_avg_return"])
    d = pd.read_csv(path, encoding="utf-8-sig")
    if d.empty or "l2_sector_name" not in d.columns:
        return pd.DataFrame(columns=["l2_sector_name", "learned_count", "learned_avg_return"])
    d["wave_return"] = pd.to_numeric(d.get("wave_return"), errors="coerce")
    out = (
        d.dropna(subset=["l2_sector_name"])
        .groupby("l2_sector_name", as_index=False)
        .agg(learned_count=("code6", "count"), learned_avg_return=("wave_return", "mean"))
        .sort_values(["learned_count", "learned_avg_return"], ascending=[False, False])
    )
    out["learned_sector_rank"] = out["learned_count"].rank(method="first", ascending=False)
    return out


def _latest_frame(features: pd.DataFrame, target_date: str) -> pd.DataFrame:
    target = pd.Timestamp(target_date)
    d = features[features["trade_date"] <= target].copy()
    if d.empty:
        raise RuntimeError(f"no daily rows on or before {target_date}")
    latest = d.sort_values(["code6", "trade_date"]).groupby("code6", as_index=False).tail(1)
    if latest.empty:
        raise RuntimeError("latest frame empty")
    return latest.reset_index(drop=True)


def _attach_recent_features(features: pd.DataFrame, latest: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for code6, g in features.groupby("code6", sort=False):
        g = g.sort_values("trade_date").tail(60)
        if g.empty:
            continue
        last20 = g.tail(20)
        close = pd.to_numeric(last20["close"], errors="coerce")
        if close.empty:
            continue
        max_dd20 = (close / close.cummax() - 1.0).min()
        rows.append(
            {
                "code6": code6,
                "ret5": _safe_float(g["close"].iloc[-1] / g["close"].iloc[-6] - 1.0) if len(g) >= 6 and g["close"].iloc[-6] > 0 else None,
                "ret10": _safe_float(g["close"].iloc[-1] / g["close"].iloc[-11] - 1.0) if len(g) >= 11 and g["close"].iloc[-11] > 0 else None,
                "ret20": _safe_float(g["close"].iloc[-1] / g["close"].iloc[-21] - 1.0) if len(g) >= 21 and g["close"].iloc[-21] > 0 else None,
                "limit_up_days20": int((pd.to_numeric(last20["change_pct"], errors="coerce") >= 9.5).sum()),
                "big_up_days20": int((pd.to_numeric(last20["change_pct"], errors="coerce") >= 5.0).sum()),
                "big_down_days20": int((pd.to_numeric(last20["change_pct"], errors="coerce") <= -5.0).sum()),
                "max_dd20": _safe_float(max_dd20),
                "up_day_ratio20": _safe_float((pd.to_numeric(last20["ret1"], errors="coerce") > 0).mean()),
            }
        )
    extra = pd.DataFrame(rows)
    return latest.merge(extra, on="code6", how="left")


def _template_scores(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    for col in [
        "amount20",
        "amount5",
        "amount60",
        "amount_ratio20_60",
        "mom20",
        "mom60",
        "range_pos120",
        "change_pct",
        "ret5",
        "ret10",
        "ret20",
        "max_dd20",
        "learned_count",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")

    close_to_high60 = d["close"] / d["high60"].replace(0, np.nan) - 1.0
    d["close_to_high60"] = close_to_high60
    d["amount5_20"] = d["amount5"] / d["amount20"].replace(0, np.nan)
    d["liquidity_bucket"] = d["amount20"].map(_liquidity_bucket)
    d["learned_sector_bonus"] = (d.get("learned_count", 0).fillna(0) / 8.0).clip(0.0, 1.0)

    d["capacity_score"] = (d["amount20"] / 100_000.0).clip(0.0, 1.0) * 20.0
    d["trend_score"] = (
        d["above_ma20"].fillna(False).astype(float) * 8.0
        + d["above_ma60"].fillna(False).astype(float) * 8.0
        + ((d["mom20"] + 0.05) / 0.25).clip(0.0, 1.0).fillna(0.0) * 8.0
        + ((d["mom60"] + 0.10) / 0.35).clip(0.0, 1.0).fillna(0.0) * 8.0
    )
    d["position_score"] = (
        ((d["range_pos120"] - 0.35) / 0.45).clip(0.0, 1.0).fillna(0.0) * 12.0
        + ((close_to_high60 + 0.15) / 0.15).clip(0.0, 1.0).fillna(0.0) * 12.0
    )
    d["volume_score"] = (
        ((d["amount_ratio20_60"] - 0.90) / 0.80).clip(0.0, 1.0).fillna(0.0) * 10.0
        + ((d["amount5_20"] - 0.90) / 0.80).clip(0.0, 1.0).fillna(0.0) * 10.0
    )
    d["acceleration_score"] = (
        ((d["ret5"] + 0.02) / 0.18).clip(0.0, 1.0).fillna(0.0) * 8.0
        + ((d["change_pct"] + 1.0) / 8.0).clip(0.0, 1.0).fillna(0.0) * 6.0
        + ((d["big_up_days20"] - 1.0) / 5.0).clip(0.0, 1.0).fillna(0.0) * 6.0
    )
    d["learned_sector_score"] = d["learned_sector_bonus"] * 12.0
    d["climax_penalty"] = (
        (pd.to_numeric(d.get("limit_up_days20", 0), errors="coerce").fillna(0) / 4.0).clip(0.0, 1.0) * 10.0
        + ((d["ret20"] - 0.65) / 0.45).clip(0.0, 1.0).fillna(0.0) * 8.0
    )
    d["wave_style_score"] = (
        d["capacity_score"]
        + d["trend_score"]
        + d["position_score"]
        + d["volume_score"]
        + d["acceleration_score"]
        + d["learned_sector_score"]
        - d["climax_penalty"]
    )

    d["template_label"] = "观察"
    institution_trend = (
        (d["amount20"] >= 30_000)
        & d["above_ma20"].fillna(False)
        & d["above_ma60"].fillna(False)
        & (close_to_high60 >= -0.10)
        & (pd.to_numeric(d.get("limit_up_days20", 0), errors="coerce").fillna(0) <= 2)
        & (d["max_dd20"].fillna(-1.0) >= -0.22)
    )
    breakout_accel = (
        (d["amount20"] >= 30_000)
        & (close_to_high60 >= -0.08)
        & (d["amount5_20"] >= 1.10)
        & (d["ret5"].fillna(0) >= 0.03)
        & (d["change_pct"].between(1.5, 9.3))
    )
    capacity_theme = (
        (d["amount20"] >= 30_000)
        & (d["learned_sector_bonus"] > 0)
        & (d["ret20"].fillna(0) >= 0.08)
        & (pd.to_numeric(d.get("big_up_days20", 0), errors="coerce").fillna(0) >= 2)
    )
    too_late = (d["ret20"].fillna(0) >= 0.75) | (pd.to_numeric(d.get("limit_up_days20", 0), errors="coerce").fillna(0) >= 5)
    d.loc[institution_trend, "template_label"] = "机构趋势主升预备"
    d.loc[breakout_accel, "template_label"] = "突破加速确认"
    d.loc[capacity_theme & ~breakout_accel, "template_label"] = "容量题材主升"
    d.loc[too_late, "template_label"] = "可能已高潮"
    d["template_pass"] = d["template_label"].ne("观察") & d["wave_style_score"].ge(45.0)
    return d.replace([np.inf, -np.inf], np.nan)


def _load_current_candidates(args: argparse.Namespace) -> tuple[pd.DataFrame, dict[str, Any]]:
    target_date = args.target_date or _latest_trade_date()
    load_start = (pd.Timestamp(target_date) - pd.Timedelta(days=int(args.lookback_days) * 2)).strftime("%Y-%m-%d")
    stocks = _load_stocks()
    daily = _load_daily(load_start, target_date)
    daily = daily.merge(stocks[["code6", "stock_name", "industry"]], on="code6", how="inner")
    features = _add_features(daily)
    latest = _latest_frame(features, target_date)
    latest = _attach_recent_features(features[features["trade_date"] <= pd.Timestamp(target_date)], latest)
    members = _load_members([2], 8)
    l2 = members[members["level"].astype("Int64").eq(2)].copy()
    l2 = l2.sort_values(["stock_code6", "sector_code"]).drop_duplicates("stock_code6")
    latest = latest.merge(
        l2[["stock_code6", "sector_code", "sector_name"]].rename(
            columns={"stock_code6": "code6", "sector_code": "l2_sector_code", "sector_name": "l2_sector_name"}
        ),
        on="code6",
        how="left",
    )
    learned_sector = _load_learned_sector_focus()
    latest = latest.merge(learned_sector, on="l2_sector_name", how="left")
    latest = _template_scores(latest)
    latest = latest[~latest["code_raw"].astype(str).str.endswith(".BJ")].copy()
    latest = latest[pd.to_numeric(latest["amount20"], errors="coerce") >= float(args.min_amount20)].copy()
    latest = latest.sort_values("wave_style_score", ascending=False).reset_index(drop=True)
    meta = {
        "target_date": target_date,
        "load_start": load_start,
        "daily_rows": int(len(daily)),
        "latest_rows": int(len(latest)),
        "learned_sector_rows": int(len(learned_sector)),
    }
    return latest, meta


def _summary_by(df: pd.DataFrame, key: str) -> pd.DataFrame:
    if df.empty or key not in df.columns:
        return pd.DataFrame()
    return (
        df.groupby(key, dropna=False)
        .agg(
            count=("code6", "count"),
            avg_score=("wave_style_score", "mean"),
            avg_ret20=("ret20", "mean"),
            avg_amount20=("amount20", "mean"),
            learned_sector_hits=("learned_sector_bonus", "sum"),
        )
        .reset_index()
        .sort_values(["count", "avg_score"], ascending=[False, False])
    )


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, limit: int | None = None) -> str:
    if df.empty:
        return "_无数据_"
    d = df.head(limit).copy() if limit else df.copy()
    pct_cols = pct_cols or set()
    rows: list[dict[str, Any]] = []
    for _, row in d.iterrows():
        item: dict[str, Any] = {}
        for col in d.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = _pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def _write_outputs(candidates: pd.DataFrame, meta: dict[str, Any]) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    passed = candidates[candidates["template_pass"]].copy()
    priority = passed[
        passed["template_label"].isin(["突破加速确认", "容量题材主升", "机构趋势主升预备"])
        & (pd.to_numeric(passed["wave_style_score"], errors="coerce") >= 100.0)
        & (pd.to_numeric(passed.get("learned_sector_bonus", 0), errors="coerce").fillna(0) > 0)
        & (pd.to_numeric(passed.get("ret20", 0), errors="coerce").fillna(0) < 0.55)
        & (pd.to_numeric(passed.get("limit_up_days20", 0), errors="coerce").fillna(0) <= 4)
    ].copy()
    top = candidates.head(200).copy()
    template_summary = _summary_by(passed, "template_label")
    sector_summary = _summary_by(passed, "l2_sector_name")
    priority_template_summary = _summary_by(priority, "template_label")
    priority_sector_summary = _summary_by(priority, "l2_sector_name")
    top.to_csv(OUT_DIR / "top_candidates.csv", index=False, encoding="utf-8-sig")
    passed.to_csv(OUT_DIR / "template_pass_candidates.csv", index=False, encoding="utf-8-sig")
    priority.to_csv(OUT_DIR / "priority_watchlist.csv", index=False, encoding="utf-8-sig")
    template_summary.to_csv(OUT_DIR / "template_summary.csv", index=False, encoding="utf-8-sig")
    sector_summary.to_csv(OUT_DIR / "sector_summary.csv", index=False, encoding="utf-8-sig")
    priority_template_summary.to_csv(OUT_DIR / "priority_template_summary.csv", index=False, encoding="utf-8-sig")
    priority_sector_summary.to_csv(OUT_DIR / "priority_sector_summary.csv", index=False, encoding="utf-8-sig")

    result = {
        "status": "completed",
        **meta,
        "passed_rows": int(len(passed)),
        "priority_rows": int(len(priority)),
        "out_dir": str(OUT_DIR),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    keep = [
        "trade_date",
        "code_raw",
        "stock_name",
        "l2_sector_name",
        "template_label",
        "wave_style_score",
        "close",
        "change_pct",
        "ret5",
        "ret20",
        "amount20",
        "amount5_20",
        "amount_ratio20_60",
        "mom20",
        "mom60",
        "range_pos120",
        "close_to_high60",
        "limit_up_days20",
        "max_dd20",
        "learned_count",
    ]
    lines = [
        "# 当前市场波段赢家模式扫描 v1",
        "",
        "## 口径",
        "",
        f"- 目标日期：{meta['target_date']}",
        "- 学习来源：2024-09-24 至 2025-03-31 的机构可参与赢家池。",
        "- 当前扫描不是买入信号，只是找“下一轮可能复刻上一波赚钱模式”的观察池。",
        "",
        "## 模板分布",
        "",
        _md_table(template_summary, pct_cols={"avg_ret20"}),
        "",
        "## 行业分布",
        "",
        _md_table(sector_summary, pct_cols={"avg_ret20"}, limit=30),
        "",
        "## 优先观察池",
        "",
        "- 口径：命中学习行业、分数 >= 100、剔除 20 日涨幅过大和涨停过密样本。",
        "",
        _md_table(priority_template_summary, pct_cols={"avg_ret20"}),
        "",
        "## 优先观察行业",
        "",
        _md_table(priority_sector_summary, pct_cols={"avg_ret20"}, limit=20),
        "",
        "## 通过模板的候选",
        "",
        _md_table(priority[[c for c in keep if c in priority.columns]], pct_cols={"ret5", "ret20", "mom20", "mom60", "range_pos120", "close_to_high60", "max_dd20"}, limit=80),
        "",
        "## 下一步",
        "",
        "- 对 `突破加速确认` 做 30m 成交承接验证，避免日线追高。",
        "- 对 `容量题材主升` 做行业扩散验证，确认是不是主线而非单票脉冲。",
        "- 对 `可能已高潮` 只保留观察，不作为新开仓模板。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    candidates, meta = _load_current_candidates(args)
    return _write_outputs(candidates, meta)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan current market candidates matching learned profitable wave styles.")
    parser.add_argument("--target-date", default=None)
    parser.add_argument("--lookback-days", type=int, default=160)
    parser.add_argument("--min-amount20", type=float, default=30_000.0)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
