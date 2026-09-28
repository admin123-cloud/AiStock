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
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = _PROJECT_ROOT
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default, _pct  # noqa: E402
from scripts.gen2_compare_prev_low_exit_fills import FillProfile, _run_profile  # noqa: E402


DEFAULT_TIER_SIGNALS = _report_path() / "gen2_preference_signal_tier_validation" / "signals_with_preference_tiers.csv"
DEFAULT_OUTPUT_DIR = _report_path() / "gen2_preference_regime_matrix_backtest"


def _load_tier_signals(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    return d.dropna(subset=["entry_date", "code", "entry_price", "confirm_datetime"]).copy()


def _load_index_regime(start_date: str, end_date: str, index_code: str) -> pd.DataFrame:
    from utils.market_warehouse import clickhouse_client

    start = (pd.Timestamp(start_date) - pd.Timedelta(days=120)).strftime("%Y-%m-%d")
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT trade_date, close
        FROM kline_daily
        WHERE code = '{index_code}'
          AND trade_date BETWEEN '{start}' AND '{end_date}'
        ORDER BY trade_date
        """
    )
    if df.empty:
        raise RuntimeError(f"No index data for {index_code}")
    d = df.copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d = d.dropna(subset=["trade_date", "close"]).reset_index(drop=True)
    d["ma20"] = d["close"].rolling(20, min_periods=20).mean()
    d["ma20_slope5"] = d["ma20"] / d["ma20"].shift(5) - 1.0
    d["market_regime"] = np.select(
        [
            (d["close"] > d["ma20"]) & (d["ma20_slope5"] > 0),
            (d["close"] < d["ma20"]) & (d["ma20_slope5"] < 0),
        ],
        ["strong", "weak"],
        default="neutral",
    )
    d["effective_date"] = d["trade_date"].shift(-1)
    return d.dropna(subset=["effective_date"])[["effective_date", "trade_date", "close", "ma20", "ma20_slope5", "market_regime"]]


def _attach_regime(signals: pd.DataFrame, start_date: str, end_date: str, index_code: str) -> pd.DataFrame:
    regime = _load_index_regime(start_date, end_date, index_code).rename(
        columns={
            "effective_date": "entry_date",
            "trade_date": "regime_source_date",
            "close": "regime_index_close",
            "ma20": "regime_index_ma20",
            "ma20_slope5": "regime_index_ma20_slope5",
        }
    )
    d = signals.merge(regime, on="entry_date", how="left")
    d["market_regime"] = d["market_regime"].fillna("neutral")
    return d


def _decision_label(row: pd.Series) -> str:
    tier = str(row.get("preference_tier") or "")
    c_subtier = str(row.get("c_subtier") or "")
    if tier == "C":
        return c_subtier
    return tier


def _matrix_weight(matrix: str, regime: str, label: str) -> float:
    if matrix == "baseline_all":
        return 1.0
    if matrix == "drop_c_risk":
        return 0.0 if label == "C-risk" else 1.0
    if matrix == "crisk_weak_zero":
        return 0.0 if regime == "weak" and label == "C-risk" else 1.0
    if matrix == "crisk_weak_neutral_zero":
        return 0.0 if regime in {"weak", "neutral"} and label == "C-risk" else 1.0
    if matrix == "crisk_regime_v1":
        if label != "C-risk":
            return 1.0
        return {"strong": 1.0, "neutral": 0.5, "weak": 0.0}.get(regime, 0.5)
    if matrix == "user_v1":
        table = {
            "strong": {"A": 1.0, "B": 1.0, "C-aggressive": 1.0},
            "neutral": {"A": 1.0, "C-aggressive": 0.5},
            "weak": {"A": 0.5},
        }
        return table.get(regime, {}).get(label, 0.0)
    if matrix == "balanced_v1":
        table = {
            "strong": {"A": 1.0, "B": 0.5, "C-aggressive": 1.0, "C-review": 0.5},
            "neutral": {"A": 1.0, "B": 0.5, "C-aggressive": 0.5},
            "weak": {"A": 0.5},
        }
        return table.get(regime, {}).get(label, 0.0)
    if matrix == "attack_v1":
        table = {
            "strong": {"A": 1.0, "B": 1.0, "C-aggressive": 1.0, "C-review": 0.5},
            "neutral": {"A": 1.0, "B": 0.5, "C-aggressive": 1.0},
            "weak": {"A": 0.5, "C-aggressive": 0.5},
        }
        return table.get(regime, {}).get(label, 0.0)
    if matrix == "defensive_v1":
        table = {
            "strong": {"A": 1.0, "B": 0.5, "C-aggressive": 0.5},
            "neutral": {"A": 0.5},
            "weak": {"A": 0.25},
        }
        return table.get(regime, {}).get(label, 0.0)
    raise ValueError(f"Unknown matrix: {matrix}")


def _apply_matrix(signals: pd.DataFrame, matrix: str) -> pd.DataFrame:
    d = signals.copy()
    d["decision_label"] = d.apply(_decision_label, axis=1)
    d["matrix_name"] = matrix
    d["capital_weight"] = [
        _matrix_weight(matrix, str(regime), str(label))
        for regime, label in zip(d["market_regime"], d["decision_label"])
    ]
    return d[d["capital_weight"] > 0].copy()


def _segment_metrics(curve_path: Path) -> list[dict[str, Any]]:
    d = pd.read_csv(curve_path)
    if d.empty:
        return []
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    rows: list[dict[str, Any]] = []
    for name, start, end in [
        ("train_2024h2_2025q1", "2024-07-09", "2025-03-31"),
        ("validation_2025q2_q4", "2025-04-01", "2025-12-31"),
        ("blind_2026ytd", "2026-01-01", "2026-05-21"),
    ]:
        g = d[(d["date"] >= pd.Timestamp(start)) & (d["date"] <= pd.Timestamp(end))].copy()
        if len(g) < 2:
            continue
        equity = pd.to_numeric(g["strategy_equity"], errors="coerce").dropna()
        if len(equity) < 2:
            continue
        rows.append(
            {
                "segment": name,
                "return": float(equity.iloc[-1] / equity.iloc[0] - 1.0),
                "max_drawdown": float((equity / equity.cummax() - 1.0).min()),
            }
        )
    return rows


def _write_report(output_dir: Path, rows: list[dict[str, Any]], segment_rows: list[dict[str, Any]]) -> None:
    lines = [
        "# G2 Preference Regime Matrix Backtest",
        "",
        "Market regime uses previous trading day's CSI1000 close vs MA20 and MA20 slope5.",
        "Entry, exits and max-position rules are unchanged; only signal eligibility and capital_weight change.",
        "",
        "| matrix | filtered | used | trades | total | excess | max_dd | win | avg_trade |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['matrix']} | {int(row.get('filtered_signal_rows') or 0)} | {int(row.get('signal_count') or 0)} | "
            f"{int(row.get('trade_count') or 0)} | {_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | "
            f"{_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} |"
        )
    lines.extend(["", "## Segment Returns", ""])
    lines.append("| matrix | segment | return | max_dd |")
    lines.append("| --- | --- | ---: | ---: |")
    for row in segment_rows:
        lines.append(f"| {row['matrix']} | {row['segment']} | {_pct(row['return'])} | {_pct(row['max_drawdown'])} |")
    (output_dir / "conclusion_zh.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    signals = _attach_regime(_load_tier_signals(Path(args.tier_signals)), str(args.start_date), str(args.end_date), str(args.index_code))
    profile = FillProfile("gap_confirm_30m_close__intraday_30m_close", gap_open_mode="confirm_30m", intraday_mode="bar_close")

    matrices = [x.strip() for x in str(args.matrices).split(",") if x.strip()]
    rows: list[dict[str, Any]] = []
    segment_rows: list[dict[str, Any]] = []
    for matrix in matrices:
        rule_dir = output_dir / matrix
        rule_dir.mkdir(parents=True, exist_ok=True)
        filtered = _apply_matrix(signals, matrix)
        filtered.to_csv(rule_dir / "signals.csv", index=False, encoding="utf-8-sig")
        signal_path = rule_dir / "signals.parquet"
        filtered.to_parquet(signal_path, index=False)
        summary = _run_profile(
            signal_source=signal_path,
            output_dir=rule_dir / "backtest",
            profile=profile,
            start_date=str(args.start_date),
            end_date=str(args.end_date),
            sort_mode=str(args.sort_mode),
        )
        summary["matrix"] = matrix
        summary["filtered_signal_rows"] = int(len(filtered))
        rows.append(summary)
        for seg in _segment_metrics(rule_dir / "backtest" / "equity_curve.csv"):
            seg["matrix"] = matrix
            segment_rows.append(seg)

    summary_df = pd.DataFrame(rows)
    segments_df = pd.DataFrame(segment_rows)
    summary_df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    segments_df.to_csv(output_dir / "segment_summary.csv", index=False, encoding="utf-8-sig")
    _write_report(output_dir, rows, segment_rows)
    payload = {
        "schema_version": 1,
        "tier_signals": str(Path(args.tier_signals)),
        "index_code": str(args.index_code),
        "start_date": str(args.start_date),
        "end_date": str(args.end_date),
        "sort_mode": str(args.sort_mode),
        "rows": summary_df.where(pd.notna(summary_df), None).to_dict("records"),
        "segment_rows": segments_df.where(pd.notna(segments_df), None).to_dict("records"),
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest G2 preference decision matrices by market regime.")
    parser.add_argument("--tier-signals", default=str(DEFAULT_TIER_SIGNALS))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--index-code", default="000852.SH")
    parser.add_argument("--sort-mode", default="trigger_time")
    parser.add_argument(
        "--matrices",
        default="baseline_all,drop_c_risk,crisk_weak_zero,crisk_weak_neutral_zero,crisk_regime_v1,user_v1,balanced_v1,attack_v1,defensive_v1",
    )
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
