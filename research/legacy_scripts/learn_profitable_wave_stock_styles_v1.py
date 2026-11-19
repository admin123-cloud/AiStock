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

from scripts.research_main_wave_sector_score import _code6, _load_members  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("profitable_wave_stock_style_learning_v1")
INDEX_CODE = "999999.SH"


def _safe_float(value: Any, digits: int = 6) -> Any:
    try:
        x = float(value)
    except Exception:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, digits)


def _pct(value: Any) -> str:
    x = _safe_float(value)
    if x is None:
        return ""
    return f"{x:.2%}"


def _num(value: Any, digits: int = 2) -> str:
    x = _safe_float(value, digits)
    if x is None:
        return ""
    return f"{x:.{digits}f}"


def _load_stocks() -> pd.DataFrame:
    df = clickhouse_query_df(
        """
        SELECT code, name, industry, list_date, quit, st, float_share, total_share
        FROM stocks
        WHERE code != ''
          AND type = 'stock'
          AND quit = 0
          AND (st = 0 OR st IS NULL)
        """
    )
    if df.empty:
        raise RuntimeError("stocks table has no active stocks")
    df["code_raw"] = df["code"].astype(str)
    df["code6"] = df["code"].map(_code6)
    df["stock_name"] = df["name"].astype(str)
    df["industry"] = df["industry"].fillna("").astype(str)
    df["float_share"] = pd.to_numeric(df["float_share"], errors="coerce")
    df["total_share"] = pd.to_numeric(df["total_share"], errors="coerce")
    df["list_date"] = pd.to_datetime(df["list_date"], errors="coerce")
    return df[["code_raw", "code6", "stock_name", "industry", "list_date", "float_share", "total_share"]].drop_duplicates("code6")


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
    return df.dropna(subset=["code6", "trade_date", "close"]).sort_values(["code6", "trade_date"]).reset_index(drop=True)


def _load_index_context(start_date: str, end_date: str) -> dict[str, Any]:
    df = clickhouse_query_df(
        """
        SELECT trade_date, close
        FROM kline_daily
        WHERE code = ?
          AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        [INDEX_CODE, start_date, end_date],
    )
    if df.empty:
        return {}
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["trade_date", "close"]).sort_values("trade_date")
    return {
        "index_start": _safe_float(df["close"].iloc[0]),
        "index_end": _safe_float(df["close"].iloc[-1]),
        "index_return": _safe_float(df["close"].iloc[-1] / df["close"].iloc[0] - 1.0),
        "index_days": int(len(df)),
    }


def _add_features(daily: pd.DataFrame) -> pd.DataFrame:
    d = daily.copy()
    g = d.groupby("code6", group_keys=False)
    d["ret1"] = d["close"] / g["close"].shift(1) - 1.0
    d["ma10"] = g["close"].rolling(10, min_periods=8).mean().reset_index(level=0, drop=True)
    d["ma20"] = g["close"].rolling(20, min_periods=15).mean().reset_index(level=0, drop=True)
    d["ma60"] = g["close"].rolling(60, min_periods=40).mean().reset_index(level=0, drop=True)
    d["high60"] = g["high"].rolling(60, min_periods=30).max().reset_index(level=0, drop=True)
    d["high120"] = g["high"].rolling(120, min_periods=60).max().reset_index(level=0, drop=True)
    d["low120"] = g["low"].rolling(120, min_periods=60).min().reset_index(level=0, drop=True)
    d["amount5"] = g["amount"].rolling(5, min_periods=3).mean().reset_index(level=0, drop=True)
    d["amount20"] = g["amount"].rolling(20, min_periods=10).mean().reset_index(level=0, drop=True)
    d["amount60"] = g["amount"].rolling(60, min_periods=30).mean().reset_index(level=0, drop=True)
    d["mom20"] = g["close"].pct_change(20)
    d["mom60"] = g["close"].pct_change(60)
    d["mom120"] = g["close"].pct_change(120)
    d["above_ma20"] = d["close"] >= d["ma20"]
    d["above_ma60"] = d["close"] >= d["ma60"]
    d["range_pos120"] = (d["close"] - d["low120"]) / (d["high120"] - d["low120"]).replace(0, np.nan)
    d["near_high60"] = d["close"] >= d["high60"] * 0.95
    d["amount_ratio20_60"] = d["amount20"] / d["amount60"].replace(0, np.nan)
    return d.replace([np.inf, -np.inf], np.nan)


def _last_on_or_before(daily: pd.DataFrame, date: pd.Timestamp) -> pd.DataFrame:
    d = daily[daily["trade_date"] <= date].copy()
    if d.empty:
        return pd.DataFrame()
    return d.sort_values(["code6", "trade_date"]).groupby("code6", as_index=False).tail(1)


def _wave_stock_metrics(features: pd.DataFrame, stocks: pd.DataFrame, members: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    start = pd.Timestamp(args.wave_start)
    end = pd.Timestamp(args.wave_end)
    pre_start = start - pd.Timedelta(days=int(args.pre_days) * 2)
    pre = features[(features["trade_date"] >= pre_start) & (features["trade_date"] < start)].copy()
    wave = features[(features["trade_date"] >= start) & (features["trade_date"] <= end)].copy()
    start_rows = _last_on_or_before(features, start)
    end_rows = _last_on_or_before(features, end)
    if start_rows.empty or end_rows.empty or wave.empty:
        raise RuntimeError("not enough daily rows for wave metrics")

    rows: list[dict[str, Any]] = []
    start_map = start_rows.set_index("code6")
    end_map = end_rows.set_index("code6")
    pre_group = pre.groupby("code6")
    wave_group = wave.groupby("code6")
    common = sorted(set(start_map.index) & set(end_map.index) & set(wave_group.groups.keys()))
    for code6 in common:
        sr = start_map.loc[code6]
        er = end_map.loc[code6]
        if float(sr["close"]) <= 0 or float(er["close"]) <= 0:
            continue
        wg = wave_group.get_group(code6).sort_values("trade_date")
        pg = pre_group.get_group(code6).sort_values("trade_date") if code6 in pre_group.groups else pd.DataFrame()
        if len(wg) < int(args.min_wave_days):
            continue
        close = pd.to_numeric(wg["close"], errors="coerce")
        amount = pd.to_numeric(wg["amount"], errors="coerce")
        ret1 = pd.to_numeric(wg["ret1"], errors="coerce")
        cummax = close.cummax()
        max_dd = (close / cummax - 1.0).min()
        first20 = wg.head(20)
        pre_amount20 = float(sr.get("amount20", np.nan))
        wave_amount20 = float(amount.mean())
        float_share = np.nan
        item = {
            "code6": code6,
            "code_raw": str(sr.get("code_raw", "")),
            "wave_start_trade_date": pd.Timestamp(sr["trade_date"]).strftime("%Y-%m-%d"),
            "wave_end_trade_date": pd.Timestamp(er["trade_date"]).strftime("%Y-%m-%d"),
            "start_close": _safe_float(sr["close"]),
            "end_close": _safe_float(er["close"]),
            "wave_return": _safe_float(float(er["close"]) / float(sr["close"]) - 1.0),
            "first20_return": _safe_float(first20["close"].iloc[-1] / float(sr["close"]) - 1.0) if len(first20) >= 2 else None,
            "wave_max_runup": _safe_float(close.max() / float(sr["close"]) - 1.0),
            "wave_max_drawdown_from_peak": _safe_float(max_dd),
            "wave_days": int(len(wg)),
            "wave_up_day_ratio": _safe_float((ret1 > 0).mean()),
            "wave_big_up_days": int((pd.to_numeric(wg["change_pct"], errors="coerce") >= 5.0).sum()),
            "wave_limit_up_days": int((pd.to_numeric(wg["change_pct"], errors="coerce") >= 9.5).sum()),
            "wave_big_down_days": int((pd.to_numeric(wg["change_pct"], errors="coerce") <= -5.0).sum()),
            "wave_avg_amount": _safe_float(wave_amount20),
            "amount_expansion_wave_vs_pre20": _safe_float(wave_amount20 / pre_amount20) if pre_amount20 and pre_amount20 > 0 else None,
            "start_amount20": _safe_float(sr.get("amount20")),
            "start_amount_ratio20_60": _safe_float(sr.get("amount_ratio20_60")),
            "start_mom20": _safe_float(sr.get("mom20")),
            "start_mom60": _safe_float(sr.get("mom60")),
            "start_mom120": _safe_float(sr.get("mom120")),
            "start_above_ma20": bool(sr.get("above_ma20", False)),
            "start_above_ma60": bool(sr.get("above_ma60", False)),
            "start_range_pos120": _safe_float(sr.get("range_pos120")),
            "start_near_high60": bool(sr.get("near_high60", False)),
            "start_close_to_high60": _safe_float(float(sr["close"]) / float(sr["high60"]) - 1.0) if pd.notna(sr.get("high60")) and float(sr["high60"]) > 0 else None,
            "pre_up_day_ratio": _safe_float((pd.to_numeric(pg.get("ret1"), errors="coerce") > 0).mean()) if not pg.empty else None,
            "pre_big_up_days": int((pd.to_numeric(pg.get("change_pct"), errors="coerce") >= 5.0).sum()) if not pg.empty else 0,
            "pre_limit_up_days": int((pd.to_numeric(pg.get("change_pct"), errors="coerce") >= 9.5).sum()) if not pg.empty else 0,
        }
        rows.append(item)

    out = pd.DataFrame(rows)
    if out.empty:
        raise RuntimeError("wave metrics produced no rows")
    out = out.merge(stocks, on="code6", how="left")
    out["float_market_cap_yi"] = pd.to_numeric(out["float_share"], errors="coerce") * pd.to_numeric(out["start_close"], errors="coerce") / 1e8
    out["total_market_cap_yi"] = pd.to_numeric(out["total_share"], errors="coerce") * pd.to_numeric(out["start_close"], errors="coerce") / 1e8

    l2 = members[members["level"].astype("Int64").eq(2)].copy()
    l2 = l2.sort_values(["stock_code6", "sector_code"]).drop_duplicates("stock_code6")
    out = out.merge(
        l2[["stock_code6", "sector_code", "sector_name"]].rename(
            columns={"stock_code6": "code6", "sector_code": "l2_sector_code", "sector_name": "l2_sector_name"}
        ),
        on="code6",
        how="left",
    )
    out["winner_rank"] = pd.to_numeric(out["wave_return"], errors="coerce").rank(method="first", ascending=False)
    return out.sort_values("wave_return", ascending=False).reset_index(drop=True)


def _cap_bucket(v: Any) -> str:
    x = _safe_float(v)
    if x is None:
        return "unknown_cap"
    if x >= 500:
        return "mega_cap_500y_plus"
    if x >= 200:
        return "large_cap_200_500y"
    if x >= 80:
        return "mid_large_80_200y"
    if x >= 30:
        return "mid_small_30_80y"
    return "small_cap_lt30y"


def _liquidity_bucket(v: Any) -> str:
    x = _safe_float(v)
    if x is None:
        return "unknown_liquidity"
    # kline_daily.amount in this local warehouse is commonly stored in 10k CNY units.
    if x >= 100_000:
        return "high_liquidity_10y_plus"
    if x >= 30_000:
        return "good_liquidity_3_10y"
    if x >= 10_000:
        return "mid_liquidity_1_3y"
    return "low_liquidity_lt1y"


def _style_label(row: pd.Series) -> str:
    cap = _safe_float(row.get("float_market_cap_yi"))
    amount20 = _safe_float(row.get("start_amount20"))
    wave_ret = _safe_float(row.get("wave_return")) or 0.0
    limit_days = int(row.get("wave_limit_up_days") or 0)
    big_up_days = int(row.get("wave_big_up_days") or 0)
    start_mom60 = _safe_float(row.get("start_mom60")) or 0.0
    range_pos = _safe_float(row.get("start_range_pos120"))
    near_high = bool(row.get("start_near_high60", False))
    up_ratio = _safe_float(row.get("wave_up_day_ratio")) or 0.0
    max_dd = abs(_safe_float(row.get("wave_max_drawdown_from_peak")) or 0.0)
    expansion = _safe_float(row.get("amount_expansion_wave_vs_pre20")) or 1.0

    institution_size = ((cap is not None and cap >= 80.0) or (amount20 is not None and amount20 >= 30_000))
    if institution_size and near_high and start_mom60 >= 0 and up_ratio >= 0.48 and limit_days <= 2 and max_dd <= 0.28:
        return "机构趋势主升"
    if near_high and wave_ret >= 0.25 and expansion >= 1.15 and max_dd <= 0.32:
        return "突破加速主升"
    if limit_days >= 2 or big_up_days >= 5:
        return "题材弹性连板/大阳"
    if (range_pos is not None and range_pos <= 0.35) and start_mom60 <= -0.08 and wave_ret >= 0.25:
        return "底部反转修复"
    if institution_size and up_ratio >= 0.48 and limit_days <= 1:
        return "机构稳步抬升"
    return "混合波段"


def _summaries(winners: pd.DataFrame, all_rows: pd.DataFrame, top_n: int) -> dict[str, pd.DataFrame]:
    w = winners.head(int(top_n)).copy()
    if "code_raw" not in w.columns:
        if "code_raw_y" in w.columns:
            w["code_raw"] = w["code_raw_y"]
        elif "code_raw_x" in w.columns:
            w["code_raw"] = w["code_raw_x"]
        else:
            w["code_raw"] = w["code6"]
    if "code_raw" not in all_rows.columns:
        if "code_raw_y" in all_rows.columns:
            all_rows["code_raw"] = all_rows["code_raw_y"]
        elif "code_raw_x" in all_rows.columns:
            all_rows["code_raw"] = all_rows["code_raw_x"]
        else:
            all_rows["code_raw"] = all_rows["code6"]
    for d in [w, all_rows]:
        d["style_label"] = d.apply(_style_label, axis=1)
        d["cap_bucket"] = d["float_market_cap_yi"].map(_cap_bucket)
        d["liquidity_bucket"] = d["start_amount20"].map(_liquidity_bucket)

    tradable = all_rows[
        (pd.to_numeric(all_rows["wave_return"], errors="coerce") >= 0.30)
        & (pd.to_numeric(all_rows["start_amount20"], errors="coerce") >= 10_000)
        & ~all_rows["code_raw"].astype(str).str.endswith(".BJ")
    ].sort_values("wave_return", ascending=False).head(int(top_n)).copy()
    institution = all_rows[
        (pd.to_numeric(all_rows["wave_return"], errors="coerce") >= 0.30)
        & (pd.to_numeric(all_rows["start_amount20"], errors="coerce") >= 30_000)
        & ~all_rows["code_raw"].astype(str).str.endswith(".BJ")
    ].sort_values("wave_return", ascending=False).head(int(top_n)).copy()
    for d in [tradable, institution]:
        d["style_label"] = d.apply(_style_label, axis=1)
        d["cap_bucket"] = d["float_market_cap_yi"].map(_cap_bucket)
        d["liquidity_bucket"] = d["start_amount20"].map(_liquidity_bucket)

    style = (
        w.groupby("style_label", dropna=False)
        .agg(
            count=("code6", "count"),
            avg_wave_return=("wave_return", "mean"),
            median_wave_return=("wave_return", "median"),
            avg_float_mcap_yi=("float_market_cap_yi", "mean"),
            avg_start_amount20=("start_amount20", "mean"),
            avg_limit_up_days=("wave_limit_up_days", "mean"),
            avg_amount_expansion=("amount_expansion_wave_vs_pre20", "mean"),
        )
        .reset_index()
        .sort_values(["count", "avg_wave_return"], ascending=[False, False])
    )
    sector = (
        w.groupby("l2_sector_name", dropna=False)
        .agg(
            count=("code6", "count"),
            avg_wave_return=("wave_return", "mean"),
            median_wave_return=("wave_return", "median"),
            best_return=("wave_return", "max"),
        )
        .reset_index()
        .sort_values(["count", "avg_wave_return"], ascending=[False, False])
    )
    cap = (
        w.groupby("cap_bucket", dropna=False)
        .agg(count=("code6", "count"), avg_wave_return=("wave_return", "mean"))
        .reset_index()
        .sort_values(["count", "avg_wave_return"], ascending=[False, False])
    )
    liq = (
        w.groupby("liquidity_bucket", dropna=False)
        .agg(count=("code6", "count"), avg_wave_return=("wave_return", "mean"))
        .reset_index()
        .sort_values(["count", "avg_wave_return"], ascending=[False, False])
    )
    all_style = (
        all_rows.groupby("style_label", dropna=False)
        .agg(count=("code6", "count"), avg_wave_return=("wave_return", "mean"), median_wave_return=("wave_return", "median"))
        .reset_index()
        .sort_values("avg_wave_return", ascending=False)
    )
    return {
        "winners": w,
        "style_summary": style,
        "sector_summary": sector,
        "cap_summary": cap,
        "liquidity_summary": liq,
        "all_style_summary": all_style,
        "tradable_winners": tradable,
        "institutional_winners": institution,
        "tradable_style_summary": _style_summary(tradable),
        "institutional_style_summary": _style_summary(institution),
    }


def _style_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    return (
        df.groupby("style_label", dropna=False)
        .agg(
            count=("code6", "count"),
            avg_wave_return=("wave_return", "mean"),
            median_wave_return=("wave_return", "median"),
            avg_start_amount20=("start_amount20", "mean"),
            avg_limit_up_days=("wave_limit_up_days", "mean"),
            avg_max_dd=("wave_max_drawdown_from_peak", "mean"),
            avg_amount_expansion=("amount_expansion_wave_vs_pre20", "mean"),
        )
        .reset_index()
        .sort_values(["count", "avg_wave_return"], ascending=[False, False])
    )


def _format_md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, num_cols: set[str] | None = None, limit: int | None = None) -> str:
    if df.empty:
        return "_无数据_"
    d = df.head(limit).copy() if limit else df.copy()
    pct_cols = pct_cols or set()
    num_cols = num_cols or set()
    rows: list[dict[str, Any]] = []
    for _, row in d.iterrows():
        item: dict[str, Any] = {}
        for col in d.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = _pct(value)
            elif col in num_cols:
                item[col] = _num(value)
            elif isinstance(value, float):
                item[col] = _num(value, 4)
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def _write_report(args: argparse.Namespace, result: dict[str, Any], frames: dict[str, pd.DataFrame]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, df in frames.items():
        df.to_csv(OUT_DIR / f"{name}.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    winners = frames["winners"]
    pct_cols = {
        "wave_return",
        "first20_return",
        "wave_max_runup",
        "wave_max_drawdown_from_peak",
        "wave_up_day_ratio",
        "start_mom20",
        "start_mom60",
        "start_mom120",
        "start_range_pos120",
        "avg_wave_return",
        "median_wave_return",
        "best_return",
    }
    num_cols = {"float_market_cap_yi", "total_market_cap_yi", "avg_float_mcap_yi", "avg_amount_expansion", "amount_expansion_wave_vs_pre20"}
    top_cols = [
        "winner_rank",
        "code_raw",
        "stock_name",
        "l2_sector_name",
        "style_label",
        "wave_return",
        "first20_return",
        "wave_max_drawdown_from_peak",
        "wave_limit_up_days",
        "start_mom60",
        "start_range_pos120",
        "start_amount20",
        "float_market_cap_yi",
        "amount_expansion_wave_vs_pre20",
    ]
    template_lines = [
        "1. 机构趋势主升：启动前已接近 60 日高位，20/60 日均线多头，成交额不低，波段中涨停少但持续上行。",
        "2. 突破加速主升：启动前靠近压力位，波段初段快速脱离平台，成交温和放大，回撤不深。",
        "3. 题材弹性连板/大阳：小中盘或高弹性方向，涨停/大阳日密集，收益高但回撤和择时要求更强。",
        "4. 底部反转修复：启动前在低位，适合震荡修复市，不应和机构大波段主升混在同一个买点系统里。",
    ]
    lines = [
        "# 前一波段赚钱股票风格学习 v1",
        "",
        "## 波段定义",
        "",
        f"- 波段：{args.wave_start} 至 {args.wave_end}",
        f"- 赢家口径：全市场波段收益 Top {args.top_n}，并要求波段收益不低于 {float(args.min_return):.0%}",
        f"- 指数同期收益：{_pct(result.get('index_return'))}",
        "",
        "## 核心观察",
        "",
        "- 这份报告不是直接给买点，而是学习上一段市场实际奖励了什么结构。",
        "- 后续策略应先判断当前市场更像哪类赚钱模式，再选择主升追随、突破确认、题材弹性或低吸修复，不应把震荡低吸模型套到所有年份。",
        "",
        "## 风格分布",
        "",
        _format_md_table(frames["style_summary"], pct_cols=pct_cols, num_cols=num_cols),
        "",
        "## 可交易赢家风格",
        "",
        "- 口径：剔除 BJ，启动前 20 日均成交额不低于 1 亿。",
        "",
        _format_md_table(frames["tradable_style_summary"], pct_cols=pct_cols, num_cols=num_cols),
        "",
        "## 机构可参与赢家风格",
        "",
        "- 口径：剔除 BJ，启动前 20 日均成交额不低于 3 亿。",
        "",
        _format_md_table(frames["institutional_style_summary"], pct_cols=pct_cols, num_cols=num_cols),
        "",
        "## 行业集中度",
        "",
        _format_md_table(frames["sector_summary"], pct_cols=pct_cols, limit=30),
        "",
        "## 市值分布",
        "",
        _format_md_table(frames["cap_summary"], pct_cols=pct_cols),
        "",
        "## 流动性分布",
        "",
        _format_md_table(frames["liquidity_summary"], pct_cols=pct_cols),
        "",
        "## 赢家样本",
        "",
        _format_md_table(winners[[c for c in top_cols if c in winners.columns]], pct_cols=pct_cols, num_cols=num_cols, limit=80),
        "",
        "## 机构可参与样本",
        "",
        _format_md_table(
            frames["institutional_winners"][[c for c in top_cols if c in frames["institutional_winners"].columns]],
            pct_cols=pct_cols,
            num_cols=num_cols,
            limit=80,
        ),
        "",
        "## 可转化模板",
        "",
        "\n".join(template_lines),
        "",
        "## 输出文件",
        "",
        f"- winners.csv: {OUT_DIR / 'winners.csv'}",
        f"- style_summary.csv: {OUT_DIR / 'style_summary.csv'}",
        f"- sector_summary.csv: {OUT_DIR / 'sector_summary.csv'}",
        f"- tradable_winners.csv: {OUT_DIR / 'tradable_winners.csv'}",
        f"- institutional_winners.csv: {OUT_DIR / 'institutional_winners.csv'}",
        f"- all_style_summary.csv: {OUT_DIR / 'all_style_summary.csv'}",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    load_start = (pd.Timestamp(args.wave_start) - pd.Timedelta(days=int(args.pre_days) * 2 + 140)).strftime("%Y-%m-%d")
    load_end = args.wave_end
    stocks = _load_stocks()
    daily = _load_daily(load_start, load_end)
    daily = daily.merge(stocks[["code6", "stock_name"]], on="code6", how="inner")
    features = _add_features(daily)
    members = _load_members([2], 8)
    all_rows = _wave_stock_metrics(features, stocks, members, args)
    all_rows["style_label"] = all_rows.apply(_style_label, axis=1)
    all_rows["cap_bucket"] = all_rows["float_market_cap_yi"].map(_cap_bucket)
    all_rows["liquidity_bucket"] = all_rows["start_amount20"].map(_liquidity_bucket)

    winners = all_rows[pd.to_numeric(all_rows["wave_return"], errors="coerce") >= float(args.min_return)].copy()
    winners = winners.sort_values("wave_return", ascending=False).head(int(args.top_n)).copy()
    frames = _summaries(winners, all_rows, int(args.top_n))
    frames["all_stocks_wave_metrics"] = all_rows

    index_ctx = _load_index_context(args.wave_start, args.wave_end)
    result = {
        "status": "completed",
        "wave_start": args.wave_start,
        "wave_end": args.wave_end,
        "top_n": int(args.top_n),
        "min_return": float(args.min_return),
        "all_stock_rows": int(len(all_rows)),
        "winner_rows": int(len(frames["winners"])),
        "out_dir": str(OUT_DIR),
        **index_ctx,
    }
    _write_report(args, result, frames)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Learn profitable stock styles from a prior market wave.")
    parser.add_argument("--wave-start", default="2024-09-24")
    parser.add_argument("--wave-end", default="2025-03-31")
    parser.add_argument("--top-n", type=int, default=120)
    parser.add_argument("--min-return", type=float, default=0.30)
    parser.add_argument("--pre-days", type=int, default=80)
    parser.add_argument("--min-wave-days", type=int, default=20)
    args = parser.parse_args()
    result = run(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
