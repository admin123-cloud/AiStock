from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default, _pct  # noqa: E402
from scripts.gen2_backtest_risk_cool_dynamic_circuit import _run_dynamic  # noqa: E402
from utils.paths import report_path  # noqa: E402

DEFAULT_VOLUME5 = report_path("g2_volume5_sector_layer_probe", "volume5_sector_enriched.parquet")
DEFAULT_BREAKOUT = report_path("g2_sector_integration_probe", "sources", "base.parquet")
DEFAULT_LATEST_DATA = report_path("gen2_event_study_full", "v4_event_dataset.parquet")
DEFAULT_OUTPUT_DIR = report_path("gen2_v2_complete_strategy")
DEFAULT_OFFICIAL_RUN_DIR = report_path("gen2_v2_complete_strategy", "runs", "official", "full")
DEFAULT_LEGACY_330_RUN_DIR = report_path("gen2_v2_complete_strategy", "runs", "official_legacy_330", "full")
DEFAULT_SORT_PROBE_DIR = report_path("gen2_v2_complete_strategy", "runs", "sort_probe")
SORT_PROBE_MODES = ("rank", "score", "trigger_time", "g2_v2")

WINDOW_STARTS = {
    "full": "2024-07-09",
    "train": "2024-07-09",
    "valid": "2025-04-01",
    "blind_2026ytd": "2026-01-01",
}
FIXED_WINDOW_ENDS = {
    "train": "2025-03-31",
    "valid": "2025-12-31",
}

SECTOR_STRONG_THRESHOLD = 0.05
VOLUME5_SECTOR_SCORE_BONUS = 0.05
MAINLINE_THEME_COLUMNS = [
    "mainline_theme_match",
    "mainline_theme_level",
    "mainline_theme_label",
    "mainline_theme_action",
    "mainline_theme_weight_hint",
    "mainline_theme_score",
    "mainline_theme_risk_label",
    "mainline_theme_position_cap_hint",
    "mainline_theme_usage",
]


def _pct_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _normalize_dates(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in ["trade_date", "entry_date"]:
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], errors="coerce").dt.strftime("%Y-%m-%d")
    if "confirm_datetime" in out.columns:
        out["confirm_datetime"] = pd.to_datetime(out["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    return out


def _drop_aux(df: pd.DataFrame) -> pd.DataFrame:
    return df.drop(columns=[c for c in ["code6", "confirm_time"] if c in df.columns]).copy()


def _ensure_mainline_theme_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    defaults: dict[str, Any] = {
        "mainline_theme_match": False,
        "mainline_theme_weight_hint": 0.0,
        "mainline_theme_score": np.nan,
    }
    for col in MAINLINE_THEME_COLUMNS:
        if col not in out.columns:
            out[col] = defaults.get(col, "")
    return out


def _prepare_volume5(path: Path) -> pd.DataFrame:
    df = _normalize_dates(pd.read_parquet(path))
    df["source_family"] = "volume5"
    df["signal_family"] = "volume5_keep80_runup_sector_bonus"
    df["v4_rank_raw"] = pd.to_numeric(df["v4_rank"], errors="coerce").fillna(999).astype(int)
    df["v4_score_raw"] = pd.to_numeric(df["v4_score"], errors="coerce").fillna(0.0)
    l3_s3 = pd.to_numeric(df.get("l3_rt_strong3_ratio", df.get("l3_s3")), errors="coerce").fillna(-1.0)
    df["l3_s3"] = l3_s3
    df["sector_strong"] = l3_s3 >= SECTOR_STRONG_THRESHOLD
    df["sector_score_bonus"] = np.where(df["sector_strong"], VOLUME5_SECTOR_SCORE_BONUS, 0.0)
    df["v4_rank"] = df["v4_rank_raw"]
    df["v4_score"] = df["v4_score_raw"] + df["sector_score_bonus"]
    df["g2_v2_family_priority"] = 0
    df["g2_v2_buy_logic"] = np.where(
        df["sector_strong"],
        "volume5_keep80_runup + sector_score_bonus",
        "volume5_keep80_runup",
    )
    return df


def _prepare_breakout(path: Path, volume5_keys: set[tuple[str, str]]) -> pd.DataFrame:
    df = _normalize_dates(pd.read_parquet(path))
    df = df[df["source_family"].astype(str).eq("big_bull")].copy()
    l3_s3 = pd.to_numeric(df.get("l3_rt_strong3_ratio", df.get("l3_s3")), errors="coerce").fillna(-1.0)
    intraday_strength = (
        pd.to_numeric(df.get("rt_return_from_d1_close"), errors="coerce").fillna(-99.0).ge(0.06)
        | pd.to_numeric(df.get("rt_breakout_vs_box_top"), errors="coerce").fillna(-99.0).ge(0.025)
    )
    df["l3_s3"] = l3_s3
    df["sector_strong"] = l3_s3 >= SECTOR_STRONG_THRESHOLD
    df = df[df["sector_strong"] & intraday_strength].copy()
    if volume5_keys:
        keys = list(zip(df["entry_date"].astype(str), df["code"].astype(str)))
        keep_mask = pd.Series([key not in volume5_keys for key in keys], index=df.index, dtype=bool)
        df = df.loc[keep_mask].copy()
    df["signal_family"] = "breakout_big_bull_sector_strong"
    df["v4_rank_raw"] = pd.to_numeric(df["v4_rank"], errors="coerce").fillna(999).astype(int)
    df["v4_score_raw"] = pd.to_numeric(df["v4_score"], errors="coerce").fillna(0.0)
    df["sector_score_bonus"] = 0.0
    df["v4_rank"] = df["v4_rank_raw"]
    df["v4_score"] = df["v4_score_raw"]
    df["g2_v2_family_priority"] = 1
    df["g2_v2_buy_logic"] = "big_bull_rebreak_2_5d + intraday_strength + sector_strong"
    return df


def build_source(volume5_path: Path, breakout_path: Path, include_breakout: bool = True) -> pd.DataFrame:
    volume5 = _prepare_volume5(volume5_path)
    if include_breakout:
        volume5_keys = set(zip(volume5["entry_date"].astype(str), volume5["code"].astype(str)))
        breakout = _prepare_breakout(breakout_path, volume5_keys)
        merged = pd.concat([volume5, breakout], ignore_index=True, sort=False)
    else:
        merged = volume5.copy()
    merged = merged.dropna(subset=["entry_date", "code", "entry_price", "confirm_datetime"]).copy()
    merged["code"] = merged["code"].astype(str)
    merged["v4_rank"] = pd.to_numeric(merged["v4_rank"], errors="coerce").fillna(999).astype(int)
    merged["v4_score"] = pd.to_numeric(merged["v4_score"], errors="coerce").fillna(0.0)
    merged["entry_price"] = pd.to_numeric(merged["entry_price"], errors="coerce")
    merged = merged.sort_values(
        ["entry_date", "g2_v2_family_priority", "v4_score", "v4_rank", "confirm_datetime", "code"],
        ascending=[True, True, False, True, True, True],
        na_position="last",
    )
    return _ensure_mainline_theme_columns(_drop_aux(merged).reset_index(drop=True))


def _trade_lot_summary(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "trades.csv"
    if not path.exists():
        return {}
    trades = pd.read_csv(path)
    if trades.empty:
        return {"lot_count": 0}
    lots = trades.groupby(["buy_date", "code", "name"], dropna=False).agg(pnl=("pnl", "sum"))
    return {
        "lot_count": int(len(lots)),
        "lot_win_rate": float((lots["pnl"] > 0).mean()),
        "lot_avg_pnl": float(lots["pnl"].mean()),
    }


def _write_report(output_dir: Path, source: pd.DataFrame, rows: list[dict[str, Any]]) -> None:
    counts = source["signal_family"].fillna("").value_counts().to_dict()
    lines = [
        "# G2 第二代完整策略回测",
        "",
        "正式结构：主线一保留 volume5_keep80_runup，并对细分板块盘中扩散强的个股加 0.05 分；主线二只接入 big_bull 二次突破，且必须满足盘中个股强度和 l3_rt_strong3_ratio >= 0.05。",
        "",
        f"- 信号总数：{len(source)}",
        f"- volume5 主线：{int(counts.get('volume5_keep80_runup_sector_bonus', 0))}",
        f"- 突破主线：{int(counts.get('breakout_big_bull_sector_strong', 0))}",
        f"- 板块强扩散阈值：l3_rt_strong3_ratio >= {SECTOR_STRONG_THRESHOLD:.2f}",
        "",
        "| window | sort | signals | trades | lots | total | excess | max_dd | win | lot_win | avg_trade |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['window']} | {row['sort_mode']} | {row['signal_count']} | {row['trade_count']} | "
            f"{row.get('lot_count', '')} | {_pct_text(row['total_return'])} | {_pct_text(row['excess_return'])} | "
            f"{_pct_text(row['max_drawdown'])} | {_pct_text(row['win_rate'])} | {_pct_text(row.get('lot_win_rate'))} | "
            f"{_pct_text(row['avg_trade_return'])} |"
        )
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _copy_run(src: Path, dst: Path) -> None:
    if src.resolve() == dst.resolve():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for name in ["summary.json", "equity_curve.csv", "trades.csv", "signals.csv", "segment_summary.csv", "decision_ledger.csv"]:
        source = src / name
        if source.exists():
            shutil.copy2(source, dst / name)


def _build_sort_probe(
    source_path: Path,
    sort_probe_root: Path,
    windows: dict[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sort_mode in SORT_PROBE_MODES:
        for window, (start, end) in windows.items():
            run_dir = sort_probe_root / sort_mode / window
            summary = _run_dynamic(source_path, run_dir, "stop_cd3_skip", start, end, sort_mode=sort_mode)
            rows.append({"sort_mode": sort_mode, "window": window, **summary, **_trade_lot_summary(run_dir)})
    return rows


def _latest_source_date(source: pd.DataFrame) -> str:
    dates = pd.to_datetime(source.get("entry_date"), errors="coerce").dropna()
    if dates.empty:
        raise RuntimeError("G2 second-generation source has no valid entry_date.")
    return str(dates.max().strftime("%Y-%m-%d"))


def _latest_data_date(source: pd.DataFrame, latest_data_path: Path) -> str:
    if latest_data_path.exists():
        try:
            df = pd.read_parquet(latest_data_path, columns=["trade_date"])
            dates = pd.to_datetime(df["trade_date"], errors="coerce").dropna()
            if not dates.empty:
                return str(dates.max().strftime("%Y-%m-%d"))
        except Exception:
            pass
    return _latest_source_date(source)


def _resolve_windows(source: pd.DataFrame, latest_data_path: Path, end_date: str | None) -> dict[str, tuple[str, str]]:
    latest = str(end_date or "").strip() or _latest_data_date(source, latest_data_path)
    latest_ts = pd.Timestamp(latest)
    windows: dict[str, tuple[str, str]] = {}
    for window, start in WINDOW_STARTS.items():
        fixed_end = FIXED_WINDOW_ENDS.get(window)
        end = fixed_end if fixed_end and pd.Timestamp(fixed_end) <= latest_ts else latest
        if pd.Timestamp(start) <= pd.Timestamp(end):
            windows[window] = (start, end)
    return windows


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    source_dir = output_dir / "sources"
    run_root = output_dir / "runs" / "official"
    source_dir.mkdir(parents=True, exist_ok=True)
    use_legacy_330 = bool(getattr(args, "legacy_330", False))
    source_only = bool(getattr(args, "source_only", False))
    source = build_source(Path(args.volume5), Path(args.breakout), include_breakout=not use_legacy_330)
    latest_data_path = Path(args.latest_data)
    windows = _resolve_windows(source, latest_data_path, str(getattr(args, "end_date", "") or "").strip() or None)
    source_path = source_dir / "g2_v2_complete.parquet"
    source.to_parquet(source_path, index=False)
    source.to_csv(source_path.with_suffix(".csv"), index=False, encoding="utf-8-sig")

    rows: list[dict[str, Any]] = []
    if not source_only:
        for window, (start, end) in windows.items():
            run_dir = run_root / window
            summary = _run_dynamic(source_path, run_dir, "stop_cd3_skip", start, end, sort_mode="g2_v2")
            rows.append({"window": window, "sort_mode": "g2_v2", **summary, **_trade_lot_summary(run_dir)})

    sort_probe_rows: list[dict[str, Any]] = []
    if bool(getattr(args, "populate_sort_probe", False)) and not source_only:
        sort_probe_rows = _build_sort_probe(source_path, Path(args.sort_probe_dir), windows)

    result = pd.DataFrame(rows)
    result.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "strategy_code": "g2_alpha191_volume5_keep80_runup_legacy_330" if use_legacy_330 else "g2_alpha191_volume5_keep80_runup",
                "strategy_name": "G2 Alpha191 volume5 + breakout sector v2",
                "source": str(source_path),
                "latest_source_date": _latest_source_date(source),
                "latest_data_date": _latest_data_date(source, latest_data_path),
                "backtest_end_date": max(end for _, end in windows.values()) if windows else "",
                "rows": rows,
                "parameters": {
                    "sector_strong_threshold": SECTOR_STRONG_THRESHOLD,
                    "volume5_sector_score_bonus": VOLUME5_SECTOR_SCORE_BONUS,
                    "sort_mode": "g2_v2",
                    "legacy_330_mode": use_legacy_330,
                    "source_only": source_only,
                },
            },
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        ),
        encoding="utf-8",
    )
    _write_report(output_dir, source, rows)

    official_target = None
    if bool(args.replace_official) and not source_only:
        official_target = Path(args.official_legacy_run_dir if use_legacy_330 else args.official_run_dir)
        _copy_run(run_root / "full", official_target)

    payload = {
        "output_dir": str(output_dir),
        "source": str(source_path),
        "latest_source_date": _latest_source_date(source),
        "latest_data_date": _latest_data_date(source, latest_data_path),
        "backtest_end_date": max(end for _, end in windows.values()) if windows else "",
        "official_run_dir": str(official_target) if official_target else "",
        "rows": rows,
        "source_only": source_only,
        "sort_probe_dir": str(args.sort_probe_dir) if bool(getattr(args, "populate_sort_probe", False)) else "",
        "sort_probe_rows": sort_probe_rows,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Build official G2 second-generation complete strategy source and backtests.")
    parser.add_argument("--volume5", default=str(DEFAULT_VOLUME5))
    parser.add_argument("--breakout", default=str(DEFAULT_BREAKOUT))
    parser.add_argument("--latest-data", default=str(DEFAULT_LATEST_DATA))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--official-run-dir", default=str(DEFAULT_OFFICIAL_RUN_DIR))
    parser.add_argument("--legacy-330", action="store_true", help="Legacy 330%% mode: only use volume5 family and skip breakout complement.")
    parser.add_argument("--official-legacy-run-dir", default=str(DEFAULT_LEGACY_330_RUN_DIR))
    parser.add_argument("--end-date", default="", help="Optional backtest end date; blank means latest source entry_date.")
    parser.add_argument("--replace-official", action="store_true")
    parser.add_argument("--source-only", action="store_true", help="Only refresh official source and freshness summary; skip backtests.")
    parser.add_argument("--populate-sort-probe", action="store_true")
    parser.add_argument("--sort-probe-dir", default=str(DEFAULT_SORT_PROBE_DIR))
    run(parser.parse_args())


if __name__ == "__main__":
    main()
