from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

REPO_ROOT = _PROJECT_ROOT
sys.path.insert(0, str(REPO_ROOT))

from api.gen2_strategy import GEN2_OPEN_RULE_V1  # noqa: E402
from scripts.gen2_backtest_open_v1_portfolio import DEFAULT_SIGNAL_SOURCE, _json_default  # noqa: E402
from scripts.gen2_sweep_open_v1_semantic_exits import ExitProfile, run_profile  # noqa: E402

DEFAULT_OUTPUT_DIR = _report_path() / "gen2_attack_v1_prev_low_validation"
FIXED_PROFILE = ExitProfile("weak_prev_low", use_prev_day_low_break=True)


from research.common.reporting import percent_text as _pct


def _run_fixed(
    signal_source: Path,
    output_dir: Path,
    start_date: str,
    end_date: str,
    buy_slippage_bps: float = 5.0,
    sell_slippage_bps: float = 5.0,
    commission_bps: float = 2.5,
    stamp_tax_bps: float = 5.0,
) -> dict[str, Any]:
    return run_profile(
        signal_source=signal_source,
        output_dir=output_dir,
        profile=FIXED_PROFILE,
        start_date=start_date,
        end_date=end_date,
        hold_days=10,
        buy_slippage_bps=buy_slippage_bps,
        sell_slippage_bps=sell_slippage_bps,
        commission_bps=commission_bps,
        stamp_tax_bps=stamp_tax_bps,
    )


def _summary_row(
    group: str,
    profile: str,
    start_date: str,
    end_date: str,
    summary: dict[str, Any],
    note: str = "",
) -> dict[str, Any]:
    return {
        "group": group,
        "profile": profile,
        "start_date": start_date,
        "end_date": end_date,
        "note": note,
        "signal_count": summary.get("signal_count"),
        "trade_count": summary.get("trade_count"),
        "total_return": summary.get("total_return"),
        "benchmark_return": summary.get("benchmark_return"),
        "excess_return": summary.get("excess_return"),
        "max_drawdown": summary.get("max_drawdown"),
        "win_rate": summary.get("win_rate"),
        "avg_trade_return": summary.get("avg_trade_return"),
        "final_equity": summary.get("final_equity"),
    }


def _load_signal_frame(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    rule = GEN2_OPEN_RULE_V1
    df = df[
        (df["pattern"] == rule["pattern"])
        & (df["g2_open_state"] == rule["g2_open_state"])
        & (df["trigger_type"] == rule["trigger_type"])
    ].copy()
    if "confirm_datetime" in df.columns:
        df["confirm_datetime"] = pd.to_datetime(df["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    if "entry_date" in df.columns:
        df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["code"] = df["code"].astype(str)
    return df


def _write_signal_filter(df: pd.DataFrame, output_path: Path, remove_keys: Iterable[tuple[str, str]]) -> tuple[Path, int]:
    remove_set = set(remove_keys)
    keys = list(zip(df["code"].astype(str), df["confirm_datetime"].astype(str)))
    mask = pd.Series([item not in remove_set for item in keys], index=df.index)
    filtered = df[mask].copy()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    filtered.to_parquet(output_path, index=False)
    return output_path, int(len(df) - len(filtered))


def _top_winner_stress(output_dir: Path, signal_source: Path, baseline_run: Path, start_date: str, end_date: str) -> list[dict[str, Any]]:
    trades = pd.read_csv(baseline_run / "trades.csv")
    if trades.empty:
        return []
    trades["pnl"] = pd.to_numeric(trades["pnl"], errors="coerce")
    trades["buy_datetime"] = pd.to_datetime(trades["buy_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    trades["code"] = trades["code"].astype(str)
    winners = trades.sort_values("pnl", ascending=False).dropna(subset=["pnl"])
    signals = _load_signal_frame(signal_source)

    rows: list[dict[str, Any]] = []
    for top_n in [1, 3, 5, 10]:
        filtered_source, removed = _write_signal_filter(
            signals,
            output_dir / "signals" / f"remove_top{top_n}_winner_trades.parquet",
            list(zip(winners.head(top_n)["code"], winners.head(top_n)["buy_datetime"])),
        )
        summary = _run_fixed(
            signal_source=filtered_source,
            output_dir=output_dir / "runs" / f"remove_top{top_n}_winner_trades",
            start_date=start_date,
            end_date=end_date,
        )
        rows.append(
            _summary_row(
                "top_winner_removal",
                f"remove_top{top_n}_winner_trades",
                start_date,
                end_date,
                summary,
                note=f"removed_signals={removed}",
            )
        )

    code_pnl = trades.groupby("code", as_index=False)["pnl"].sum().sort_values("pnl", ascending=False)
    for top_n in [1, 3, 5]:
        remove_codes = set(code_pnl.head(top_n)["code"].astype(str))
        filtered = signals[~signals["code"].isin(remove_codes)].copy()
        filtered_source = output_dir / "signals" / f"remove_top{top_n}_winner_codes.parquet"
        filtered.to_parquet(filtered_source, index=False)
        summary = _run_fixed(
            signal_source=filtered_source,
            output_dir=output_dir / "runs" / f"remove_top{top_n}_winner_codes",
            start_date=start_date,
            end_date=end_date,
        )
        rows.append(
            _summary_row(
                "top_code_removal",
                f"remove_top{top_n}_winner_codes",
                start_date,
                end_date,
                summary,
                note=f"codes={','.join(sorted(remove_codes))}",
            )
        )
    return rows


def _rolling_windows(output_dir: Path, signal_source: Path) -> list[dict[str, Any]]:
    windows = [
        ("w1_2024h2", "2024-07-09", "2024-12-31"),
        ("w2_2024q4_2025q1", "2024-10-01", "2025-03-31"),
        ("w3_2025h1", "2025-01-01", "2025-06-30"),
        ("w4_2025q2_q3", "2025-04-01", "2025-09-30"),
        ("w5_2025h2", "2025-07-01", "2025-12-31"),
        ("w6_2025q4_2026q1", "2025-10-01", "2026-03-31"),
        ("w7_2026ytd", "2026-01-01", "2026-05-21"),
    ]
    rows: list[dict[str, Any]] = []
    for name, start, end in windows:
        summary = _run_fixed(
            signal_source=signal_source,
            output_dir=output_dir / "runs" / name,
            start_date=start,
            end_date=end,
        )
        rows.append(_summary_row("rolling_window", name, start, end, summary))
    return rows


def _time_splits(output_dir: Path, signal_source: Path) -> list[dict[str, Any]]:
    splits = [
        ("train_2024h2_2025q1", "2024-07-09", "2025-03-31"),
        ("validation_2025q2_q4", "2025-04-01", "2025-12-31"),
        ("blind_2026ytd", "2026-01-01", "2026-05-21"),
    ]
    rows: list[dict[str, Any]] = []
    for name, start, end in splits:
        summary = _run_fixed(
            signal_source=signal_source,
            output_dir=output_dir / "runs" / name,
            start_date=start,
            end_date=end,
        )
        rows.append(_summary_row("time_split", name, start, end, summary))
    return rows


def _cost_stress(output_dir: Path, signal_source: Path, start_date: str, end_date: str) -> list[dict[str, Any]]:
    costs = [
        ("cost_base", 5.0, 5.0, 2.5, 5.0),
        ("cost_high", 10.0, 10.0, 5.0, 5.0),
        ("cost_very_high", 20.0, 20.0, 5.0, 5.0),
    ]
    rows: list[dict[str, Any]] = []
    for name, buy_slip, sell_slip, comm, tax in costs:
        summary = _run_fixed(
            signal_source=signal_source,
            output_dir=output_dir / "runs" / name,
            start_date=start_date,
            end_date=end_date,
            buy_slippage_bps=buy_slip,
            sell_slippage_bps=sell_slip,
            commission_bps=comm,
            stamp_tax_bps=tax,
        )
        rows.append(
            _summary_row(
                "cost_stress",
                name,
                start_date,
                end_date,
                summary,
                note=f"buy_slip={buy_slip};sell_slip={sell_slip};comm={comm};tax={tax}",
            )
        )
    return rows


def _write_execution_diagnostics(output_dir: Path, baseline_run: Path) -> dict[str, Any]:
    trades = pd.read_csv(baseline_run / "trades.csv")
    curve = pd.read_csv(baseline_run / "equity_curve.csv")
    signals = pd.read_csv(baseline_run / "signals.csv")
    diagnostics: dict[str, Any] = {}

    if not trades.empty:
        trades["pnl"] = pd.to_numeric(trades["pnl"], errors="coerce")
        trades["return"] = pd.to_numeric(trades["return"], errors="coerce")
        trades["buy_datetime"] = pd.to_datetime(trades["buy_datetime"], errors="coerce")
        trades["sell_datetime"] = pd.to_datetime(trades["sell_datetime"], errors="coerce")
        trades["holding_minutes"] = (trades["sell_datetime"] - trades["buy_datetime"]).dt.total_seconds() / 60.0
        diagnostics["exit_reason_counts"] = trades["exit_reason"].fillna("").astype(str).value_counts().to_dict()
        diagnostics["trade_return_quantiles"] = {
            "p10": float(trades["return"].quantile(0.10)),
            "p25": float(trades["return"].quantile(0.25)),
            "p50": float(trades["return"].quantile(0.50)),
            "p75": float(trades["return"].quantile(0.75)),
            "p90": float(trades["return"].quantile(0.90)),
        }
        diagnostics["holding_minutes_quantiles"] = {
            "p25": float(trades["holding_minutes"].quantile(0.25)),
            "p50": float(trades["holding_minutes"].quantile(0.50)),
            "p75": float(trades["holding_minutes"].quantile(0.75)),
        }
        gross_profit = float(trades.loc[trades["pnl"] > 0, "pnl"].sum())
        diagnostics["top5_profit_share"] = (
            float(trades.sort_values("pnl", ascending=False).head(5)["pnl"].sum()) / gross_profit if gross_profit > 0 else None
        )

    if not curve.empty:
        curve["date"] = pd.to_datetime(curve["date"], errors="coerce")
        curve["strategy_equity"] = pd.to_numeric(curve["strategy_equity"], errors="coerce")
        monthly = curve.dropna(subset=["date", "strategy_equity"]).copy()
        monthly["month"] = monthly["date"].dt.strftime("%Y-%m")
        month_ret = monthly.groupby("month")["strategy_equity"].agg(["first", "last"]).reset_index()
        month_ret["return"] = month_ret["last"] / month_ret["first"] - 1.0
        month_ret.to_csv(output_dir / "monthly_returns.csv", index=False, encoding="utf-8-sig")
        diagnostics["positive_month_rate"] = float((month_ret["return"] > 0).mean()) if not month_ret.empty else None
        diagnostics["worst_months"] = month_ret.sort_values("return").head(5).to_dict("records")
        diagnostics["avg_holding_count"] = float(pd.to_numeric(curve["holding_count"], errors="coerce").mean())
        diagnostics["max_holding_count"] = int(pd.to_numeric(curve["holding_count"], errors="coerce").max())

    if not signals.empty and "confirm_datetime" in signals.columns:
        signals["confirm_datetime"] = pd.to_datetime(signals["confirm_datetime"], errors="coerce")
        signals["entry_date"] = pd.to_datetime(signals["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        diagnostics["signal_days"] = int(signals["entry_date"].nunique())
        diagnostics["avg_signals_per_signal_day"] = float(signals.groupby("entry_date").size().mean())
        diagnostics["max_signals_per_signal_day"] = int(signals.groupby("entry_date").size().max())
        diagnostics["confirm_time_counts"] = signals["confirm_datetime"].dt.strftime("%H:%M").value_counts().sort_index().to_dict()

    (output_dir / "execution_diagnostics.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return diagnostics


def _write_report(output_dir: Path, df: pd.DataFrame, diagnostics: dict[str, Any]) -> None:
    lines = [
        "# G2 Attack V1 PrevLow Validation",
        "",
        "固定策略：NORMAL + V4 rank<=100 pullback_restart + 30m 底分型突破放量确认；最多 2 仓，每日最多买 1 只，单票最多 50%；30m -5% 硬止损，+10% 卖一半，剩余仓位最多持有 10 个交易日，盈利后跌破上一交易日低点离场。",
        "",
        "## Summary",
        "",
        "| group | profile | window | signals | trades | total | excess | max_dd | win | avg_trade | note |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in df.to_dict("records"):
        lines.append(
            f"| {row['group']} | {row['profile']} | {row['start_date']}~{row['end_date']} | {int(row.get('signal_count') or 0)} | {int(row.get('trade_count') or 0)} | {_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} | {row.get('note') or ''} |"
        )

    lines.extend(["", "## Execution Diagnostics", ""])
    exit_counts = diagnostics.get("exit_reason_counts") or {}
    if exit_counts:
        lines.append(f"- Exit reason counts: `{json.dumps(exit_counts, ensure_ascii=False, sort_keys=True)}`")
    if diagnostics.get("trade_return_quantiles"):
        q = diagnostics["trade_return_quantiles"]
        lines.append(f"- Trade return quantiles: p10 {_pct(q.get('p10'))}, p50 {_pct(q.get('p50'))}, p90 {_pct(q.get('p90'))}.")
    if diagnostics.get("top5_profit_share") is not None:
        lines.append(f"- Top5 profit share: {_pct(diagnostics.get('top5_profit_share'))}.")
    if diagnostics.get("positive_month_rate") is not None:
        lines.append(f"- Positive month rate: {_pct(diagnostics.get('positive_month_rate'))}.")
    lines.append(
        f"- Signal days: {diagnostics.get('signal_days', 0)}, avg signals/signal-day: {float(diagnostics.get('avg_signals_per_signal_day') or 0):.2f}, max signals/signal-day: {int(diagnostics.get('max_signals_per_signal_day') or 0)}."
    )
    lines.append(
        f"- Holding count: avg {float(diagnostics.get('avg_holding_count') or 0):.2f}, max {int(diagnostics.get('max_holding_count') or 0)}."
    )
    lines.extend(["", "## First Read", ""])
    baseline = df[(df["group"] == "baseline")].head(1)
    if not baseline.empty:
        row = baseline.iloc[0]
        lines.append(f"- Baseline full-window return is {_pct(row.get('total_return'))}, excess {_pct(row.get('excess_return'))}, max drawdown {_pct(row.get('max_drawdown'))}.")
    rolling = df[df["group"] == "rolling_window"].copy()
    if not rolling.empty:
        worst = rolling.sort_values("total_return").iloc[0]
        lines.append(f"- Weakest rolling window is `{worst['profile']}` with total {_pct(worst.get('total_return'))}, excess {_pct(worst.get('excess_return'))}.")
    top = df[df["group"].isin(["top_winner_removal", "top_code_removal"])].copy()
    if not top.empty:
        worst = top.sort_values("total_return").iloc[0]
        lines.append(f"- Top-winner stress worst case is `{worst['profile']}` with total {_pct(worst.get('total_return'))}, excess {_pct(worst.get('excess_return'))}.")

    (output_dir / "validation_report.md").write_text("\n".join(lines), encoding="utf-8")


def run_validation(output_dir: Path, signal_source: Path, start_date: str, end_date: str) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline_run = output_dir / "runs" / "baseline_full"
    baseline = _run_fixed(signal_source, baseline_run, start_date, end_date)
    rows = [_summary_row("baseline", "baseline_full", start_date, end_date, baseline)]
    rows.extend(_cost_stress(output_dir, signal_source, start_date, end_date))
    rows.extend(_time_splits(output_dir, signal_source))
    rows.extend(_rolling_windows(output_dir, signal_source))
    rows.extend(_top_winner_stress(output_dir, signal_source, baseline_run, start_date, end_date))

    df = pd.DataFrame(rows)
    df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    diagnostics = _write_execution_diagnostics(output_dir, baseline_run)
    payload = {
        "schema_version": 1,
        "strategy_code": "g2_attack_v1_prev_low",
        "start_date": start_date,
        "end_date": end_date,
        "rows": df.where(pd.notna(df), None).to_dict("records"),
        "diagnostics": diagnostics,
        "outputs": {
            "summary": "summary.csv",
            "report": "validation_report.md",
            "execution_diagnostics": "execution_diagnostics.json",
            "monthly_returns": "monthly_returns.csv",
            "runs": "runs/",
            "signals": "signals/",
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(output_dir, df, diagnostics)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate fixed G2 Attack V1 PrevLow strategy.")
    parser.add_argument("--signal-source", default=str(DEFAULT_SIGNAL_SOURCE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    args = parser.parse_args()
    payload = run_validation(
        output_dir=Path(args.output_dir),
        signal_source=Path(args.signal_source),
        start_date=str(args.start_date),
        end_date=str(args.end_date),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
