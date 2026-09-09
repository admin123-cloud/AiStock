from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[1]))

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
from scripts.gen2_runtime_dates import add_end_date_argument, resolve_end_date  # noqa: E402
from utils.paths import report_path  # noqa: E402

BASE = report_path("gen2_breakout_buy_point_research")
PROBE = BASE / "breakout_family_intraday_strength_probe"
OUT = PROBE / "combo_policy_probe"
START_DATE = "2024-07-09"


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    return str(obj)


def _read_source(path: Path, label: str) -> pd.DataFrame:
    df = pd.read_parquet(path).copy()
    df["source_family"] = label
    return df


def _resolve_existing_path(candidates: list[Path], label: str) -> Path:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"{label} source missing. tried: " + " | ".join(str(item) for item in candidates))


def _load_volume5_source() -> pd.DataFrame:
    path = _resolve_existing_path(
        [
            BASE / "sources" / "volume5_dynamic_stop_cd3_mapped.parquet",
            report_path("gen2_alpha191_light_constraint_matrix", "sources", "volume5_keep80_runup_le100.parquet"),
            report_path("gen2_v2_complete_strategy", "sources", "g2_v2_complete.parquet"),
        ],
        "volume5",
    )
    return _read_source(path, "volume5")


def _load_bigbull_source() -> pd.DataFrame:
    direct_path = BASE / "breakout_family_dynamic_probe" / "sources" / "pool_rank200_box__big_bull_rebreak_2_5d_vol12_prior60_mapped.parquet"
    if direct_path.exists():
        return _read_source(direct_path, "big_bull")
    signals_path = _resolve_existing_path([BASE / "signals.parquet"], "big_bull fallback signals")
    signals = pd.read_parquet(signals_path).copy()
    pattern_col = signals.get("pattern")
    if pattern_col is not None:
        mask = pattern_col.astype(str).eq("pool_rank200_box__big_bull_rebreak_2_5d_vol12")
    else:
        mask = signals.get("setup_type", pd.Series("", index=signals.index)).astype(str).eq("big_bull_rebreak_2_5d")
    signals = signals.loc[mask].copy()
    signals["source_family"] = "big_bull"
    return signals


def _dedupe_source(df: pd.DataFrame) -> pd.DataFrame:
    sort_cols = [c for c in ["entry_date", "confirm_datetime", "source_priority", "v4_rank"] if c in df.columns]
    if "source_priority" not in df.columns:
        df["source_priority"] = df["source_family"].map({"volume5": 0, "big_bull": 1}).fillna(9)
    if sort_cols:
        df = df.sort_values(sort_cols, ascending=True).copy()
    keys = [c for c in ["entry_date", "code"] if c in df.columns]
    if len(keys) == 2:
        df = df.drop_duplicates(keys, keep="first")
    return df.reset_index(drop=True)


def _normalize_for_concat(df: pd.DataFrame) -> pd.DataFrame:
    df = df.loc[:, ~df.columns.duplicated()].copy()
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            if col in {"trade_date", "entry_date"}:
                df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")
            else:
                df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    return df


def _write_source(name: str, df: pd.DataFrame) -> Path:
    source_dir = OUT / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"{name}.parquet"
    for col in ["trade_date", "entry_date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")
    if "confirm_datetime" in df.columns:
        df["confirm_datetime"] = pd.to_datetime(df["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    df.to_parquet(path, index=False)
    df.to_csv(path.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    return path


from research.common.reporting import percent_text as _pct


def _summarize_sources(df: pd.DataFrame) -> dict[str, Any]:
    return {
        "signals": int(len(df)),
        "big_bull_signals": int((df["source_family"] == "big_bull").sum()) if "source_family" in df.columns else None,
        "volume5_signals": int((df["source_family"] == "volume5").sum()) if "source_family" in df.columns else None,
    }


def _attribution(run_dir: Path) -> list[dict[str, Any]]:
    trades = pd.read_csv(run_dir / "trades.csv")
    signals = pd.read_csv(run_dir / "signals.csv")
    if trades.empty or signals.empty or "source_family" not in signals.columns:
        return []
    trades["buy_date"] = pd.to_datetime(trades["buy_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    signals["entry_date"] = pd.to_datetime(signals["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    source_map = (
        signals.sort_values(["entry_date", "confirm_datetime"])
        .drop_duplicates(["entry_date", "code"], keep="first")
        .set_index(["entry_date", "code"])["source_family"]
        .to_dict()
    )
    trades["source_family"] = [source_map.get((r.buy_date, str(r.code))) for r in trades.itertuples(index=False)]
    rows: list[dict[str, Any]] = []
    for source, g in trades.groupby("source_family", dropna=False):
        rows.append(
            {
                "source": str(source),
                "trades": int(len(g)),
                "pnl": float(g["pnl"].sum()) if "pnl" in g.columns else None,
                "avg_return": float(g["return"].mean()) if "return" in g.columns else None,
                "win_rate": float((g["return"] > 0).mean()) if "return" in g.columns else None,
                "stop_ratio": float((g["exit_reason"].astype(str) == "stop_loss_30m").mean())
                if "exit_reason" in g.columns
                else None,
            }
        )
    return rows


def _monthly(run_dir: Path) -> dict[str, Any]:
    trades = pd.read_csv(run_dir / "trades.csv")
    if trades.empty:
        return {"trades": 0, "active_months": 0, "avg_trades_per_active_month": 0.0, "positive_months": 0, "negative_months": 0, "pnl": 0.0}
    trades["month"] = pd.to_datetime(trades["buy_date"], errors="coerce").dt.strftime("%Y-%m")
    pnl = trades.groupby("month")["pnl"].sum()
    counts = trades.groupby("month").size()
    return {
        "trades": int(len(trades)),
        "active_months": int(len(counts)),
        "avg_trades_per_active_month": float(counts.mean()),
        "positive_months": int((pnl > 0).sum()),
        "negative_months": int((pnl < 0).sum()),
        "pnl": float(trades["pnl"].sum()),
    }


def run(end_date: str) -> dict[str, Any]:
    OUT.mkdir(parents=True, exist_ok=True)
    volume5 = _load_volume5_source()
    bigbull = _load_bigbull_source()
    candidates = pd.read_csv(BASE / "candidates.csv")
    candidates["trade_date"] = pd.to_datetime(candidates["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    candidates["entry_date"] = pd.to_datetime(candidates["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    enrich_cols = [
        "trade_date",
        "entry_date",
        "code",
        "close",
        "setup_big_bull_rebreak_2_5d_top",
    ]
    enrich = candidates[[c for c in enrich_cols if c in candidates.columns]].drop_duplicates(
        ["trade_date", "entry_date", "code"], keep="first"
    )
    bigbull = bigbull.merge(enrich, on=["trade_date", "entry_date", "code"], how="left")
    bigbull["rt_return_from_d1_close"] = pd.to_numeric(bigbull["entry_price"], errors="coerce") / pd.to_numeric(
        bigbull["close"], errors="coerce"
    ) - 1.0
    bigbull["rt_breakout_vs_box_top"] = pd.to_numeric(bigbull["entry_price"], errors="coerce") / pd.to_numeric(
        bigbull["setup_big_bull_rebreak_2_5d_top"], errors="coerce"
    ) - 1.0

    configs = {
        "rtret60_or_breakbox25": (bigbull["rt_return_from_d1_close"] >= 0.060)
        | (bigbull["rt_breakout_vs_box_top"] >= 0.025),
        "rtret70_or_breakbox35": (bigbull["rt_return_from_d1_close"] >= 0.070)
        | (bigbull["rt_breakout_vs_box_top"] >= 0.035),
        "rtret60_and_breakbox25": (bigbull["rt_return_from_d1_close"] >= 0.060)
        & (bigbull["rt_breakout_vs_box_top"] >= 0.025),
        "rtret70_and_breakbox35": (bigbull["rt_return_from_d1_close"] >= 0.070)
        & (bigbull["rt_breakout_vs_box_top"] >= 0.035),
    }

    rows: list[dict[str, Any]] = []
    for name, mask in configs.items():
        source_df = _dedupe_source(
            pd.concat(
                [_normalize_for_concat(volume5), _normalize_for_concat(bigbull[mask].copy())],
                ignore_index=True,
                sort=False,
            )
        )
        source_path = _write_source(name, source_df)
        for policy in ["stop_cd3_skip", "dd8_half_stop_cd3_skip"]:
            run_dir = OUT / "runs" / name / policy
            summary = _run_dynamic(source_path, run_dir, policy, START_DATE, end_date, sort_mode="trigger_time")
            rows.append(
                {
                    "variant": name,
                    "policy": policy,
                    **_summarize_sources(source_df),
                    "trade_count": summary["trade_count"],
                    "total_return": summary["total_return"],
                    "excess_return": summary["excess_return"],
                    "max_drawdown": summary["max_drawdown"],
                    "win_rate": summary["win_rate"],
                    "avg_trade_return": summary["avg_trade_return"],
                    "guard_days": summary.get("portfolio_guard_active_days"),
                    "half_weight_days": summary.get("half_weight_days"),
                    "attribution": _attribution(run_dir),
                    "monthly": _monthly(run_dir),
                }
            )

    df = pd.DataFrame(rows)
    df.to_csv(OUT / "combo_policy_summary.csv", index=False, encoding="utf-8-sig")
    (OUT / "combo_policy_summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    lines = [
        "# Breakout Intraday Combo Policy Probe",
        "",
        "| variant | policy | big_bull signals | trades | total | excess | max_dd | win | guard days | half days |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['variant']} | {row['policy']} | {row['big_bull_signals']} | {row['trade_count']} | "
            f"{_pct(row['total_return'])} | {_pct(row['excess_return'])} | {_pct(row['max_drawdown'])} | "
            f"{_pct(row['win_rate'])} | {row['guard_days']} | {row['half_weight_days']} |"
        )
    (OUT / "combo_policy_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    payload = {"out": str(OUT), "end_date": end_date, "rows": rows}
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the breakout combo source and refresh it to the latest trade date.")
    add_end_date_argument(parser)
    args = parser.parse_args()
    run(resolve_end_date(args.end_date))


if __name__ == "__main__":
    main()
