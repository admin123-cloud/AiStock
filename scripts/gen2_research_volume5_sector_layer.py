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

import scripts.gen2_research_breakout_sector_filter_matrix as sector_filter_matrix  # noqa: E402
from scripts.gen2_backtest_risk_cool_dynamic_circuit import _run_dynamic  # noqa: E402
from scripts.gen2_runtime_dates import add_end_date_argument, resolve_end_date, resolve_window_ends  # noqa: E402
from scripts.gen2_research_breakout_sector_filter_matrix import (  # noqa: E402
    _members,
    _sector_intraday,
)
from utils.paths import report_path  # noqa: E402

SOURCE = report_path("gen2_breakout_buy_point_research", "sources", "volume5_dynamic_stop_cd3_mapped.parquet")
OUT = report_path("g2_volume5_sector_layer_probe")
from research.common.reporting import timestamp_json_default as _json_default


from research.common.reporting import percent_text as _pct


def _code6(code: Any) -> str:
    return str(code).split(".")[0].zfill(6)


def _num_col(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(-1.0, index=df.index, dtype=float)
    return pd.to_numeric(df[column], errors="coerce").fillna(-1.0)


def _resolve_volume5_source() -> Path:
    candidates = [
        SOURCE,
        report_path("gen2_alpha191_light_constraint_matrix", "sources", "volume5_keep80_runup_le100.parquet"),
        report_path("gen2_v2_complete_strategy", "sources", "g2_v2_complete.parquet"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("volume5 source missing. tried: " + " | ".join(str(item) for item in candidates))


def _enrich_volume5() -> pd.DataFrame:
    df = pd.read_parquet(_resolve_volume5_source()).copy()
    df["source_family"] = "volume5"
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["confirm_datetime"] = pd.to_datetime(df["confirm_datetime"], errors="coerce")
    df["confirm_time"] = df["confirm_datetime"].dt.strftime("%H:%M")
    df["code6"] = df["code"].map(_code6)

    members = _members(
        tuple(sorted(set(df["code6"].dropna().astype(str).str.zfill(6))))
    ).sort_values(["stock_code6", "level", "stock_count"], ascending=[True, True, True])
    sector_filter_matrix._MEMBERS_CACHE = members
    sector_map = {
        (r.stock_code6, int(r.level)): r
        for r in members.itertuples(index=False)
        if pd.notna(r.level)
    }
    rows: list[dict[str, Any]] = []
    for row in df.itertuples(index=False):
        out = row._asdict()
        for level in [1, 2, 3]:
            prefix = f"l{level}"
            sec = sector_map.get((out["code6"], level))
            if sec is None:
                out[f"{prefix}_sector_code"] = None
                out[f"{prefix}_sector_name"] = None
                continue
            out[f"{prefix}_sector_code"] = sec.sector_code
            out[f"{prefix}_sector_name"] = sec.sector_name
            out[f"{prefix}_sector_stock_count"] = int(sec.stock_count)
            stats = _sector_intraday(str(sec.sector_code), out["entry_date"], out["confirm_time"])
            for key, value in stats.items():
                out[f"{prefix}_rt_{key}"] = value
        rows.append(out)
    enriched = pd.DataFrame(rows)
    enriched["confirm_datetime"] = pd.to_datetime(enriched["confirm_datetime"], errors="coerce").dt.strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    enriched["l3_s3"] = _num_col(enriched, "l3_rt_strong3_ratio")
    enriched["l2_s3"] = _num_col(enriched, "l2_rt_strong3_ratio")
    enriched["l3_rise"] = _num_col(enriched, "l3_rt_rise_ratio")
    return enriched


def _drop_aux(df: pd.DataFrame) -> pd.DataFrame:
    return df.drop(columns=[c for c in ["confirm_time", "code6"] if c in df.columns]).copy()


def _write_source(name: str, df: pd.DataFrame) -> Path:
    source_dir = OUT / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"{name}.parquet"
    out = _drop_aux(df)
    out.to_parquet(path, index=False)
    out.to_csv(path.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    return path


def _variants(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    l3s3 = df["l3_s3"]
    l2s3 = df["l2_s3"]
    variants: dict[str, pd.DataFrame] = {
        "base": df,
        "gate_l3s3_03": df[l3s3 >= 0.03],
        "gate_l3s3_05": df[l3s3 >= 0.05],
        "gate_l3s3_06": df[l3s3 >= 0.06],
        "gate_l2or3": df[(l3s3 >= 0.05) | (l2s3 >= 0.08)],
    }
    for weight in [0.50, 0.70]:
        soft = df.copy()
        soft["alpha191_position_weight"] = np.where(l3s3 >= 0.05, 1.0, weight)
        variants[f"soft_w{int(weight * 100)}"] = soft
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


def run(end_date: str, force_rebuild: bool = False) -> dict[str, Any]:
    OUT.mkdir(parents=True, exist_ok=True)
    enriched_path = OUT / "volume5_sector_enriched.parquet"
    if enriched_path.exists() and not force_rebuild:
        enriched = pd.read_parquet(enriched_path)
    else:
        enriched = _enrich_volume5()
        enriched.to_parquet(enriched_path, index=False)
        enriched.to_csv(enriched_path.with_suffix(".csv"), index=False, encoding="utf-8-sig")

    rows: list[dict[str, Any]] = []
    windows = resolve_window_ends(end_date)
    for variant, source_df in _variants(enriched).items():
        source_path = _write_source(variant, source_df)
        counts = {
            "signals": int(len(source_df)),
            "sector_strong_signals": int((pd.to_numeric(source_df["l3_s3"], errors="coerce") >= 0.05).sum()),
        }
        for sort_mode in ["trigger_time", "rank"]:
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
    result.to_csv(OUT / "volume5_sector_layer_summary.csv", index=False, encoding="utf-8-sig")
    (OUT / "volume5_sector_layer_summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    lines = [
        "# Volume5 Sector Layer Probe",
        "",
        "| variant | sort | window | signals | strong | trades | lots | total | excess | max_dd | win | lot_win | avg_trade |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['variant']} | {row['sort_mode']} | {row['window']} | {row['signals']} | "
            f"{row['sector_strong_signals']} | {row['trades']} | {row.get('lot_count', '')} | "
            f"{_pct(row['total_return'])} | {_pct(row['excess_return'])} | {_pct(row['max_drawdown'])} | "
            f"{_pct(row['win_rate'])} | {_pct(row.get('lot_win_rate'))} | {_pct(row['avg_trade_return'])} |"
        )
    (OUT / "volume5_sector_layer_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    payload = {"out": str(OUT), "end_date": end_date, "rows": len(rows), "windows": windows, "force_rebuild": force_rebuild}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh volume5 sector layer probe to the latest trade date.")
    add_end_date_argument(parser)
    parser.add_argument("--force-rebuild", action="store_true", help="Rebuild sector enrichment even if cached output exists.")
    args = parser.parse_args()
    run(resolve_end_date(args.end_date), force_rebuild=bool(args.force_rebuild))


if __name__ == "__main__":
    main()
