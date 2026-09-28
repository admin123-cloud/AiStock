from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_RUNS = [
    "score_ge_050=reports/gen2_alpha191_keep80_policy_sweep/backtests/score_ge_050/trigger_time/stop_cd3_skip",
    "score050_overhead005=reports/gen2_alpha191_keep80_policy_sweep/backtests/score050_overhead005/trigger_time/stop_cd3_skip",
    "baseline_keep80=reports/gen2_alpha191_keep80_policy_sweep/backtests/baseline_keep80/trigger_time/stop_cd3_skip",
]


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _money(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value):.2f}"


def _max_drawdown_window(curve: pd.DataFrame) -> dict[str, Any]:
    d = curve.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d["strategy_equity"] = pd.to_numeric(d["strategy_equity"], errors="coerce")
    d["drawdown"] = pd.to_numeric(d["drawdown"], errors="coerce")
    trough_idx = d["drawdown"].idxmin()
    trough = d.loc[trough_idx]
    before = d.loc[:trough_idx].copy()
    peak_idx = before["strategy_equity"].idxmax()
    peak = d.loc[peak_idx]
    after = d.loc[trough_idx:].copy()
    recovered = after[after["strategy_equity"] >= float(peak["strategy_equity"])]
    recovery_date = None if recovered.empty else recovered.iloc[0]["date"].strftime("%Y-%m-%d")
    return {
        "peak_date": peak["date"].strftime("%Y-%m-%d"),
        "trough_date": trough["date"].strftime("%Y-%m-%d"),
        "recovery_date": recovery_date,
        "peak_equity": float(peak["strategy_equity"]),
        "trough_equity": float(trough["strategy_equity"]),
        "drawdown": float(trough["drawdown"]),
        "days": int((trough["date"] - peak["date"]).days),
    }


def _window_trades(trades: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    d = trades.copy()
    d["buy_date"] = pd.to_datetime(d["buy_date"], errors="coerce")
    d["sell_date"] = pd.to_datetime(d["sell_date"], errors="coerce")
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    return d[(d["buy_date"] <= end_ts) & (d["sell_date"] >= start_ts)].copy()


def _attach_signal_features(trades: pd.DataFrame, signals: pd.DataFrame) -> pd.DataFrame:
    if trades.empty or signals.empty:
        return trades
    t = trades.copy()
    s = signals.copy()
    t["buy_datetime_key"] = pd.to_datetime(t["buy_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    s["confirm_datetime_key"] = pd.to_datetime(s["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    cols = [
        "code",
        "confirm_datetime_key",
        "alpha191_gate_score",
        "overhead_pressure_amount_share",
        "rt_30m_amount_ratio",
        "mom5",
        "mom20",
        "vol_ratio",
        "cap_bucket",
        "rank_change_status",
        "downtrend_rebound_reject",
        "entry_pass",
    ]
    keep = [c for c in cols if c in s.columns]
    merged = t.merge(s[keep], left_on=["code", "buy_datetime_key"], right_on=["code", "confirm_datetime_key"], how="left")
    return merged


def _analyze_run(name: str, run_dir: Path) -> dict[str, Any]:
    curve = pd.read_csv(run_dir / "equity_curve.csv")
    trades = pd.read_csv(run_dir / "trades.csv")
    signals = pd.read_csv(run_dir / "signals.csv")
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    window = _max_drawdown_window(curve)
    dd_trades = _window_trades(trades, window["peak_date"], window["trough_date"])
    enriched = _attach_signal_features(dd_trades, signals)
    if not enriched.empty:
        enriched["pnl"] = pd.to_numeric(enriched["pnl"], errors="coerce")
        enriched["return"] = pd.to_numeric(enriched["return"], errors="coerce")
    losers = enriched[enriched["pnl"] < 0].sort_values("pnl").copy() if not enriched.empty else pd.DataFrame()
    exit_counts = enriched["exit_reason"].fillna("").astype(str).value_counts().to_dict() if not enriched.empty else {}
    cap_counts = enriched["cap_bucket"].fillna("").astype(str).value_counts().to_dict() if "cap_bucket" in enriched else {}
    downtrend_count = int(enriched["downtrend_rebound_reject"].fillna(False).astype(bool).sum()) if "downtrend_rebound_reject" in enriched else 0
    bought_rows = enriched.sort_values(["buy_date", "buy_datetime", "code"]).copy() if not enriched.empty else pd.DataFrame()
    out_cols = [
        "code",
        "name",
        "buy_date",
        "sell_date",
        "buy_datetime",
        "sell_datetime",
        "pnl",
        "return",
        "exit_reason",
        "v4_rank",
        "v4_score",
        "alpha191_gate_score",
        "overhead_pressure_amount_share",
        "rt_30m_amount_ratio",
        "mom5",
        "mom20",
        "cap_bucket",
        "rank_change_status",
        "downtrend_rebound_reject",
    ]
    available = [c for c in out_cols if c in bought_rows.columns]
    return {
        "name": name,
        "run_dir": str(run_dir),
        "summary": summary,
        "window": window,
        "window_trade_count": int(len(enriched)),
        "window_pnl": None if enriched.empty else float(enriched["pnl"].sum()),
        "window_avg_return": None if enriched.empty else float(enriched["return"].mean()),
        "window_exit_counts": exit_counts,
        "window_cap_counts": cap_counts,
        "window_downtrend_rebound_count": downtrend_count,
        "worst_trades": losers.head(10)[available].to_dict("records") if not losers.empty else [],
        "window_trades": bought_rows[available].to_dict("records") if not bought_rows.empty else [],
    }


def _parse_runs(items: list[str]) -> list[tuple[str, Path]]:
    runs: list[tuple[str, Path]] = []
    for item in items:
        if "=" in item:
            name, path = item.split("=", 1)
        else:
            path = item
            name = Path(path).parents[2].name if len(Path(path).parents) >= 3 else Path(path).name
        runs.append((name, Path(path)))
    return runs


def _write_report(output_dir: Path, analyses: list[dict[str, Any]]) -> None:
    lines = [
        "# Alpha191 Drawdown Analysis",
        "",
        "Focus: locate the max drawdown window and the trades/features that caused it.",
        "",
        "| run | total | sharpe | max_dd | peak | trough | recovery | window_trades | window_pnl | exit_counts | cap_counts |",
        "| --- | ---: | ---: | ---: | --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    for item in analyses:
        s = item["summary"]
        w = item["window"]
        lines.append(
            f"| {item['name']} | {_pct(s.get('total_return'))} | {float(s.get('daily_sharpe') or 0):.2f} | "
            f"{_pct(w.get('drawdown'))} | {w['peak_date']} | {w['trough_date']} | {w.get('recovery_date') or ''} | "
            f"{item['window_trade_count']} | {_money(item.get('window_pnl'))} | "
            f"{json.dumps(item['window_exit_counts'], ensure_ascii=False, sort_keys=True)} | "
            f"{json.dumps(item['window_cap_counts'], ensure_ascii=False, sort_keys=True)} |"
        )
    for item in analyses:
        lines.extend(["", f"## {item['name']}", "", "Worst trades in max drawdown window:", ""])
        lines.append("| code | name | buy | sell | pnl | return | exit | rank | score | alpha | pressure | amount_ratio | cap |")
        lines.append("| --- | --- | --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |")
        for row in item["worst_trades"]:
            lines.append(
                f"| {row.get('code','')} | {row.get('name','')} | {row.get('buy_datetime','')} | {row.get('sell_datetime','')} | "
                f"{_money(row.get('pnl'))} | {_pct(row.get('return'))} | {row.get('exit_reason','')} | "
                f"{row.get('v4_rank','')} | {float(row.get('v4_score') or 0):.3f} | "
                f"{float(row.get('alpha191_gate_score') or 0):.3f} | {_pct(row.get('overhead_pressure_amount_share'))} | "
                f"{float(row.get('rt_30m_amount_ratio') or 0):.2f} | {row.get('cap_bucket','')} |"
            )
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze Alpha191 keep80 drawdown windows.")
    parser.add_argument("--runs", nargs="*", default=DEFAULT_RUNS)
    parser.add_argument("--output-dir", default="reports/gen2_alpha191_drawdown_analysis")
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    analyses = [_analyze_run(name, path) for name, path in _parse_runs(args.runs)]
    _write_report(output_dir, analyses)
    (output_dir / "summary.json").write_text(json.dumps(analyses, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir), "runs": [a["name"] for a in analyses]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
