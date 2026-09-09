from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_wave_style_template_strategy_v1 import _max_drawdown  # noqa: E402
from scripts.gen3_full_candidate_2slot_backtest_v1 import _simulate_portfolio  # noqa: E402
from scripts.gen3_promotion_self_test_v1 import (  # noqa: E402
    CONTRACTS,
    _build_price_context,
    _prepare_contract_candidates,
)
from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path  # noqa: E402


SOURCE = report_path("score120_sector_diffusion_30m_overlay_v1", "base_trades_with_sector_diffusion_30m.csv")
OUT_DIR = report_path("score120_sector_natural_gate_v1")
CONTRACT_NAME = "g3_2slot_50_default_stop12_take12_prevlow"


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, Path):
        return str(value)
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return str(value)


def _pct(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return ""
    if not math.isfinite(x):
        return ""
    return f"{x:.2%}"


def _read_source() -> pd.DataFrame:
    if not SOURCE.exists():
        raise FileNotFoundError(f"missing source: {SOURCE}")
    d = pd.read_csv(SOURCE, low_memory=False, encoding="utf-8-sig")
    for col in ["trade_date", "entry_date", "policy_exit_date"]:
        d[col] = pd.to_datetime(d[col], errors="coerce").dt.normalize()
    numeric_cols = [
        "entry_price",
        "exit_price",
        "gross_ret",
        "net_ret",
        "rank_key",
        "wave_style_score",
        "amount_rank",
        "selected_score",
        "index_mom20",
        "index_mom60",
        "sector_candidate_count",
        "sector_share",
        "sector_avg_score",
        "sector_avg_ret5",
        "sector_avg_ret20",
        "sector_candidate_count_chg5",
        "sector_diffusion_score",
        "m30_close_above_ma20",
        "m30_close_above_ma40",
        "m30_mom6",
        "m30_mom12",
        "m30_day_ret",
        "m30_day_close_pos",
        "m30_day_amp",
        "m30_last_bar_ret",
        "m30_amount_last2_ratio",
    ]
    for col in numeric_cols:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["code", "trade_date", "entry_date", "policy_exit_date", "entry_price", "net_ret"]).copy()
    d["name"] = d.get("stock_name", d.get("name", "")).fillna("").astype(str)
    d["score"] = pd.to_numeric(d.get("selected_score"), errors="coerce").fillna(pd.to_numeric(d.get("rank_key"), errors="coerce"))
    d["mode"] = "institutional_mainwave"
    d["route"] = "institutional_mainwave"
    d["mode_pick_rank"] = 1
    d["router_candidate_rank"] = d.groupby("entry_date")["rank_key"].rank(method="first", ascending=False)
    d["sector_for_distinct"] = d.get("l2_sector_name", "").fillna("").astype(str)
    d["sector_code"] = "qmt:SW2" + d["sector_for_distinct"].astype(str)
    d["trade_key"] = d["route"] + "|" + d["entry_date"].dt.strftime("%Y-%m-%d") + "|" + d["code"].astype(str)
    return d.sort_values(["entry_date", "rank_key", "amount_rank"], ascending=[True, False, False]).reset_index(drop=True)


def _load_sector_kline_features(min_date: pd.Timestamp, max_date: pd.Timestamp) -> pd.DataFrame:
    start = (min_date - pd.Timedelta(days=260)).strftime("%Y-%m-%d")
    end = max_date.strftime("%Y-%m-%d")
    raw = clickhouse_query_df(
        f"""
        SELECT
            code,
            trade_date,
            open,
            high,
            low,
            close,
            change_pct,
            stock_count,
            rise_count,
            limit_up_count,
            total_amount
        FROM sector_kline_daily
        WHERE code LIKE 'qmt:SW2%'
          AND trade_date BETWEEN toDate('{start}') AND toDate('{end}')
        ORDER BY code, trade_date
        """
    )
    if raw.empty:
        return pd.DataFrame()
    raw["trade_date"] = pd.to_datetime(raw["trade_date"], errors="coerce").dt.normalize()
    for col in ["open", "high", "low", "close", "change_pct", "stock_count", "rise_count", "limit_up_count", "total_amount"]:
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    frames: list[pd.DataFrame] = []
    for _, g in raw.sort_values(["code", "trade_date"]).groupby("code", dropna=False):
        x = g.copy()
        close = x["close"]
        amount = x["total_amount"]
        x["sector_mom20"] = close / close.shift(20) - 1.0
        x["sector_mom60"] = close / close.shift(60) - 1.0
        x["sector_mom120"] = close / close.shift(120) - 1.0
        x["sector_ma20"] = close.rolling(20, min_periods=10).mean()
        x["sector_ma60"] = close.rolling(60, min_periods=30).mean()
        x["sector_close_vs_ma20"] = close / x["sector_ma20"] - 1.0
        x["sector_close_vs_ma60"] = close / x["sector_ma60"] - 1.0
        x["sector_high60"] = close.rolling(60, min_periods=30).max()
        x["sector_close_vs_high60"] = close / x["sector_high60"] - 1.0
        x["sector_amount_ma20"] = amount.rolling(20, min_periods=10).mean()
        x["sector_amount_ma60"] = amount.rolling(60, min_periods=30).mean()
        x["sector_amount_ratio20_60"] = x["sector_amount_ma20"] / x["sector_amount_ma60"]
        x["sector_rise_rate"] = x["rise_count"] / x["stock_count"].where(x["stock_count"] > 0)
        x["sector_limit_up_rate"] = x["limit_up_count"] / x["stock_count"].where(x["stock_count"] > 0)
        frames.append(x)
    out = pd.concat(frames, ignore_index=True, sort=False)
    keep = [
        "code",
        "trade_date",
        "change_pct",
        "stock_count",
        "rise_count",
        "limit_up_count",
        "total_amount",
        "sector_mom20",
        "sector_mom60",
        "sector_mom120",
        "sector_close_vs_ma20",
        "sector_close_vs_ma60",
        "sector_close_vs_high60",
        "sector_amount_ratio20_60",
        "sector_rise_rate",
        "sector_limit_up_rate",
    ]
    return out[keep].rename(columns={"code": "sector_code"})


def _enrich_sector_features(source: pd.DataFrame) -> pd.DataFrame:
    features = _load_sector_kline_features(source["trade_date"].min(), source["trade_date"].max())
    if features.empty:
        out = source.copy()
        for col in [
            "sector_mom20",
            "sector_mom60",
            "sector_mom120",
            "sector_close_vs_ma20",
            "sector_close_vs_ma60",
            "sector_close_vs_high60",
            "sector_amount_ratio20_60",
            "sector_rise_rate",
            "sector_limit_up_rate",
        ]:
            out[col] = pd.NA
        return out
    return source.merge(features, on=["sector_code", "trade_date"], how="left")


def _variant_mask(d: pd.DataFrame, name: str) -> pd.Series:
    diff = pd.to_numeric(d["sector_diffusion_score"], errors="coerce")
    mom60 = pd.to_numeric(d["index_mom60"], errors="coerce")
    m30 = pd.to_numeric(d["m30_close_above_ma20"], errors="coerce")
    sector_mom20 = pd.to_numeric(d["sector_mom20"], errors="coerce")
    sector_mom60 = pd.to_numeric(d["sector_mom60"], errors="coerce")
    sector_close_ma20 = pd.to_numeric(d["sector_close_vs_ma20"], errors="coerce")
    sector_close_ma60 = pd.to_numeric(d["sector_close_vs_ma60"], errors="coerce")
    sector_high60 = pd.to_numeric(d["sector_close_vs_high60"], errors="coerce")
    sector_amount = pd.to_numeric(d["sector_amount_ratio20_60"], errors="coerce")
    rise_rate = pd.to_numeric(d["sector_rise_rate"], errors="coerce")
    rank_key = pd.to_numeric(d["rank_key"], errors="coerce")
    base_runtime = (m30 >= 0.0) & (mom60 <= 0.05)
    core = diff >= 65.0
    band55_65 = (diff >= 55.0) & (diff < 65.0)
    band60_65 = (diff >= 60.0) & (diff < 65.0)
    trend_ok = (
        (sector_mom20 >= 0.06)
        & (sector_mom60 >= 0.08)
        & (sector_close_ma20 >= 0.0)
        & (sector_amount >= 1.0)
        & (rise_rate >= 0.48)
    )
    strong_trend_ok = (
        (sector_mom20 >= 0.10)
        & (sector_mom60 >= 0.12)
        & (sector_close_ma20 >= 0.0)
        & (sector_close_ma60 >= 0.0)
        & (sector_amount >= 1.05)
        & (rise_rate >= 0.50)
    )
    breakout_ok = (
        (sector_mom20 >= 0.08)
        & (sector_high60 >= -0.03)
        & (sector_close_ma20 >= 0.0)
        & (sector_amount >= 1.05)
        & (rise_rate >= 0.50)
    )
    if name == "baseline_diff65":
        return base_runtime & core
    if name == "near65_sector_trend":
        return base_runtime & (core | (band60_65 & trend_ok))
    if name == "near65_sector_breakout":
        return base_runtime & (core | (band60_65 & breakout_ok))
    if name == "band55_sector_strong_trend":
        return base_runtime & (core | (band55_65 & strong_trend_ok & (rank_key >= 102.0)))
    if name == "band55_sector_trend_no_rank":
        return base_runtime & (core | (band55_65 & strong_trend_ok))
    if name == "sector_trend_only_no_diff":
        return base_runtime & trend_ok
    if name == "no_diffusion_gate":
        return base_runtime
    raise KeyError(name)


def _summary(curve: pd.DataFrame, closed: pd.DataFrame, prepared: pd.DataFrame, signals: pd.DataFrame, variant: str) -> dict[str, Any]:
    rets = pd.to_numeric(closed.get("net_ret"), errors="coerce").dropna() if not closed.empty else pd.Series(dtype=float)
    equity = pd.to_numeric(curve.get("equity"), errors="coerce").dropna() if not curve.empty else pd.Series(dtype=float)
    return {
        "variant": variant,
        "signal_rows": int(len(signals)),
        "signal_days": int(signals["entry_date"].nunique()) if not signals.empty else 0,
        "prepared_rows": int(len(prepared)),
        "closed_trades": int(len(closed)),
        "return": float(equity.iloc[-1] / equity.iloc[0] - 1.0) if len(equity) else 0.0,
        "max_drawdown": float(_max_drawdown(equity)) if len(equity) else 0.0,
        "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
        "avg_trade_return": float(rets.mean()) if len(rets) else 0.0,
        "worst_trade": float(rets.min()) if len(rets) else 0.0,
        "best_trade": float(rets.max()) if len(rets) else 0.0,
        "sum_pnl": float(pd.to_numeric(closed.get("realized_pnl"), errors="coerce").sum()) if not closed.empty else 0.0,
    }


def _concentration(closed: pd.DataFrame) -> dict[str, Any]:
    if closed.empty:
        return {}
    d = closed.copy()
    pnl = pd.to_numeric(d.get("realized_pnl"), errors="coerce").fillna(0.0)
    total = float(pnl.sum())
    out: dict[str, Any] = {"total_pnl": total}
    if total:
        ordered = d.assign(_pnl=pnl).sort_values("_pnl", ascending=False)
        for n in [1, 3, 5, 10]:
            out[f"top{n}_pnl_share"] = float(ordered.head(n)["_pnl"].sum() / total)
    return out


def _by_year(closed: pd.DataFrame) -> pd.DataFrame:
    if closed.empty:
        return pd.DataFrame()
    d = closed.copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce")
    return (
        d.groupby(d["entry_date"].dt.year)
        .agg(
            trades=("code", "count"),
            pnl=("realized_pnl", "sum"),
            avg_ret=("net_ret", "mean"),
            win=("net_ret", lambda x: float((pd.to_numeric(x, errors="coerce") > 0).mean())),
            best=("net_ret", "max"),
            worst=("net_ret", "min"),
        )
        .reset_index(names="year")
    )


def _md_table(df: pd.DataFrame, pct_cols: set[str]) -> str:
    if df.empty:
        return "_No data_"
    d = df.copy()
    for col in pct_cols:
        if col in d.columns:
            d[col] = d[col].map(_pct)
    return d.to_markdown(index=False)


def run() -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    source = _enrich_sector_features(_read_source())
    variants = [
        "baseline_diff65",
        "near65_sector_trend",
        "near65_sector_breakout",
        "band55_sector_strong_trend",
        "band55_sector_trend_no_rank",
        "sector_trend_only_no_diff",
        "no_diffusion_gate",
    ]
    masks = {name: _variant_mask(source, name) for name in variants}
    union_mask = pd.Series(False, index=source.index)
    for mask in masks.values():
        union_mask |= mask

    contract = next(c for c in CONTRACTS if c.name == CONTRACT_NAME)
    price_context = _build_price_context(source[union_mask].copy())
    summaries: list[dict[str, Any]] = []
    baseline_keys = set(source[masks["baseline_diff65"]]["trade_key"].astype(str))
    rescued_frames: list[pd.DataFrame] = []
    concentration_rows: list[dict[str, Any]] = []
    year_frames: list[pd.DataFrame] = []

    for variant in variants:
        signals = source[masks[variant]].copy()
        prepared = _prepare_contract_candidates(signals, contract, price_context)
        if not prepared.empty:
            prepared["candidate_variant"] = variant
            prepared["router_candidate_rank"] = prepared.groupby("entry_date")["rank_key"].rank(method="first", ascending=False)
        curve, closed, decisions = _simulate_portfolio(
            prepared,
            variant=variant,
            daily_open_limit=2,
            direction_col="sector_for_distinct",
            allow_same_direction_mainwave=True,
        )
        variant_dir = OUT_DIR / variant
        variant_dir.mkdir(parents=True, exist_ok=True)
        signals.to_csv(variant_dir / "signals.csv", index=False, encoding="utf-8-sig")
        prepared.to_csv(variant_dir / "prepared_candidates.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(variant_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(variant_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        decisions.to_csv(variant_dir / "daily_decisions.csv", index=False, encoding="utf-8-sig")
        summary = _summary(curve, closed, prepared, signals, variant)
        summaries.append(summary)
        concentration_rows.append({"variant": variant, **_concentration(closed)})
        by_year = _by_year(closed)
        if not by_year.empty:
            by_year.insert(0, "variant", variant)
            year_frames.append(by_year)
        if variant != "baseline_diff65" and not signals.empty:
            rescued = signals[~signals["trade_key"].astype(str).isin(baseline_keys)].copy()
            if not rescued.empty:
                rescued["variant"] = variant
                rescued_frames.append(rescued)

    summary_df = pd.DataFrame(summaries)
    concentration_df = pd.DataFrame(concentration_rows)
    year_df = pd.concat(year_frames, ignore_index=True, sort=False) if year_frames else pd.DataFrame()
    rescued_df = pd.concat(rescued_frames, ignore_index=True, sort=False) if rescued_frames else pd.DataFrame()
    summary_df.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    concentration_df.to_csv(OUT_DIR / "concentration.csv", index=False, encoding="utf-8-sig")
    year_df.to_csv(OUT_DIR / "by_year.csv", index=False, encoding="utf-8-sig")
    if not rescued_df.empty:
        rescued_cols = [
            "variant",
            "entry_date",
            "code",
            "name",
            "l2_sector_name",
            "net_ret",
            "rank_key",
            "sector_diffusion_score",
            "sector_mom20",
            "sector_mom60",
            "sector_close_vs_ma20",
            "sector_amount_ratio20_60",
            "sector_rise_rate",
            "m30_mom6",
            "m30_day_close_pos",
            "m30_last_bar_ret",
            "index_mom60",
        ]
        rescued_df[[c for c in rescued_cols if c in rescued_df.columns]].to_csv(
            OUT_DIR / "rescued_signal_rows.csv",
            index=False,
            encoding="utf-8-sig",
        )

    report_lines = [
        "# Score120 sector natural gate audit",
        "",
        "This is a research-only audit. It does not change the formal G3 contract.",
        "",
        "## Summary",
        "",
        _md_table(
            summary_df,
            {"return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "best_trade"},
        ),
        "",
        "## Concentration",
        "",
        _md_table(concentration_df, {"top1_pnl_share", "top3_pnl_share", "top5_pnl_share", "top10_pnl_share"}),
        "",
        "## By year",
        "",
        _md_table(year_df, {"avg_ret", "win", "best", "worst"}),
        "",
        "## Variant definitions",
        "",
        "- baseline_diff65: formal gate, sector_diffusion_score >= 65.",
        "- near65_sector_trend: baseline OR 60-65 diffusion band with sector 20d/60d momentum, MA20, amount, and breadth confirmation.",
        "- near65_sector_breakout: baseline OR 60-65 diffusion band near 60d high with amount and breadth confirmation.",
        "- band55_sector_strong_trend: baseline OR 55-65 diffusion band with stricter sector trend plus rank_key >= 102.",
        "- band55_sector_trend_no_rank: same sector trend, without stock rank rescue.",
        "- sector_trend_only_no_diff: ignores diffusion, tests whether sector K-line trend alone is enough.",
        "- no_diffusion_gate: removes the sector gate, used only as a negative control.",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(report_lines), encoding="utf-8")
    payload = {
        "status": "completed",
        "out_dir": str(OUT_DIR),
        "source": str(SOURCE),
        "summaries": summaries,
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    print(json.dumps(run(), ensure_ascii=False, default=_json_default))


if __name__ == "__main__":
    main()
