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

from scripts.audit_score120_exit_payoff_contracts_v1 import _load_signals, _net  # noqa: E402
from scripts.audit_score120_hybrid_exit_contracts_v1 import _load_minute_bars  # noqa: E402
from scripts.audit_score120_mainwave_risk_contract_variants_v1 import _metrics, _simulate  # noqa: E402
from scripts.audit_score120_minute_exit_contracts_v1 import _adjust_minute_to_daily_close, _load_daily_closes  # noqa: E402
from scripts.backtest_g3_five_strategies_from_scratch_v1 import _md_table  # noqa: E402
from scripts.backtest_wave_style_template_strategy_v1 import _load_index, _trade_calendar  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("score120_entry_timing_variants_v1")
COST_BPS = 30.0


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        value = float(value)
        return value if math.isfinite(value) else None
    if pd.isna(value):
        return None
    return str(value)


def _with_ma(bars: pd.DataFrame) -> pd.DataFrame:
    if bars.empty:
        return bars
    out = bars.sort_values(["code", "datetime"]).copy()
    out["ma20"] = out.groupby("code")["close"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    out["ma40"] = out.groupby("code")["close"].transform(lambda s: s.rolling(40, min_periods=20).mean())
    out["prev_close"] = out.groupby("code")["close"].shift(1)
    return out


def _entry_variants() -> list[str]:
    return [
        "next_open_baseline",
        "skip_gap_gt5pct",
        "wait_30m_pullback_ma20_3d",
        "wait_30m_pullback_3pct_repair_3d",
        "half_now_half_pullback_ma20_3d",
        "half_now_half_pullback_3pct_3d",
    ]


def _variant_entry(row: pd.Series, bars: pd.DataFrame, variant: str) -> dict[str, Any] | None:
    base_entry = float(row["entry_price"])
    entry_date = pd.Timestamp(row["entry_date"]).normalize()
    exit_date = pd.Timestamp(row["policy_exit_date"]).normalize()
    trade_date = pd.Timestamp(row["trade_date"]).normalize()
    signal_close = float(row.get("signal_close", np.nan))
    if variant == "next_open_baseline":
        return {"entry_price": base_entry, "entry_date": entry_date, "entry_mode": "full_next_open", "position_scale": 1.0}
    if variant == "skip_gap_gt5pct":
        if math.isfinite(signal_close) and signal_close > 0 and base_entry / signal_close - 1.0 > 0.05:
            return None
        return {"entry_price": base_entry, "entry_date": entry_date, "entry_mode": "full_next_open_gap_ok", "position_scale": 1.0}

    window_end = entry_date + pd.Timedelta(days=5)
    g = bars[(bars["trade_date"] >= entry_date) & (bars["trade_date"] <= min(exit_date, window_end))].copy()
    if g.empty:
        return None
    g["bar_ret_from_base"] = g["close"] / base_entry - 1.0
    g["repair"] = g["close"] >= g["prev_close"].fillna(g["close"])

    pull: pd.Series | None = None
    if "ma20" in variant:
        candidates = g[(g["ma20"].notna()) & (g["low"] <= g["ma20"] * 1.01) & (g["close"] >= g["ma20"] * 0.995)]
        if not candidates.empty:
            pull = candidates.iloc[0]
    elif "3pct" in variant:
        candidates = g[(g["bar_ret_from_base"] <= -0.03) & (g["bar_ret_from_base"] >= -0.08) & (g["repair"])]
        if not candidates.empty:
            pull = candidates.iloc[0]

    if variant.startswith("wait_"):
        if pull is None:
            return None
        return {
            "entry_price": float(pull["close"]),
            "entry_date": pd.Timestamp(pull["trade_date"]).normalize(),
            "entry_datetime": pd.Timestamp(pull["datetime"]),
            "entry_mode": variant,
            "position_scale": 1.0,
        }
    if variant.startswith("half_"):
        if pull is None:
            return {
                "entry_price": base_entry,
                "entry_date": entry_date,
                "entry_mode": variant + "_no_fill_half_cash",
                "position_scale": 0.5,
                "blend_second_price": np.nan,
            }
        second = float(pull["close"])
        return {
            "entry_price": (base_entry + second) / 2.0,
            "entry_date": entry_date,
            "entry_datetime": pd.Timestamp(pull["datetime"]),
            "entry_mode": variant,
            "position_scale": 1.0,
            "blend_second_price": second,
        }
    return None


def _max_adverse(bars: pd.DataFrame, entry_date: pd.Timestamp, exit_date: pd.Timestamp, entry_price: float) -> float:
    if bars.empty or entry_price <= 0:
        return math.nan
    g = bars[(bars["trade_date"] >= entry_date) & (bars["trade_date"] <= exit_date)]
    if g.empty:
        return math.nan
    return float(g["low"].min() / entry_price - 1.0)


def _build_variant_trades(signals: pd.DataFrame, bars30: pd.DataFrame) -> pd.DataFrame:
    by_code = {code: g.copy() for code, g in bars30.groupby("code")} if not bars30.empty else {}
    rows: list[dict[str, Any]] = []
    for variant in _entry_variants():
        for _, row in signals.iterrows():
            code = str(row["code_raw"])
            bars = by_code.get(code, pd.DataFrame())
            entry = _variant_entry(row, bars, variant)
            if entry is None:
                skipped = row.to_dict()
                skipped.update({"entry_variant": variant, "entry_status": "skipped"})
                rows.append(skipped)
                continue
            entry_price = float(entry["entry_price"])
            exit_price = float(row["exit_price"])
            scale = float(entry.get("position_scale", 1.0))
            net_ret = scale * _net(exit_price / entry_price - 1.0)
            entry_date = pd.Timestamp(entry["entry_date"]).normalize()
            exit_date = pd.Timestamp(row["policy_exit_date"]).normalize()
            adverse = _max_adverse(bars, entry_date, exit_date, entry_price)
            out = row.to_dict()
            out.update(
                {
                    "entry_variant": variant,
                    "entry_status": "entered",
                    "entry_mode": entry.get("entry_mode"),
                    "entry_price_original": float(row["entry_price"]),
                    "entry_price": entry_price,
                    "entry_date": entry_date,
                    "policy_exit_date": exit_date,
                    "net_ret": net_ret,
                    "original_net_ret": float(row["net_ret"]),
                    "position_scale": scale,
                    "max_adverse_ret": adverse,
                    "hit_minus8": bool(math.isfinite(adverse) and adverse <= -0.08),
                    "hit_minus10": bool(math.isfinite(adverse) and adverse <= -0.10),
                    "hit_minus12": bool(math.isfinite(adverse) and adverse <= -0.12),
                    "entry_improvement": entry_price / float(row["entry_price"]) - 1.0,
                    "risk_exit_note": "policy_exit_after_entry_variant",
                }
            )
            rows.append(out)
    return pd.DataFrame(rows)


def _trade_summary(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for variant, g0 in df.groupby("entry_variant"):
        entered = g0[g0["entry_status"].eq("entered")].copy()
        ret = pd.to_numeric(entered.get("net_ret"), errors="coerce")
        adverse = pd.to_numeric(entered.get("max_adverse_ret"), errors="coerce")
        rows.append(
            {
                "entry_variant": variant,
                "source_signals": int(len(g0)),
                "entered": int(len(entered)),
                "entry_rate": float(len(entered) / len(g0)) if len(g0) else 0.0,
                "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
                "mean_ret": float(ret.mean()) if len(ret) else 0.0,
                "median_ret": float(ret.median()) if len(ret) else 0.0,
                "worst_ret": float(ret.min()) if len(ret) else 0.0,
                "mean_entry_improvement": float(pd.to_numeric(entered.get("entry_improvement"), errors="coerce").mean()) if len(entered) else 0.0,
                "avg_max_adverse": float(adverse.mean()) if len(adverse.dropna()) else math.nan,
                "worst_max_adverse": float(adverse.min()) if len(adverse.dropna()) else math.nan,
                "hit_minus8_rate": float(entered.get("hit_minus8", pd.Series(dtype=bool)).mean()) if len(entered) else 0.0,
                "hit_minus10_rate": float(entered.get("hit_minus10", pd.Series(dtype=bool)).mean()) if len(entered) else 0.0,
                "hit_minus12_rate": float(entered.get("hit_minus12", pd.Series(dtype=bool)).mean()) if len(entered) else 0.0,
            }
        )
    return pd.DataFrame(rows).sort_values(["mean_ret", "hit_minus10_rate"], ascending=[False, True])


def _portfolio_summary(trades: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    entered = trades[trades["entry_status"].eq("entered")].copy()
    calendar = _trade_calendar(pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=60))
    index_df = _load_index(start, end)
    windows = {
        "full": (start, end),
        "train_2020_2023": ("2020-01-01", "2023-12-31"),
        "valid_2024_2025": ("2024-01-01", "2025-12-31"),
        "post_2024_09": ("2024-09-24", end),
        "blind_2026ytd": ("2026-01-01", end),
    }
    rows: list[dict[str, Any]] = []
    for variant in _entry_variants():
        sig = entered[entered["entry_variant"].eq(variant)].copy()
        if sig.empty:
            continue
        # Fold half-cash no-fill into a lower realized return while keeping slot usage conservative.
        spec = {
            "variant": variant,
            "slot_pct": 0.50,
            "cooldown_after_losses": 2,
            "cooldown_mode": "dynamic_recovery",
            "min_cooldown_days": 3,
            "max_cooldown_days": 15,
        }
        curve, closed = _simulate(sig, calendar, spec, slots=2, daily_open_limit=2)
        run_dir = OUT_DIR / variant
        run_dir.mkdir(parents=True, exist_ok=True)
        curve.to_csv(run_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        for window, (ws, we) in windows.items():
            rows.append(_metrics(curve, closed, index_df, variant, window, ws, we))
    return pd.DataFrame(rows)


def run(args: argparse.Namespace) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    signals = _load_signals(args.start_date, args.end_date)
    daily = _load_daily_closes(signals)
    if not daily.empty:
        signals = signals.merge(
            daily.rename(columns={"trade_date": "signal_trade_date", "daily_close": "signal_close"}),
            left_on=["code_raw", "trade_date"],
            right_on=["code", "signal_trade_date"],
            how="left",
            suffixes=("", "_daily"),
        )
    bars30 = _with_ma(_adjust_minute_to_daily_close(_load_minute_bars(signals, 30), daily))
    trades = _build_variant_trades(signals, bars30)
    trade_summary = _trade_summary(trades)
    portfolio = _portfolio_summary(trades, args.start_date, args.end_date)

    signals.to_csv(OUT_DIR / "source_signals.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_DIR / "entry_variant_trades.csv", index=False, encoding="utf-8-sig")
    trade_summary.to_csv(OUT_DIR / "entry_variant_trade_summary.csv", index=False, encoding="utf-8-sig")
    portfolio.to_csv(OUT_DIR / "entry_variant_portfolio_summary.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "entry_rate",
        "win_rate",
        "mean_ret",
        "median_ret",
        "worst_ret",
        "mean_entry_improvement",
        "avg_max_adverse",
        "worst_max_adverse",
        "hit_minus8_rate",
        "hit_minus10_rate",
        "hit_minus12_rate",
        "strategy_ret",
        "index_ret",
        "excess_ret",
        "max_drawdown",
        "mean_trade_ret",
        "worst_trade",
    }
    full = portfolio[portfolio["window"].eq("full")].sort_values(["strategy_ret", "max_drawdown"], ascending=[False, False])
    lines = [
        "# Score120 机构主升买点时机审计 v1",
        "",
        "## 口径",
        "",
        "- 选股逻辑不变：score>=120 + sector_diffusion>=65 + 30m close>=MA20 + index_mom60<=5%。",
        "- 退出先固定为原策略政策退出，用来隔离买点质量影响。",
        "- 对比：次日开盘追、过滤高开、等待30m回踩MA20、等待3%回撤修复、半仓追半仓等回踩。",
        "- 风险观察使用入场后到政策退出期间的30m最低价，统计是否触及 -8%/-10%/-12%。",
        f"- 样本数：{len(signals)}。",
        "",
        "## 单笔买点质量",
        "",
        _md_table(trade_summary, pct_cols=pct_cols),
        "",
        "## 组合表现（全周期排序）",
        "",
        _md_table(full, pct_cols=pct_cols),
        "",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    result = {
        "status": "completed",
        "out_dir": str(OUT_DIR),
        "signals": int(len(signals)),
        "variants": _entry_variants(),
        "best_full": full.iloc[0].to_dict() if not full.empty else {},
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Score120 entry timing variants.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-06-17")
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
