from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default, _load_trade_dates, _pct  # noqa: E402
from scripts.gen2_candidate_outcome_supervision import _prepare_source  # noqa: E402
from scripts.gen2_compare_prev_low_exit_fills import FillProfile, _run_profile  # noqa: E402


DEFAULT_CANDIDATES = ROOT / "reports" / "gen2_candidate_outcome_supervision" / "labeled_candidates.parquet"
DEFAULT_BASE_TRADES = ROOT / "reports" / "gen2_candidate_outcome_supervision" / "backtests" / "risk_cool_v2" / "trades.csv"
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "gen2_risk_cool_position_controls"


def _base_mask(d: pd.DataFrame) -> pd.Series:
    return (
        pd.to_numeric(d["mom20"], errors="coerce").le(0.30)
        & pd.to_numeric(d["mom5"], errors="coerce").le(0.14)
        & pd.to_numeric(d["vol_ratio"], errors="coerce").le(1.90)
        & pd.to_numeric(d["rt_return_from_d1_close"], errors="coerce").between(0.00, 0.12)
        & pd.to_numeric(d["rt_30m_amount_ratio"], errors="coerce").between(1.5, 7.0)
    )


def _cooldown_dates_from_base_trades(base_trades: Path, trade_dates: list[str], days: int) -> set[str]:
    if days <= 0 or not base_trades.exists():
        return set()
    trades = pd.read_csv(base_trades)
    if trades.empty:
        return set()
    trades["sell_date"] = pd.to_datetime(trades["sell_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    stops = sorted(set(trades.loc[trades["exit_reason"].astype(str).eq("stop_loss_30m"), "sell_date"].dropna().astype(str)))
    idx = {date: i for i, date in enumerate(trade_dates)}
    blocked: set[str] = set()
    for stop_date in stops:
        pos = idx.get(stop_date)
        if pos is None:
            continue
        for offset in range(1, days + 1):
            if pos + offset < len(trade_dates):
                blocked.add(trade_dates[pos + offset])
    return blocked


def _policy_weights(d: pd.DataFrame, policy: str, cooldown_dates: set[str]) -> pd.Series:
    weight = pd.Series(1.0, index=d.index)
    regime = d["legacy_regime"].astype(str)
    breadth = pd.to_numeric(d["market_breadth"], errors="coerce")
    idx_mom20 = pd.to_numeric(d["index_mom20"], errors="coerce")
    idx_ge_ma20 = d["index_close_ge_ma20"].fillna(False).astype(bool)
    entry_date = d["entry_date"].astype(str)

    if policy == "base_full":
        return weight
    if policy == "trend_down_half":
        weight.loc[regime.eq("trend_down")] = 0.5
        return weight
    if policy == "not_ma20_half":
        weight.loc[~idx_ge_ma20] = 0.5
        return weight
    if policy == "weak_mom_half":
        weight.loc[idx_mom20.le(0)] = 0.5
        return weight
    if policy == "breadth_lt55_half":
        weight.loc[breadth.lt(0.55)] = 0.5
        return weight
    if policy == "risk_off_half":
        weight.loc[(~idx_ge_ma20) | idx_mom20.le(0) | breadth.lt(0.55) | regime.eq("trend_down")] = 0.5
        return weight
    if policy == "cooldown_skip":
        weight.loc[entry_date.isin(cooldown_dates)] = 0.0
        return weight
    if policy == "cooldown_half":
        weight.loc[entry_date.isin(cooldown_dates)] = 0.5
        return weight
    if policy == "risk_off_plus_cooldown_half":
        weight.loc[(~idx_ge_ma20) | idx_mom20.le(0) | breadth.lt(0.55) | regime.eq("trend_down")] = 0.5
        weight.loc[entry_date.isin(cooldown_dates)] = np.minimum(weight.loc[entry_date.isin(cooldown_dates)], 0.5)
        return weight
    if policy == "risk_off_half_cooldown_skip":
        weight.loc[(~idx_ge_ma20) | idx_mom20.le(0) | breadth.lt(0.55) | regime.eq("trend_down")] = 0.5
        weight.loc[entry_date.isin(cooldown_dates)] = 0.0
        return weight
    raise ValueError(f"Unknown policy: {policy}")


def _segment_rows(equity_path: Path) -> list[dict[str, Any]]:
    e = pd.read_csv(equity_path)
    e["date"] = pd.to_datetime(e["date"], errors="coerce")
    e["strategy_equity"] = pd.to_numeric(e["strategy_equity"], errors="coerce")
    e["drawdown"] = pd.to_numeric(e["drawdown"], errors="coerce")
    windows = [
        ("2024H2-2025Q1", "2024-07-09", "2025-03-31"),
        ("2025Q2-Q4", "2025-04-01", "2025-12-31"),
        ("2026YTD", "2026-01-01", "2026-05-21"),
    ]
    rows = []
    for name, start, end in windows:
        g = e[(e["date"] >= pd.Timestamp(start)) & (e["date"] <= pd.Timestamp(end))].copy()
        if g.empty:
            continue
        first = float(g.iloc[0]["strategy_equity"])
        last = float(g.iloc[-1]["strategy_equity"])
        rows.append({"segment": name, "return": last / first - 1.0 if first else None, "max_drawdown": float(g["drawdown"].min())})
    return rows


def _write_report(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# G2 risk_cool Position Control Sweep",
        "",
        "All policies keep the same candidate rule and exits. Only `capital_weight` changes.",
        "Cooldown policies are a first approximation built from previous stop-loss dates in the base run; they should be upgraded to an exact dynamic simulator before deployment.",
        "",
        "| policy | cooldown | avg_weight | zero_weight | signals | trades | total | excess | max_dd | win | avg_trade |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['policy']} | {row['cooldown_days']} | {row['avg_capital_weight']:.2f} | {row['zero_weight_signals']} | "
            f"{row.get('signal_count', 0)} | {row.get('trade_count', 0)} | {_pct(row.get('total_return'))} | "
            f"{_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | "
            f"{_pct(row.get('avg_trade_return'))} |"
        )
    (output_dir / "position_control_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates = pd.read_parquet(args.candidates)
    candidates["entry_date"] = pd.to_datetime(candidates["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    selected = candidates[_base_mask(candidates).fillna(False)].copy()
    trade_dates = _load_trade_dates(str(args.start_date), str(args.end_date))
    profile = FillProfile("gap_confirm_30m_close__intraday_30m_close", gap_open_mode="confirm_30m", intraday_mode="bar_close")
    policies = [x.strip() for x in str(args.policies).split(",") if x.strip()]
    rows: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []

    for policy in policies:
        cooldown_values = [0]
        if "cooldown" in policy:
            cooldown_values = [int(x) for x in str(args.cooldown_days).split(",") if x.strip()]
        for cooldown_days in cooldown_values:
            cooldown_dates = _cooldown_dates_from_base_trades(Path(args.base_trades), trade_dates, cooldown_days)
            d = selected.copy()
            d["capital_weight"] = _policy_weights(d, policy, cooldown_dates)
            key = f"{policy}__cd{cooldown_days}"
            source = output_dir / "sources" / f"{key}.parquet"
            source.parent.mkdir(parents=True, exist_ok=True)
            prepared = _prepare_source(d)
            prepared.to_parquet(source, index=False)
            prepared.to_csv(source.with_suffix(".csv"), index=False, encoding="utf-8-sig")
            run_dir = output_dir / "backtests" / key
            summary = _run_profile(
                signal_source=source,
                output_dir=run_dir,
                profile=profile,
                start_date=str(args.start_date),
                end_date=str(args.end_date),
                sort_mode="trigger_time",
            )
            summary["policy"] = policy
            summary["cooldown_days"] = int(cooldown_days)
            summary["cooldown_date_count"] = int(len(cooldown_dates))
            summary["avg_capital_weight"] = float(d["capital_weight"].mean())
            summary["zero_weight_signals"] = int((d["capital_weight"] <= 0).sum())
            rows.append(summary)
            for seg in _segment_rows(run_dir / "equity_curve.csv"):
                segments.append({"policy": policy, "cooldown_days": int(cooldown_days), **seg})

    df = pd.DataFrame(rows).sort_values(["max_drawdown", "total_return"], ascending=[False, False])
    seg_df = pd.DataFrame(segments)
    df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    seg_df.to_csv(output_dir / "segment_summary.csv", index=False, encoding="utf-8-sig")
    _write_report(output_dir, df.to_dict("records"))
    payload = {
        "schema_version": 1,
        "rows": df.where(pd.notna(df), None).to_dict("records"),
        "outputs": {"summary": "summary.csv", "segments": "segment_summary.csv", "report": "position_control_report.md"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Position control sweep for G2 risk_cool_v2.")
    parser.add_argument("--candidates", default=str(DEFAULT_CANDIDATES))
    parser.add_argument("--base-trades", default=str(DEFAULT_BASE_TRADES))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument(
        "--policies",
        default="base_full,trend_down_half,not_ma20_half,weak_mom_half,breadth_lt55_half,risk_off_half,cooldown_half,cooldown_skip,risk_off_plus_cooldown_half,risk_off_half_cooldown_skip",
    )
    parser.add_argument("--cooldown-days", default="3,5")
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
