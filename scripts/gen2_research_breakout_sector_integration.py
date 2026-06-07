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

from scripts.gen2_backtest_risk_cool_dynamic_circuit import _run_dynamic  # noqa: E402
from scripts.gen2_runtime_dates import add_end_date_argument, resolve_end_date, resolve_window_ends  # noqa: E402


BASE = (
    ROOT
    / "reports"
    / "gen2_breakout_buy_point_research"
    / "breakout_family_intraday_strength_probe"
    / "combo_policy_probe"
    / "sector_context_probe"
    / "sector_filter_matrix"
)
ENRICHED = BASE / "sector_enriched_combo_or.parquet"
OUT = ROOT / "reports" / "g2_sector_integration_probe"
def _json_default(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return str(obj)


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _num_col(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(-1.0, index=df.index, dtype=float)
    return pd.to_numeric(df[column], errors="coerce").fillna(-1.0)


def _prepare_base() -> pd.DataFrame:
    df = pd.read_parquet(ENRICHED).copy()
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["confirm_datetime"] = pd.to_datetime(df["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    df["confirm_time"] = pd.to_datetime(df["confirm_datetime"], errors="coerce").dt.strftime("%H:%M")
    df["l3_s3"] = _num_col(df, "l3_rt_strong3_ratio")
    df["l2_s3"] = _num_col(df, "l2_rt_strong3_ratio")
    df["l3_rise"] = _num_col(df, "l3_rt_rise_ratio")
    df["sector_strong_l3_05"] = df["l3_s3"] >= 0.05
    df["sector_strong_l3_03"] = df["l3_s3"] >= 0.03
    df["sector_strong_l3_06"] = df["l3_s3"] >= 0.06
    df["sector_strong_l2_or_l3"] = (df["l3_s3"] >= 0.05) | (df["l2_s3"] >= 0.08)
    return df


def _drop_aux(df: pd.DataFrame) -> pd.DataFrame:
    drop_cols = ["confirm_time", "code6"]
    return df.drop(columns=[c for c in drop_cols if c in df.columns]).copy()


def _write_source(name: str, df: pd.DataFrame) -> Path:
    source_dir = OUT / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"{name}.parquet"
    out = _drop_aux(df)
    out.to_parquet(path, index=False)
    out.to_csv(path.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    return path


def _counts(df: pd.DataFrame) -> dict[str, Any]:
    return {
        "signals": int(len(df)),
        "volume5_signals": int((df["source_family"] == "volume5").sum()),
        "bigbull_signals": int((df["source_family"] == "big_bull").sum()),
        "sector_strong_signals": int(df["sector_strong_l3_05"].sum()),
    }


def _variants(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    strong = df["sector_strong_l3_05"]
    strong03 = df["sector_strong_l3_03"]
    strong06 = df["sector_strong_l3_06"]
    bigbull = df["source_family"] == "big_bull"
    volume5 = df["source_family"] == "volume5"

    variants: dict[str, pd.DataFrame] = {
        "base": df,
        "gate_all_05": df[strong],
        "gate_bb_only": df[(~bigbull) | strong],
        "gate_v5_only": df[(~volume5) | strong],
        "gate_all_03": df[strong03],
        "gate_all_06": df[strong06],
        "gate_l2or3": df[df["sector_strong_l2_or_l3"]],
    }

    # Same-day integration: if any sector-strong signal appears on that date,
    # keep only sector-strong signals for that date. Otherwise keep the weak day.
    day_has_strong = df.groupby("entry_date")["sector_strong_l3_05"].transform("any")
    variants["prefer_day"] = df[(~day_has_strong) | strong]

    early_1000 = df["confirm_time"].eq("10:00")
    day_has_later_strong = (
        df.assign(later_strong=df["sector_strong_l3_05"] & ~early_1000)
        .groupby("entry_date")["later_strong"]
        .transform("any")
    )
    weak_1000_v5 = early_1000 & volume5 & ~strong
    weak_1000_any = early_1000 & ~strong
    variants["veto_10v5_weak"] = df[~weak_1000_v5]
    variants["veto_10any_weak"] = df[~weak_1000_any]
    variants["veto_10v5_if_later"] = df[~(weak_1000_v5 & day_has_later_strong)]
    variants["veto_10any_if_later"] = df[~(weak_1000_any & day_has_later_strong)]

    # Soft sizing: keep all signals, but reduce weak sector candidates.
    for weight in [0.50, 0.70]:
        soft = df.copy()
        soft["alpha191_position_weight"] = np.where(strong, 1.0, weight)
        variants[f"soft_w{int(weight * 100)}"] = soft

    # Source-aware soft sizing.
    soft_big = df.copy()
    soft_big["alpha191_position_weight"] = np.where((bigbull & ~strong), 0.50, 1.0)
    variants["soft_bb_half"] = soft_big

    soft_vol = df.copy()
    soft_vol["alpha191_position_weight"] = np.where((volume5 & ~strong), 0.50, 1.0)
    variants["soft_v5_half"] = soft_vol

    return variants


def _trade_lot_summary(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "trades.csv"
    if not path.exists():
        return {}
    trades = pd.read_csv(path)
    if trades.empty:
        return {"lot_count": 0}
    lot = trades.groupby(["buy_date", "code", "name"], dropna=False).agg(
        pnl=("pnl", "sum"),
        ret=("return", "sum"),
        exits=("exit_reason", "count"),
    )
    return {
        "lot_count": int(len(lot)),
        "lot_win_rate": float((lot["pnl"] > 0).mean()),
        "lot_avg_pnl": float(lot["pnl"].mean()),
    }


def _run_or_read(source_path: Path, run_dir: Path, start: str, end: str, sort_mode: str) -> dict[str, Any]:
    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        cached = json.loads(summary_path.read_text(encoding="utf-8"))
        if str(cached.get("start_date") or "") == start and str(cached.get("end_date") or "") == end:
            return cached
    return _run_dynamic(source_path, run_dir, "stop_cd3_skip", start, end, sort_mode=sort_mode)


def _empty_summary() -> dict[str, Any]:
    return {
        "trade_count": 0,
        "total_return": 0.0,
        "excess_return": 0.0,
        "max_drawdown": 0.0,
        "win_rate": 0.0,
        "avg_trade_return": 0.0,
    }


def run(end_date: str) -> dict[str, Any]:
    OUT.mkdir(parents=True, exist_ok=True)
    base = _prepare_base()
    rows: list[dict[str, Any]] = []
    windows = resolve_window_ends(end_date)
    for variant, source_df in _variants(base).items():
        source_path = _write_source(variant, source_df)
        counts = _counts(source_df)
        for sort_mode in ["trigger_time"]:
            for window, (start, end) in windows.items():
                run_dir = OUT / "runs" / variant / sort_mode / window
                summary = _empty_summary() if source_df.empty else _run_or_read(source_path, run_dir, start, end, sort_mode)
                rows.append(
                    {
                        "variant": variant,
                        "sort_mode": sort_mode,
                        "window": window,
                        **counts,
                        "trades": summary["trade_count"],
                        "total_return": summary["total_return"],
                        "excess_return": summary["excess_return"],
                        "max_drawdown": summary["max_drawdown"],
                        "win_rate": summary["win_rate"],
                        "avg_trade_return": summary["avg_trade_return"],
                        **_trade_lot_summary(run_dir),
                    }
                )

    result = pd.DataFrame(rows)
    result.to_csv(OUT / "sector_integration_summary.csv", index=False, encoding="utf-8-sig")
    (OUT / "sector_integration_summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )

    lines = [
        "# Sector Integration Probe",
        "",
        "| variant | window | signals | strong | trades | lots | total | excess | max_dd | win | lot_win | avg_trade |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['variant']} | {row['window']} | {row['signals']} | {row['sector_strong_signals']} | "
            f"{row['trades']} | {row.get('lot_count', '')} | {_pct(row['total_return'])} | "
            f"{_pct(row['excess_return'])} | {_pct(row['max_drawdown'])} | {_pct(row['win_rate'])} | "
            f"{_pct(row.get('lot_win_rate'))} | {_pct(row['avg_trade_return'])} |"
        )
    (OUT / "sector_integration_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    payload = {"out": str(OUT), "end_date": end_date, "rows": len(rows), "windows": windows}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh breakout sector integration to the latest trade date.")
    add_end_date_argument(parser)
    args = parser.parse_args()
    run(resolve_end_date(args.end_date))


if __name__ == "__main__":
    main()
