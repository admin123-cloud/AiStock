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

from scripts.backtest_wave_style_template_strategy_v1 import _max_drawdown, _trade_calendar  # noqa: E402
from scripts.gen3_full_candidate_2slot_backtest_v1 import _simulate_portfolio  # noqa: E402
from scripts.gen3_promotion_self_test_v1 import (  # noqa: E402
    CONTRACTS,
    INITIAL_CAPITAL,
    _build_price_context,
    _prepare_contract_candidates,
)
from utils.paths import report_path  # noqa: E402


SOURCE = report_path("score120_sector_diffusion_30m_overlay_v1", "base_trades_with_sector_diffusion_30m.csv")
OUT_DIR = report_path("score120_sector_diffusion_rescue_band_v1")
CONTRACT_NAME = "g3_2slot_50_default_stop12_take12_prevlow"


def _pct(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return ""
    if not math.isfinite(x):
        return ""
    return f"{x:.2%}"


def _load_source() -> pd.DataFrame:
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
    d = d.dropna(subset=["code", "entry_date", "policy_exit_date", "entry_price", "net_ret"]).copy()
    d["name"] = d.get("stock_name", d.get("name", "")).fillna("").astype(str)
    d["score"] = pd.to_numeric(d.get("selected_score"), errors="coerce").fillna(pd.to_numeric(d.get("rank_key"), errors="coerce"))
    d["mode"] = "institutional_mainwave"
    d["route"] = "institutional_mainwave"
    d["mode_pick_rank"] = 1
    d["router_candidate_rank"] = d.groupby("entry_date")["rank_key"].rank(method="first", ascending=False)
    d["sector_for_distinct"] = d.get("l2_sector_name", "").fillna("").astype(str)
    d["trade_key"] = d["route"] + "|" + d["entry_date"].dt.strftime("%Y-%m-%d") + "|" + d["code"].astype(str)
    return d.sort_values(["entry_date", "rank_key", "amount_rank"], ascending=[True, False, False]).reset_index(drop=True)


def _variant_mask(d: pd.DataFrame, name: str) -> pd.Series:
    diff = pd.to_numeric(d["sector_diffusion_score"], errors="coerce")
    mom60 = pd.to_numeric(d["index_mom60"], errors="coerce")
    m30 = pd.to_numeric(d["m30_close_above_ma20"], errors="coerce")
    m30_mom6 = pd.to_numeric(d.get("m30_mom6"), errors="coerce")
    m30_close_pos = pd.to_numeric(d.get("m30_day_close_pos"), errors="coerce")
    last_bar = pd.to_numeric(d.get("m30_last_bar_ret"), errors="coerce")
    rank_key = pd.to_numeric(d.get("rank_key"), errors="coerce")
    amount_rank = pd.to_numeric(d.get("amount_rank"), errors="coerce")
    base_runtime = (m30 >= 0.0) & (mom60 <= 0.05)
    core = diff >= 65.0
    rescue_band = (diff >= 60.0) & (diff < 65.0)
    if name == "baseline_diff65":
        return base_runtime & core
    if name == "broad_diff60":
        return base_runtime & (diff >= 60.0)
    if name == "rescue_60_65_rank102":
        return base_runtime & (core | (rescue_band & (rank_key >= 102.0)))
    if name == "rescue_60_65_m30_positive":
        return base_runtime & (core | (rescue_band & (m30_mom6 >= 0.0) & (last_bar <= 0.03)))
    if name == "rescue_60_65_quality":
        return base_runtime & (
            core
            | (
                rescue_band
                & (rank_key >= 102.0)
                & (m30_mom6 >= 0.0)
                & (m30_close_pos >= 0.45)
                & (last_bar <= 0.03)
            )
        )
    if name == "rescue_60_65_quality_amount":
        return base_runtime & (
            core
            | (
                rescue_band
                & (rank_key >= 102.0)
                & (amount_rank >= 0.0)
                & (m30_mom6 >= 0.0)
                & (m30_close_pos >= 0.45)
                & (last_bar <= 0.03)
            )
        )
    if name == "rescue_strong_stock_anydiff":
        strong_stock = (
            (diff < 65.0)
            & (rank_key >= 107.8)
            & (m30_mom6 >= 0.0)
            & (m30_close_pos >= 0.70)
            & (last_bar <= 0.025)
        )
        return base_runtime & (core | strong_stock)
    if name == "rescue_strong_stock_plus_near65":
        strong_stock = (
            (diff < 65.0)
            & (rank_key >= 107.8)
            & (m30_mom6 >= 0.0)
            & (m30_close_pos >= 0.70)
            & (last_bar <= 0.025)
        )
        near65_quality = (
            rescue_band
            & (rank_key >= 102.0)
            & (m30_mom6 >= 0.0)
            & (m30_close_pos >= 0.70)
            & (last_bar <= 0.02)
        )
        return base_runtime & (core | strong_stock | near65_quality)
    if name == "no_diffusion_gate":
        return base_runtime
    raise KeyError(name)


def _summary(curve: pd.DataFrame, closed: pd.DataFrame, prepared: pd.DataFrame, signals: pd.DataFrame, variant: str) -> dict[str, Any]:
    rets = pd.to_numeric(closed.get("net_ret"), errors="coerce").dropna() if not closed.empty else pd.Series(dtype=float)
    open_pos = pd.to_numeric(curve.get("open_positions"), errors="coerce").fillna(0) if not curve.empty else pd.Series(dtype=float)
    equity = pd.to_numeric(curve.get("equity"), errors="coerce").dropna() if not curve.empty else pd.Series(dtype=float)
    return {
        "variant": variant,
        "signal_rows": int(len(signals)),
        "signal_days": int(signals["entry_date"].nunique()) if not signals.empty else 0,
        "prepared_rows": int(len(prepared)),
        "closed_trades": int(len(closed)),
        "return": float(equity.iloc[-1] / equity.iloc[0] - 1.0) if len(equity) else 0.0,
        "max_drawdown": _max_drawdown(equity) if len(equity) else 0.0,
        "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
        "avg_trade_return": float(rets.mean()) if len(rets) else 0.0,
        "worst_trade": float(rets.min()) if len(rets) else 0.0,
        "best_trade": float(rets.max()) if len(rets) else 0.0,
        "sum_pnl": float(pd.to_numeric(closed.get("realized_pnl"), errors="coerce").sum()) if not closed.empty else 0.0,
        "active_days": int((open_pos > 0).sum()) if len(open_pos) else 0,
        "full_days": int((open_pos >= 2).sum()) if len(open_pos) else 0,
        "max_open_positions": int(open_pos.max()) if len(open_pos) else 0,
    }


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
    source = _load_source()
    variants = [
        "baseline_diff65",
        "broad_diff60",
        "rescue_60_65_rank102",
        "rescue_60_65_m30_positive",
        "rescue_60_65_quality",
        "rescue_60_65_quality_amount",
        "rescue_strong_stock_anydiff",
        "rescue_strong_stock_plus_near65",
        "no_diffusion_gate",
    ]
    union_mask = pd.Series(False, index=source.index)
    masks: dict[str, pd.Series] = {}
    for variant in variants:
        masks[variant] = _variant_mask(source, variant)
        union_mask |= masks[variant]
    contract = next(c for c in CONTRACTS if c.name == CONTRACT_NAME)
    price_context = _build_price_context(source[union_mask].copy())
    calendar_start = pd.Timestamp("2020-01-01")
    all_summaries: list[dict[str, Any]] = []
    rescued_rows: list[pd.DataFrame] = []
    baseline_codes = set(source[masks["baseline_diff65"]]["trade_key"].astype(str))
    for variant in variants:
        signals = source[masks[variant]].copy()
        prepared = _prepare_contract_candidates(signals, contract, price_context)
        if not prepared.empty:
            prepared["candidate_variant"] = variant
            prepared["router_candidate_rank"] = prepared.groupby("entry_date")["rank_key"].rank(method="first", ascending=False)
        end = max(pd.Timestamp("2026-07-03"), pd.to_datetime(prepared["policy_exit_date"], errors="coerce").max() if not prepared.empty else pd.Timestamp("2026-07-03"))
        calendar = _trade_calendar(calendar_start, end)
        curve, closed, decisions = _simulate_portfolio(
            prepared,
            variant=variant,
            daily_open_limit=2,
            direction_col="sector_for_distinct",
            allow_same_direction_mainwave=True,
        )
        (OUT_DIR / variant).mkdir(parents=True, exist_ok=True)
        signals.to_csv(OUT_DIR / variant / "signals.csv", index=False, encoding="utf-8-sig")
        prepared.to_csv(OUT_DIR / variant / "prepared_candidates.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(OUT_DIR / variant / "equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(OUT_DIR / variant / "closed_trades.csv", index=False, encoding="utf-8-sig")
        decisions.to_csv(OUT_DIR / variant / "daily_decisions.csv", index=False, encoding="utf-8-sig")
        all_summaries.append(_summary(curve, closed, prepared, signals, variant))
        if variant != "baseline_diff65" and not signals.empty:
            rescued = signals[~signals["trade_key"].astype(str).isin(baseline_codes)].copy()
            if not rescued.empty:
                rescued["variant"] = variant
                rescued_rows.append(rescued)
    summary = pd.DataFrame(all_summaries)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    rescued_df = pd.concat(rescued_rows, ignore_index=True, sort=False) if rescued_rows else pd.DataFrame()
    if not rescued_df.empty:
        show_cols = [
            "variant",
            "entry_date",
            "code",
            "name",
            "l2_sector_name",
            "net_ret",
            "rank_key",
            "sector_diffusion_score",
            "m30_mom6",
            "m30_day_close_pos",
            "m30_last_bar_ret",
            "index_mom60",
        ]
        rescued_df[show_cols].sort_values(["variant", "net_ret"], ascending=[True, False]).to_csv(
            OUT_DIR / "rescued_signals.csv",
            index=False,
            encoding="utf-8-sig",
        )
    pct_cols = {"return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "best_trade"}
    report = [
        "# Score120 sector diffusion rescue band audit v1",
        "",
        "## Portfolio Summary",
        "",
        _md_table(summary, pct_cols),
        "",
        "## Notes",
        "",
        "- baseline_diff65 = current hard sector diffusion gate.",
        "- rescue variants keep index_mom60<=5% and signal-day 30m close>=MA20.",
        "- Returns are recomputed with the current G3 2-slot 50% stop12/take12/prev-low contract.",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(report), encoding="utf-8")
    result = {"status": "completed", "out_dir": str(OUT_DIR), "variants": variants}
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    print(json.dumps(run(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
