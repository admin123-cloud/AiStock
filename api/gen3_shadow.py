"""
G3 shadow live read-only API.

This router exposes the independent G3 research chain status without feeding it
into the formal trading/order path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from fastapi import APIRouter, Query

from utils.logger import get_logger
from utils.paths import report_path, runtime_path

router = APIRouter(prefix="/gen3-shadow", tags=["G3影子策略"])
logger = get_logger("gen3_shadow")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_DIR = report_path("gen3_shadow_live_daily_update_v1")
DAILY_UPDATE_SCRIPT = PROJECT_ROOT / "scripts" / "gen3_shadow_live_daily_update_v1.py"
GUARDED_REPORT_DIR = report_path("gen3_guarded_shadow_only_v1")
GUARDED_RUNTIME_DIR = runtime_path("gen3_guarded_shadow")
GUARDED_UPDATE_SCRIPT = PROJECT_ROOT / "scripts" / "gen3_guarded_shadow_only_v1.py"
RANGE_FILTERED_REPORT_DIR = report_path("gen3_range_filtered_shadow_only_v2")
RANGE_FILTERED_RUNTIME_DIR = runtime_path("gen3_range_filtered_shadow")
RANGE_FILTERED_LIVE_SAFE_SCRIPT = PROJECT_ROOT / "scripts" / "gen3_build_range_filtered_live_safe_payload_v2.py"
RANGE_FILTERED_UPDATE_SCRIPT = PROJECT_ROOT / "scripts" / "gen3_range_filtered_shadow_only_v2.py"
ROUTE_EXECUTION_V3_DIR = report_path("gen3_route_execution_mandate_candidate_package_v3")
V4_RESEARCH_PACKAGE_DIR = report_path("gen3_v4_research_package_v1")
V4_STRONG_HISTORY_DIR = report_path("gen3_v4_strong_failure_attribution_v1")
V4_STRONG_ENTRY_QUALITY_DIR = report_path("gen3_v4_strong_entry_quality_audit_v1")
RANGE_SECOND_ACCEPT_STRUCTURE_DIR = report_path("gen3_range_second_acceptance_structure_veto_v1")
RANGE_SECOND_ACCEPT_SELECTED_VARIANT = "deep_and_reclaim_any_second_accept__not_floor_not_strong_unless_extreme_volume"
RANGE_SECOND_ACCEPT_SELECTED_SOURCE = "deep_and_reclaim_any_second_accept"
RANGE_SECOND_ACCEPT_SELECTED_RULE = "not_floor_not_strong_unless_extreme_volume"
RANGE_SECOND_ACCEPT_SELECTED_PROFILE = "cost30"


def _read_csv_records(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    df = pd.read_csv(path, low_memory=False)
    if limit is not None:
        df = df.head(limit)
    df = df.astype(object).where(pd.notna(df), None)
    return json.loads(df.to_json(orient="records", force_ascii=False))


def _read_first_record(path: Path) -> dict[str, Any]:
    rows = _read_csv_records(path, limit=1)
    return rows[0] if rows else {}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to read json artifact: %s", path)
        return {}


def _path_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "path": str(path),
            "modified_at": None,
            "size_bytes": None,
        }
    stat = path.stat()
    return {
        "exists": True,
        "path": str(path),
        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(sep=" ", timespec="seconds"),
        "size_bytes": stat.st_size,
    }


def _to_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _max_drawdown(series: pd.Series) -> float:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return 0.0
    return float((values / values.cummax() - 1.0).min())


def _annual_curve_summary(curve: pd.DataFrame, trades: pd.DataFrame) -> list[dict[str, Any]]:
    if curve.empty or "date" not in curve.columns or "equity" not in curve.columns:
        return []

    d = curve.copy()
    d["trade_date"] = pd.to_datetime(d["date"], errors="coerce")
    d["equity"] = pd.to_numeric(d["equity"], errors="coerce")
    d = d.dropna(subset=["trade_date", "equity"]).sort_values("trade_date")
    if d.empty:
        return []

    trade_df = trades.copy()
    if not trade_df.empty and "entry_date" in trade_df.columns:
        trade_df["entry_dt"] = pd.to_datetime(trade_df["entry_date"], errors="coerce")
        trade_df["year"] = trade_df["entry_dt"].dt.year
    else:
        trade_df = pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for year, g in d.groupby(d["trade_date"].dt.year, sort=True):
        start_equity = float(g["equity"].iloc[0])
        end_equity = float(g["equity"].iloc[-1])
        year_trades = trade_df[trade_df["year"].eq(year)] if not trade_df.empty and "year" in trade_df.columns else pd.DataFrame()
        win_rate = None
        ret_col = "stress_net_ret" if "stress_net_ret" in year_trades.columns else "policy_net_ret" if "policy_net_ret" in year_trades.columns else None
        if not year_trades.empty and ret_col:
            rets = pd.to_numeric(year_trades[ret_col], errors="coerce").dropna()
            if not rets.empty:
                win_rate = float((rets > 0).mean())
        rows.append(
            {
                "year": int(year),
                "start_date": str(g["date"].iloc[0]),
                "end_date": str(g["date"].iloc[-1]),
                "start_equity": start_equity,
                "end_equity": end_equity,
                "return": end_equity / start_equity - 1.0 if start_equity > 0 else None,
                "max_drawdown": _max_drawdown(g["equity"]),
                "trading_days": int(len(g)),
                "trade_count": int(len(year_trades)),
                "win_rate": win_rate,
                "avg_open_positions": _to_float(pd.to_numeric(g.get("open_positions"), errors="coerce").mean()) if "open_positions" in g.columns else None,
                "worst_open_mtm_ret": _to_float(pd.to_numeric(g.get("worst_open_mtm_ret"), errors="coerce").min()) if "worst_open_mtm_ret" in g.columns else None,
            }
        )
    return rows


def _drawdown_window(curve: pd.DataFrame) -> dict[str, Any]:
    if curve.empty or "date" not in curve.columns or "equity" not in curve.columns:
        return {}

    d = curve.copy()
    d["equity"] = pd.to_numeric(d["equity"], errors="coerce")
    d = d.dropna(subset=["equity"]).reset_index(drop=True)
    if d.empty:
        return {}

    d["peak"] = d["equity"].cummax()
    d["drawdown_calc"] = d["equity"] / d["peak"] - 1.0
    trough_idx = int(d["drawdown_calc"].idxmin())
    peak_idx = int(d.loc[:trough_idx, "equity"].idxmax())
    recovered = d.loc[trough_idx:][d.loc[trough_idx:, "equity"].ge(float(d.loc[peak_idx, "equity"]))]
    return {
        "peak_date": str(d.loc[peak_idx, "date"]),
        "trough_date": str(d.loc[trough_idx, "date"]),
        "recovery_date": str(recovered.iloc[0]["date"]) if not recovered.empty else None,
        "peak_equity": float(d.loc[peak_idx, "equity"]),
        "trough_equity": float(d.loc[trough_idx, "equity"]),
        "drawdown": float(d.loc[trough_idx, "drawdown_calc"]),
        "days_to_trough": int(trough_idx - peak_idx),
        "days_to_recover": int(recovered.index[0] - peak_idx) if not recovered.empty else None,
    }


def _route_trade_summary(trades: pd.DataFrame) -> list[dict[str, Any]]:
    if trades.empty or "route" not in trades.columns:
        return []

    d = trades.copy()
    ret_col = "stress_net_ret" if "stress_net_ret" in d.columns else "policy_net_ret" if "policy_net_ret" in d.columns else None
    d["stress_net_ret_num"] = pd.to_numeric(d[ret_col], errors="coerce") if ret_col else None
    d["realized_pnl_num"] = pd.to_numeric(d.get("realized_pnl"), errors="coerce")
    rows: list[dict[str, Any]] = []
    for route, g in d.groupby("route", sort=True):
        rets = g["stress_net_ret_num"].dropna()
        rows.append(
            {
                "route": route,
                "trade_count": int(len(g)),
                "win_rate": float((rets > 0).mean()) if not rets.empty else None,
                "avg_trade_return": float(rets.mean()) if not rets.empty else None,
                "worst_trade": float(rets.min()) if not rets.empty else None,
                "best_trade": float(rets.max()) if not rets.empty else None,
                "sum_realized_pnl": float(g["realized_pnl_num"].sum()) if "realized_pnl_num" in g else None,
            }
        )
    return rows


def _read_v4_strong_history(limit: int, offset: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = V4_STRONG_HISTORY_DIR / "strong_main_path_attribution.csv"
    entry_quality_path = V4_STRONG_ENTRY_QUALITY_DIR / "entry_quality_enriched.csv"
    if not path.exists():
        return [], {
            "total": 0,
            "limit": limit,
            "offset": offset,
            "returned": 0,
            "source": str(path),
            "exists": False,
        }

    df = pd.read_csv(path, low_memory=False)
    if entry_quality_path.exists():
        try:
            quality = pd.read_csv(entry_quality_path, low_memory=False)
            quality_keep = [
                col
                for col in [
                    "entry_date",
                    "code",
                    "entry_upper_shadow",
                    "entry_amount_ratio20",
                    "entry_break_high20",
                    "runup_from_60d_low",
                    "early_fail",
                ]
                if col in quality.columns
            ]
            if quality_keep:
                quality = quality[quality_keep].copy()
                quality["entry_date"] = pd.to_datetime(quality["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
                quality["code"] = quality["code"].astype(str)
                df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
                df["code"] = df["code"].astype(str)
                df = df.merge(quality, on=["entry_date", "code"], how="left")
        except Exception:
            logger.exception("Failed to merge G3 V4 strong entry quality artifact")

    for date_col in ["entry_date", "policy_exit_date"]:
        if date_col in df.columns:
            df[date_col] = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in [
        "policy_net_ret",
        "realized_pnl",
        "score",
        "strong_day_rank",
        "entry_price",
        "min_low_ret",
        "max_high_ret",
        "giveback_from_high",
        "d1_close_ret",
        "d2_close_ret",
        "nextopen_ret_delta",
        "index_hold_ret",
        "entry_upper_shadow",
        "entry_amount_ratio20",
        "entry_break_high20",
        "runup_from_60d_low",
    ]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "entry_date" in df.columns:
        df["entry_date_sort"] = pd.to_datetime(df["entry_date"], errors="coerce")
        df = df.sort_values(["entry_date_sort", "code"], ascending=[False, True])
    total = int(len(df))
    page = df.iloc[offset : offset + limit].copy()
    keep_cols = [
        col
        for col in [
            "entry_date",
            "policy_exit_date",
            "code",
            "name",
            "route",
            "route_source",
            "score",
            "strong_day_rank",
            "entry_price",
            "policy_net_ret",
            "realized_pnl",
            "min_low_ret",
            "max_high_ret",
            "giveback_from_high",
            "d1_close_ret",
            "d2_close_ret",
            "nextopen_ret_delta",
            "index_hold_ret",
            "failure_tag",
            "entry_upper_shadow",
            "entry_amount_ratio20",
            "entry_break_high20",
            "runup_from_60d_low",
            "early_fail",
        ]
        if col in page.columns
    ]
    records = json.loads(
        page[keep_cols].astype(object).where(pd.notna(page[keep_cols]), None).to_json(
            orient="records",
            force_ascii=False,
        )
    ) if keep_cols and not page.empty else []
    return records, {
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(records),
        "source": str(path),
        "entry_quality_source": str(entry_quality_path),
        "exists": True,
    }


def _records_from_df(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    return json.loads(df.astype(object).where(pd.notna(df), None).to_json(orient="records", force_ascii=False))


def _read_range_second_acceptance_research() -> dict[str, Any]:
    """
    Read the range/icepoint second-acceptance structure-veto candidate.

    This is an explicit research-only label for the G3 V4 research page. It is
    not a formal buy signal and must not be wired to any order path.
    """

    root = RANGE_SECOND_ACCEPT_STRUCTURE_DIR
    variant = RANGE_SECOND_ACCEPT_SELECTED_VARIANT
    profile = RANGE_SECOND_ACCEPT_SELECTED_PROFILE
    run_dir = root / f"{variant}__{profile}"
    summary_path = root / "summary.csv"
    windows_path = root / "window_metrics.csv"
    coverage_path = root / "coverage.csv"
    concentration_path = root / "concentration.csv"
    curve_path = run_dir / "mtm_equity_curve.csv"
    trades_path = run_dir / "closed_trades.csv"

    summary_rows = _read_csv_records(summary_path, limit=200)
    selected_summary = next(
        (row for row in summary_rows if row.get("variant") == variant and row.get("profile") == profile),
        {},
    )
    window_rows = [
        row
        for row in _read_csv_records(windows_path, limit=300)
        if row.get("variant") == variant and row.get("profile") == profile
    ]
    coverage = next(
        (
            row
            for row in _read_csv_records(coverage_path, limit=100)
            if row.get("source_variant") == RANGE_SECOND_ACCEPT_SELECTED_SOURCE
            and row.get("rule") == RANGE_SECOND_ACCEPT_SELECTED_RULE
        ),
        {},
    )
    concentration = next(
        (
            row
            for row in _read_csv_records(concentration_path, limit=100)
            if row.get("source_variant") == RANGE_SECOND_ACCEPT_SELECTED_SOURCE
            and row.get("rule") == RANGE_SECOND_ACCEPT_SELECTED_RULE
        ),
        {},
    )

    curve_df = pd.read_csv(curve_path, low_memory=False) if curve_path.exists() else pd.DataFrame()
    trades_df = pd.read_csv(trades_path, low_memory=False) if trades_path.exists() else pd.DataFrame()

    if not curve_df.empty:
        for col in ["cash", "reserved_principal", "equity", "open_positions", "opened", "realized_pnl", "peak", "drawdown", "ret_from_start"]:
            if col in curve_df.columns:
                curve_df[col] = pd.to_numeric(curve_df[col], errors="coerce")
        if "date" in curve_df.columns:
            curve_df["date"] = curve_df["date"].astype(str)

    if not trades_df.empty:
        if "entry_date" in trades_df.columns:
            trades_df["entry_date_sort"] = pd.to_datetime(trades_df["entry_date"], errors="coerce")
            trades_df = trades_df.sort_values(["entry_date_sort", "code"], ascending=[False, True])
        numeric_cols = [
            "policy_net_ret",
            "realized_pnl",
            "entry_price_used",
            "entry_price",
            "stake",
            "range_pos60",
            "close_position",
            "amount_ratio3",
            "gap_open",
            "index_mom20",
            "ice_score",
            "climax_score",
            "emotion_score",
            "rank_in_day",
        ]
        for col in numeric_cols:
            if col in trades_df.columns:
                trades_df[col] = pd.to_numeric(trades_df[col], errors="coerce")

    curve_keep = [
        col
        for col in ["date", "equity", "ret_from_start", "drawdown", "open_positions", "opened", "realized_pnl"]
        if col in curve_df.columns
    ]
    trade_keep = [
        col
        for col in [
            "entry_date",
            "policy_exit_date",
            "code",
            "name",
            "policy_net_ret",
            "realized_pnl",
            "entry_price_used",
            "stake",
            "confirm_datetime",
            "bar_time",
            "emotion_signal",
            "source_variant",
            "source_desc",
            "range_pos60",
            "close_position",
            "amount_ratio3",
            "gap_open",
            "index_mom20",
            "near_floor",
            "strong_reclaim",
            "extreme_accept",
            "d0_second_accept",
            "d1_second_accept",
            "d0_second_accept_datetime",
            "d1_second_accept_datetime",
            "desc",
        ]
        if col in trades_df.columns
    ]
    if "entry_date_sort" in trades_df.columns:
        trades_df = trades_df.drop(columns=["entry_date_sort"])

    first_date = str(curve_df["date"].iloc[0]) if not curve_df.empty and "date" in curve_df.columns else None
    last_date = str(curve_df["date"].iloc[-1]) if not curve_df.empty and "date" in curve_df.columns else None
    initial_capital = 150000.0
    final_equity = _to_float(curve_df["equity"].iloc[-1]) if not curve_df.empty and "equity" in curve_df.columns else None

    return {
        "key": variant,
        "profile": profile,
        "label_cn": "横盘/冰点二次承接结构否决版",
        "name_explain": {
            "deep_and_reclaim_any_second_accept": "箱体底部且日线修复，D0 后半日或 D1 任一出现 30m 放量承接。",
            "not_floor_not_strong_unless_extreme_volume": "否决既不够贴近箱体底部、日线修复也不够强的样本；除非 30m 出现极端放量承接。",
            "cost30": "按 30bps 交易成本测算的展示口径。",
        },
        "research_only": True,
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "backtest_start": "2020-01-01",
        "backtest_end": "2026-05-29",
        "curve_start": first_date,
        "curve_end": last_date,
        "initial_capital": initial_capital,
        "final_equity": final_equity,
        "summary": selected_summary,
        "overview": {
            "start_date": first_date,
            "end_date": last_date,
            "trading_days": int(len(curve_df)),
            "trade_count": int(len(trades_df)),
            "total_return": final_equity / initial_capital - 1.0 if final_equity else None,
            "max_drawdown": _max_drawdown(curve_df["equity"]) if not curve_df.empty and "equity" in curve_df.columns else None,
            "win_rate": selected_summary.get("win_rate"),
            "avg_trade_return": selected_summary.get("avg_trade_return"),
            "worst_trade": selected_summary.get("worst_trade"),
        },
        "windows": window_rows,
        "coverage": coverage,
        "concentration": concentration,
        "curve": _records_from_df(curve_df[curve_keep]) if curve_keep else [],
        "trades": _records_from_df(trades_df[trade_keep]) if trade_keep else [],
        "artifacts": {
            "report_dir": _path_status(root),
            "run_dir": _path_status(run_dir),
            "summary": _path_status(summary_path),
            "windows": _path_status(windows_path),
            "coverage": _path_status(coverage_path),
            "concentration": _path_status(concentration_path),
            "curve": _path_status(curve_path),
            "closed_trades": _path_status(trades_path),
        },
    }


def _run_daily_update(as_of: str | None, allow_after_close: bool) -> dict[str, Any]:
    if not DAILY_UPDATE_SCRIPT.exists():
        return {
            "ran": False,
            "ok": False,
            "returncode": None,
            "error": f"daily update script not found: {DAILY_UPDATE_SCRIPT}",
        }

    cmd = [
        sys.executable,
        str(DAILY_UPDATE_SCRIPT),
        "--out-dir",
        str(DEFAULT_REPORT_DIR),
    ]
    if as_of:
        cmd.extend(["--as-of", as_of])
    if allow_after_close:
        cmd.append("--allow-after-close")

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except Exception as exc:
        logger.exception("G3 shadow daily update failed to run")
        return {
            "ran": True,
            "ok": False,
            "returncode": None,
            "error": str(exc),
        }

    return {
        "ran": True,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
    }


def _run_guarded_update(as_of: str | None) -> dict[str, Any]:
    if not GUARDED_UPDATE_SCRIPT.exists():
        return {
            "ran": False,
            "ok": False,
            "returncode": None,
            "error": f"guarded update script not found: {GUARDED_UPDATE_SCRIPT}",
        }

    cmd = [
        sys.executable,
        str(GUARDED_UPDATE_SCRIPT),
        "--out-dir",
        str(GUARDED_REPORT_DIR),
        "--runtime-dir",
        str(GUARDED_RUNTIME_DIR),
    ]
    if as_of:
        cmd.extend(["--as-of", as_of])

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except Exception as exc:
        logger.exception("G3 guarded shadow update failed to run")
        return {
            "ran": True,
            "ok": False,
            "returncode": None,
            "error": str(exc),
        }

    return {
        "ran": True,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
    }


def _run_range_filtered_update(as_of: str | None) -> dict[str, Any]:
    missing = [
        str(path)
        for path in [RANGE_FILTERED_LIVE_SAFE_SCRIPT, RANGE_FILTERED_UPDATE_SCRIPT]
        if not path.exists()
    ]
    if missing:
        return {
            "ran": False,
            "ok": False,
            "returncode": None,
            "error": f"range-filtered update script not found: {', '.join(missing)}",
        }

    steps: list[dict[str, Any]] = []
    commands = [
        [sys.executable, str(RANGE_FILTERED_LIVE_SAFE_SCRIPT)],
        [
            sys.executable,
            str(RANGE_FILTERED_UPDATE_SCRIPT),
            "--out-dir",
            str(RANGE_FILTERED_REPORT_DIR),
            "--runtime-dir",
            str(RANGE_FILTERED_RUNTIME_DIR),
        ],
    ]
    if as_of:
        commands[-1].extend(["--as-of", as_of])

    for cmd in commands:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except Exception as exc:
            logger.exception("G3 range-filtered shadow update failed to run")
            steps.append(
                {
                    "cmd": cmd,
                    "ok": False,
                    "returncode": None,
                    "error": str(exc),
                }
            )
            return {
                "ran": True,
                "ok": False,
                "returncode": None,
                "steps": steps,
                "error": str(exc),
            }

        step = {
            "cmd": cmd,
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
        }
        steps.append(step)
        if proc.returncode != 0:
            return {
                "ran": True,
                "ok": False,
                "returncode": proc.returncode,
                "steps": steps,
            }

    return {
        "ran": True,
        "ok": True,
        "returncode": 0,
        "steps": steps,
    }


@router.get("/live")
async def get_gen3_shadow_live(
    as_of: Optional[str] = Query(default=None, description="Optional as-of timestamp for refresh mode."),
    refresh: bool = Query(default=False, description="Run the G3 shadow daily update before reading output."),
    allow_after_close: bool = Query(default=False, description="Replay helper for after-close research checks."),
    limit: int = Query(default=20, ge=1, le=200, description="Maximum candidate/block rows returned."),
) -> dict[str, Any]:
    """
    Return the latest G3 shadow live status.

    Default behavior is read-only: it reads the existing daily update artifacts.
    `refresh=true` reruns only the independent G3 shadow daily update script and
    still returns observe-only candidates.
    """

    refresh_result = _run_daily_update(as_of=as_of, allow_after_close=allow_after_close) if refresh else {
        "ran": False,
        "ok": None,
        "returncode": None,
    }

    diagnosis_path = DEFAULT_REPORT_DIR / "g3_shadow_daily_diagnosis.csv"
    summary_path = DEFAULT_REPORT_DIR / "entry" / "g3_shadow_live_summary.csv"
    candidates_path = DEFAULT_REPORT_DIR / "entry" / "g3_shadow_live_candidates.csv"
    blocked_path = DEFAULT_REPORT_DIR / "entry" / "g3_shadow_live_blocked.csv"
    recent_dates_path = DEFAULT_REPORT_DIR / "entry" / "g3_shadow_live_recent_signal_dates.csv"

    diagnosis = _read_first_record(diagnosis_path)
    summary = _read_first_record(summary_path)
    candidates = _read_csv_records(candidates_path, limit=limit)
    blocked = _read_csv_records(blocked_path, limit=limit)
    recent_signal_dates = _read_csv_records(recent_dates_path, limit=20)

    if not diagnosis:
        diagnosis = {
            "diagnosis_code": "NO_G3_SHADOW_REPORT",
            "diagnosis": "未找到 G3 shadow daily update 结果；需要先运行 Step53 日更链路。",
            "display_candidates": 0,
            "blocked_rows": 0,
            "auto_order_allowed_rows": 0,
        }

    return {
        "ok": True,
        "mode": "observe_only",
        "auto_order_allowed": False,
        "message": diagnosis.get("diagnosis", ""),
        "diagnosis": diagnosis,
        "summary": summary,
        "candidates": candidates,
        "blocked": blocked,
        "recent_signal_dates": recent_signal_dates,
        "refresh": refresh_result,
        "artifacts": {
            "report_dir": _path_status(DEFAULT_REPORT_DIR),
            "diagnosis": _path_status(diagnosis_path),
            "summary": _path_status(summary_path),
            "candidates": _path_status(candidates_path),
            "blocked": _path_status(blocked_path),
            "recent_signal_dates": _path_status(recent_dates_path),
        },
        "guardrails": {
            "independent_from_g2": True,
            "formal_buy_signal": False,
            "order_path_enabled": False,
            "requires_visibility_proof_before_live_trading": True,
        },
    }


@router.get("/route-execution-v3/backtest")
async def get_gen3_route_execution_v3_backtest(
    trade_limit: int = Query(default=120, ge=1, le=1000, description="Maximum closed trade rows returned."),
    trade_offset: int = Query(default=0, ge=0, description="Closed trade row offset after descending entry date sort."),
) -> dict[str, Any]:
    """
    Return the dedicated G3 V3 historical backtest payload.

    This is read-only research data. It does not expose formal buy signals,
    order routing, or any live execution switch.
    """

    summary_json_path = ROUTE_EXECUTION_V3_DIR / "summary.json"
    summary_csv_path = ROUTE_EXECUTION_V3_DIR / "g3_route_execution_mandate_candidate_summary.csv"
    windows_path = ROUTE_EXECUTION_V3_DIR / "g3_route_execution_mandate_candidate_windows.csv"
    routes_path = ROUTE_EXECUTION_V3_DIR / "g3_route_execution_mandate_candidate_route_attribution.csv"
    guard_audit_path = ROUTE_EXECUTION_V3_DIR / "g3_v3_sector_index_guard_audit.csv"
    guard_windows_path = ROUTE_EXECUTION_V3_DIR / "g3_v3_sector_index_guard_windows.csv"
    goal_path = ROUTE_EXECUTION_V3_DIR / "g3_route_execution_mandate_goal_audit.csv"
    visibility_path = ROUTE_EXECUTION_V3_DIR / "g3_route_execution_mandate_visibility_audit.csv"
    trades_path = ROUTE_EXECUTION_V3_DIR / "g3_route_execution_mandate_candidate_closed_trades.csv"
    curve_path = ROUTE_EXECUTION_V3_DIR / "g3_route_execution_mandate_candidate_equity_curve.csv"

    summary = _read_json(summary_json_path)
    profile_rows = _read_csv_records(summary_csv_path, limit=20)
    window_rows = _read_csv_records(windows_path, limit=120)
    route_rows = _read_csv_records(routes_path, limit=120)
    guard_audit_rows = _read_csv_records(guard_audit_path, limit=30)
    guard_window_rows = _read_csv_records(guard_windows_path, limit=120)
    goal_rows = _read_csv_records(goal_path, limit=30)
    visibility_rows = _read_csv_records(visibility_path, limit=50)

    curve_df = pd.read_csv(curve_path, low_memory=False) if curve_path.exists() else pd.DataFrame()
    trades_df = pd.read_csv(trades_path, low_memory=False) if trades_path.exists() else pd.DataFrame()

    if not curve_df.empty:
        for col in [
            "cash",
            "reserved_principal",
            "mtm_value",
            "equity",
            "open_positions",
            "opened",
            "skipped",
            "realized_pnl",
            "worst_open_mtm_ret",
            "missing_close_positions",
            "peak",
            "drawdown",
            "ret_from_start",
        ]:
            if col in curve_df.columns:
                curve_df[col] = pd.to_numeric(curve_df[col], errors="coerce")
        curve_df["date"] = curve_df["date"].astype(str)

    if not trades_df.empty:
        if "entry_date" in trades_df.columns:
            trades_df["entry_date_sort"] = pd.to_datetime(trades_df["entry_date"], errors="coerce")
            trades_df = trades_df.sort_values(["entry_date_sort", "code"], ascending=[False, True])
        for col in ["stress_net_ret", "policy_net_ret", "realized_pnl", "score", "entry_price", "stress_exit_price"]:
            if col in trades_df.columns:
                trades_df[col] = pd.to_numeric(trades_df[col], errors="coerce")

    curve_records = []
    if not curve_df.empty:
        keep_cols = [
            col
            for col in [
                "date",
                "equity",
                "ret_from_start",
                "drawdown",
                "cash",
                "reserved_principal",
                "mtm_value",
                "open_positions",
                "opened",
                "skipped",
                "realized_pnl",
                "worst_open_mtm_ret",
                "missing_close_positions",
            ]
            if col in curve_df.columns
        ]
        curve_records = json.loads(
            curve_df[keep_cols].astype(object).where(pd.notna(curve_df[keep_cols]), None).to_json(
                orient="records",
                force_ascii=False,
            )
        )

    trade_total = int(len(trades_df))
    trade_page = trades_df.iloc[trade_offset : trade_offset + trade_limit].copy() if not trades_df.empty else pd.DataFrame()
    if "entry_date_sort" in trade_page.columns:
        trade_page = trade_page.drop(columns=["entry_date_sort"])
    trade_records = json.loads(
        trade_page.astype(object).where(pd.notna(trade_page), None).to_json(orient="records", force_ascii=False)
    ) if not trade_page.empty else []

    first_date = str(curve_df["date"].iloc[0]) if not curve_df.empty and "date" in curve_df.columns else None
    last_date = str(curve_df["date"].iloc[-1]) if not curve_df.empty and "date" in curve_df.columns else None
    final_equity = _to_float(curve_df["equity"].iloc[-1]) if not curve_df.empty and "equity" in curve_df.columns else None
    initial_capital = 150000.0

    return {
        "ok": True,
        "mode": "research_backtest_only",
        "candidate": "g3_route_execution_mandate_v3",
        "auto_order_allowed": False,
        "formal_buy_signal": False,
        "order_path_enabled": False,
        "summary": summary,
        "overview": {
            "start_date": first_date,
            "end_date": last_date,
            "trading_days": int(len(curve_df)),
            "trade_count": trade_total,
            "initial_capital": initial_capital,
            "final_equity": final_equity,
            "total_return": final_equity / initial_capital - 1.0 if final_equity else None,
            "max_drawdown": _max_drawdown(curve_df["equity"]) if not curve_df.empty and "equity" in curve_df.columns else None,
            "worst_open_mtm_ret": _to_float(curve_df["worst_open_mtm_ret"].min()) if not curve_df.empty and "worst_open_mtm_ret" in curve_df.columns else None,
            "avg_open_positions": _to_float(curve_df["open_positions"].mean()) if not curve_df.empty and "open_positions" in curve_df.columns else None,
            "max_open_positions": int(curve_df["open_positions"].max()) if not curve_df.empty and "open_positions" in curve_df.columns else None,
        },
        "profiles": profile_rows,
        "windows": window_rows,
        "route_attribution": route_rows,
        "sector_index_guard_audit": guard_audit_rows,
        "sector_index_guard_windows": guard_window_rows,
        "annual": _annual_curve_summary(curve_df, trades_df),
        "drawdown_window": _drawdown_window(curve_df),
        "route_trade_summary": _route_trade_summary(trades_df),
        "goal_audit": goal_rows,
        "visibility_audit": visibility_rows,
        "curve": curve_records,
        "trades": trade_records,
        "trade_page": {
            "total": trade_total,
            "limit": trade_limit,
            "offset": trade_offset,
            "returned": len(trade_records),
        },
        "artifacts": {
            "report_dir": _path_status(ROUTE_EXECUTION_V3_DIR),
            "summary_json": _path_status(summary_json_path),
            "summary_csv": _path_status(summary_csv_path),
            "windows": _path_status(windows_path),
            "route_attribution": _path_status(routes_path),
            "sector_index_guard_audit": _path_status(guard_audit_path),
            "sector_index_guard_windows": _path_status(guard_windows_path),
            "goal_audit": _path_status(goal_path),
            "visibility_audit": _path_status(visibility_path),
            "curve": _path_status(curve_path),
            "closed_trades": _path_status(trades_path),
        },
        "guardrails": {
            "independent_from_g2_runtime": True,
            "research_only": True,
            "historical_backtest_only": True,
            "formal_buy_signal": False,
            "order_path_enabled": False,
            "auto_order_allowed": False,
            "requires_same_day_fill_audit_before_shadow_payload": True,
        },
    }


@router.get("/v4-research/backtest")
async def get_gen3_v4_research_backtest(
    variant: str = Query(default="v4_h10_margin", pattern="^v4_h(5|10)_margin$"),
    profile: str = Query(
        default="cost30",
        pattern="^cost(30|50|100)(_range_shock2|_all_shock2)?$",
        description="Research stress profile generated by the V4 package.",
    ),
    trade_limit: int = Query(default=160, ge=1, le=1000),
    trade_offset: int = Query(default=0, ge=0),
    strong_trade_limit: int = Query(default=300, ge=1, le=1000),
    strong_trade_offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """
    Return the G3 V4 research package payload.

    V4 is still a research candidate: no formal buy signal, no shadow live
    payload, and no order path are exposed by this endpoint.
    """

    run_dir = V4_RESEARCH_PACKAGE_DIR / f"{variant}__{profile}"
    summary_json_path = V4_RESEARCH_PACKAGE_DIR / "summary.json"
    summary_csv_path = V4_RESEARCH_PACKAGE_DIR / "g3_v4_research_summary.csv"
    windows_path = V4_RESEARCH_PACKAGE_DIR / "g3_v4_research_windows.csv"
    routes_path = V4_RESEARCH_PACKAGE_DIR / "g3_v4_research_route_attribution.csv"
    goal_path = V4_RESEARCH_PACKAGE_DIR / "g3_v4_research_goal_audit.csv"
    guard_audit_path = V4_RESEARCH_PACKAGE_DIR / "g3_v4_sector_index_guard_audit.csv"
    curve_path = run_dir / "mtm_equity_curve.csv"
    trades_path = run_dir / "closed_trades.csv"

    summary = _read_json(summary_json_path)
    profile_rows = _read_csv_records(summary_csv_path, limit=100)
    window_rows = _read_csv_records(windows_path, limit=200)
    route_rows = _read_csv_records(routes_path, limit=200)
    goal_rows = _read_csv_records(goal_path, limit=30)
    guard_audit_rows = _read_csv_records(guard_audit_path, limit=20)

    selected_profiles = [row for row in profile_rows if row.get("variant") == variant and row.get("profile") == profile]
    selected_windows = [row for row in window_rows if row.get("variant") == variant and row.get("profile") == profile]
    selected_routes = [row for row in route_rows if row.get("variant") == variant and row.get("profile") == profile]

    curve_df = pd.read_csv(curve_path, low_memory=False) if curve_path.exists() else pd.DataFrame()
    trades_df = pd.read_csv(trades_path, low_memory=False) if trades_path.exists() else pd.DataFrame()

    if not curve_df.empty:
        for col in [
            "cash",
            "reserved_principal",
            "mtm_value",
            "equity",
            "open_positions",
            "opened",
            "skipped",
            "realized_pnl",
            "worst_open_mtm_ret",
            "missing_close_positions",
            "peak",
            "drawdown",
            "ret_from_start",
            "open_down_panic",
            "open_range_gap",
            "open_strong_main",
        ]:
            if col in curve_df.columns:
                curve_df[col] = pd.to_numeric(curve_df[col], errors="coerce")
        curve_df["date"] = curve_df["date"].astype(str)

    if not trades_df.empty:
        if "entry_date" in trades_df.columns:
            trades_df["entry_date_sort"] = pd.to_datetime(trades_df["entry_date"], errors="coerce")
            trades_df = trades_df.sort_values(["entry_date_sort", "code"], ascending=[False, True])
        for col in ["policy_net_ret", "realized_pnl", "score", "entry_price", "exit_value", "stake"]:
            if col in trades_df.columns:
                trades_df[col] = pd.to_numeric(trades_df[col], errors="coerce")

    curve_records = []
    if not curve_df.empty:
        keep_cols = [
            col
            for col in [
                "date",
                "equity",
                "ret_from_start",
                "drawdown",
                "cash",
                "reserved_principal",
                "mtm_value",
                "open_positions",
                "opened",
                "skipped",
                "realized_pnl",
                "worst_open_mtm_ret",
                "missing_close_positions",
                "open_down_panic",
                "open_range_gap",
                "open_strong_main",
            ]
            if col in curve_df.columns
        ]
        curve_records = json.loads(
            curve_df[keep_cols].astype(object).where(pd.notna(curve_df[keep_cols]), None).to_json(
                orient="records",
                force_ascii=False,
            )
        )

    trade_total = int(len(trades_df))
    trade_page = trades_df.iloc[trade_offset : trade_offset + trade_limit].copy() if not trades_df.empty else pd.DataFrame()
    if "entry_date_sort" in trade_page.columns:
        trade_page = trade_page.drop(columns=["entry_date_sort"])
    trade_records = json.loads(
        trade_page.astype(object).where(pd.notna(trade_page), None).to_json(orient="records", force_ascii=False)
    ) if not trade_page.empty else []
    strong_history_records, strong_history_page = _read_v4_strong_history(strong_trade_limit, strong_trade_offset)
    range_second_acceptance_research = _read_range_second_acceptance_research()

    first_date = str(curve_df["date"].iloc[0]) if not curve_df.empty and "date" in curve_df.columns else None
    last_date = str(curve_df["date"].iloc[-1]) if not curve_df.empty and "date" in curve_df.columns else None
    final_equity = _to_float(curve_df["equity"].iloc[-1]) if not curve_df.empty and "equity" in curve_df.columns else None
    initial_capital = 150000.0

    return {
        "ok": True,
        "mode": "v4_research_backtest_only",
        "candidate": "g3_v4_research_package_v1",
        "variant": variant,
        "profile": profile,
        "auto_order_allowed": False,
        "formal_buy_signal": False,
        "order_path_enabled": False,
        "summary": summary,
        "overview": {
            "start_date": first_date,
            "end_date": last_date,
            "trading_days": int(len(curve_df)),
            "trade_count": trade_total,
            "initial_capital": initial_capital,
            "final_equity": final_equity,
            "total_return": final_equity / initial_capital - 1.0 if final_equity else None,
            "max_drawdown": _max_drawdown(curve_df["equity"]) if not curve_df.empty and "equity" in curve_df.columns else None,
            "worst_open_mtm_ret": _to_float(curve_df["worst_open_mtm_ret"].min()) if not curve_df.empty and "worst_open_mtm_ret" in curve_df.columns else None,
            "avg_open_positions": _to_float(curve_df["open_positions"].mean()) if not curve_df.empty and "open_positions" in curve_df.columns else None,
            "max_open_positions": int(curve_df["open_positions"].max()) if not curve_df.empty and "open_positions" in curve_df.columns else None,
        },
        "profiles": profile_rows,
        "selected_profile": selected_profiles[0] if selected_profiles else {},
        "windows": selected_windows,
        "route_attribution": selected_routes,
        "annual": _annual_curve_summary(curve_df, trades_df),
        "drawdown_window": _drawdown_window(curve_df),
        "route_trade_summary": _route_trade_summary(trades_df),
        "goal_audit": goal_rows,
        "sector_index_guard_audit": guard_audit_rows,
        "curve": curve_records,
        "trades": trade_records,
        "trade_page": {
            "total": trade_total,
            "limit": trade_limit,
            "offset": trade_offset,
            "returned": len(trade_records),
        },
        "strong_history_trades": strong_history_records,
        "strong_history_page": strong_history_page,
        "range_second_acceptance_research": range_second_acceptance_research,
        "artifacts": {
            "report_dir": _path_status(V4_RESEARCH_PACKAGE_DIR),
            "run_dir": _path_status(run_dir),
            "summary_json": _path_status(summary_json_path),
            "summary_csv": _path_status(summary_csv_path),
            "windows": _path_status(windows_path),
            "route_attribution": _path_status(routes_path),
            "goal_audit": _path_status(goal_path),
            "sector_index_guard_audit": _path_status(guard_audit_path),
            "curve": _path_status(curve_path),
            "closed_trades": _path_status(trades_path),
            "strong_history": _path_status(V4_STRONG_HISTORY_DIR / "strong_main_path_attribution.csv"),
            "strong_entry_quality": _path_status(V4_STRONG_ENTRY_QUALITY_DIR / "entry_quality_enriched.csv"),
        },
        "guardrails": {
            "independent_from_g2_runtime": True,
            "research_only": True,
            "historical_backtest_only": True,
            "formal_buy_signal": False,
            "order_path_enabled": False,
            "auto_order_allowed": False,
            "requires_range_sample_expansion_before_live_trading": True,
            "requires_all_chain_execution_shock_fix_before_live_trading": True,
            "requires_live_safe_payload_before_shadow_monitor": True,
        },
    }


@router.get("/guarded")
async def get_gen3_guarded_shadow(
    as_of: Optional[str] = Query(default=None, description="Optional as-of timestamp for refresh mode."),
    refresh: bool = Query(default=False, description="Run the G3 guarded shadow-only update before reading output."),
    limit: int = Query(default=20, ge=1, le=200, description="Maximum candidate rows returned."),
) -> dict[str, Any]:
    """
    Return the G3 guarded candidate observation status.

    This endpoint is deliberately read-only from the trading perspective. Even
    when `refresh=true`, it only rebuilds the guarded shadow-only artifacts and
    keeps all order/formal-signal switches disabled.
    """

    refresh_result = _run_guarded_update(as_of=as_of) if refresh else {
        "ran": False,
        "ok": None,
        "returncode": None,
    }

    latest_summary_path = GUARDED_RUNTIME_DIR / "latest_summary.json"
    latest_candidates_path = GUARDED_RUNTIME_DIR / "latest_candidates.csv"
    summary_path = GUARDED_REPORT_DIR / "g3_guarded_shadow_summary.csv"
    recent_path = GUARDED_REPORT_DIR / "g3_guarded_shadow_recent_candidates.csv"
    isolation_path = GUARDED_REPORT_DIR / "g3_guarded_shadow_isolation_manifest.csv"

    latest_summary = _read_json(latest_summary_path)
    summary = _read_first_record(summary_path)
    candidates = _read_csv_records(latest_candidates_path, limit=limit)
    recent_candidates = _read_csv_records(recent_path, limit=limit)
    isolation = _read_csv_records(isolation_path, limit=50)

    if not latest_summary and not summary:
        latest_summary = {
            "diagnosis_code": "NO_G3_GUARDED_SHADOW_REPORT",
            "diagnosis": "未找到 G3 guarded shadow-only 观察产物；需要先运行 guarded 影子观察脚本。",
            "today_shadow_candidates": 0,
            "auto_order_allowed_rows": 0,
            "formal_buy_signal_rows": 0,
            "order_path_enabled_rows": 0,
        }

    diagnosis = latest_summary.get("diagnosis") or summary.get("diagnosis", "")

    return {
        "ok": True,
        "mode": "guarded_shadow_only_observe",
        "auto_order_allowed": False,
        "formal_buy_signal": False,
        "order_path_enabled": False,
        "message": diagnosis,
        "summary": latest_summary or summary,
        "csv_summary": summary,
        "candidates": candidates,
        "recent_candidates": recent_candidates,
        "isolation": isolation,
        "refresh": refresh_result,
        "artifacts": {
            "runtime_dir": _path_status(GUARDED_RUNTIME_DIR),
            "report_dir": _path_status(GUARDED_REPORT_DIR),
            "latest_summary": _path_status(latest_summary_path),
            "latest_candidates": _path_status(latest_candidates_path),
            "summary": _path_status(summary_path),
            "recent_candidates": _path_status(recent_path),
            "isolation": _path_status(isolation_path),
        },
        "guardrails": {
            "independent_from_g2": True,
            "uses_guarded_candidate_package": True,
            "formal_buy_signal": False,
            "order_path_enabled": False,
            "auto_order_allowed": False,
            "requires_intraday_visibility_audit_before_live_trading": True,
            "requires_execution_pressure_review_before_live_trading": True,
        },
    }


@router.get("/range-filtered")
async def get_gen3_range_filtered_shadow(
    as_of: Optional[str] = Query(default=None, description="Optional as-of timestamp for refresh mode."),
    refresh: bool = Query(default=False, description="Run the G3 range-filtered shadow-only update before reading output."),
    limit: int = Query(default=20, ge=1, le=200, description="Maximum candidate rows returned."),
) -> dict[str, Any]:
    """
    Return the G3 range-filtered V2 observation status.

    This endpoint is still shadow-only. It exposes the V2 candidate that absorbed
    the G2 volume5 strong-market idea and filtered noisy range-gap samples, but
    it never enables formal buy signals or order routing.
    """

    refresh_result = _run_range_filtered_update(as_of=as_of) if refresh else {
        "ran": False,
        "ok": None,
        "returncode": None,
    }

    latest_summary_path = RANGE_FILTERED_RUNTIME_DIR / "latest_summary.json"
    latest_candidates_path = RANGE_FILTERED_RUNTIME_DIR / "latest_candidates.csv"
    summary_path = RANGE_FILTERED_REPORT_DIR / "g3_range_filtered_shadow_summary.csv"
    route_summary_path = RANGE_FILTERED_REPORT_DIR / "g3_range_filtered_shadow_route_summary.csv"
    recent_path = RANGE_FILTERED_REPORT_DIR / "g3_range_filtered_shadow_recent_candidates.csv"
    isolation_path = RANGE_FILTERED_REPORT_DIR / "g3_range_filtered_shadow_isolation_manifest.csv"

    latest_summary = _read_json(latest_summary_path)
    summary = _read_first_record(summary_path)
    route_summary = _read_csv_records(route_summary_path, limit=50)
    candidates = _read_csv_records(latest_candidates_path, limit=limit)
    recent_candidates = _read_csv_records(recent_path, limit=limit)
    isolation = _read_csv_records(isolation_path, limit=50)

    if not latest_summary and not summary:
        latest_summary = {
            "diagnosis_code": "NO_G3_RANGE_FILTERED_SHADOW_REPORT",
            "diagnosis": "No G3 range-filtered V2 shadow artifacts found.",
            "today_shadow_candidates": 0,
            "auto_order_allowed_rows": 0,
            "formal_buy_signal_rows": 0,
            "order_path_enabled_rows": 0,
        }

    diagnosis = latest_summary.get("diagnosis") or summary.get("diagnosis", "")

    return {
        "ok": True,
        "mode": "range_filtered_v2_shadow_only_observe",
        "candidate": "g3_range_filtered_volume5_v2",
        "auto_order_allowed": False,
        "formal_buy_signal": False,
        "order_path_enabled": False,
        "message": diagnosis,
        "summary": latest_summary or summary,
        "csv_summary": summary,
        "route_summary": route_summary,
        "candidates": candidates,
        "recent_candidates": recent_candidates,
        "isolation": isolation,
        "refresh": refresh_result,
        "artifacts": {
            "runtime_dir": _path_status(RANGE_FILTERED_RUNTIME_DIR),
            "report_dir": _path_status(RANGE_FILTERED_REPORT_DIR),
            "latest_summary": _path_status(latest_summary_path),
            "latest_candidates": _path_status(latest_candidates_path),
            "summary": _path_status(summary_path),
            "route_summary": _path_status(route_summary_path),
            "recent_candidates": _path_status(recent_path),
            "isolation": _path_status(isolation_path),
        },
        "guardrails": {
            "independent_from_g2": True,
            "uses_range_filtered_candidate_package_v2": True,
            "absorbs_g2_volume5_strong_market_idea": True,
            "formal_buy_signal": False,
            "order_path_enabled": False,
            "auto_order_allowed": False,
            "requires_execution_pressure_review_before_live_trading": True,
            "severe_haircut2_not_yet_passed": True,
        },
    }


@router.get("/route-execution-v3")
async def get_gen3_route_execution_v3(
    limit: int = Query(default=30, ge=1, le=200, description="Maximum closed trade rows returned."),
) -> dict[str, Any]:
    """
    Return the latest G3 V3 research candidate package.

    V3 is a research-only package. It inherits V2 selection and adds a route
    execution mandate: down/range same-day exits, strong route still stress-tested
    under next-open haircut.
    """

    summary_json_path = ROUTE_EXECUTION_V3_DIR / "summary.json"
    summary_csv_path = ROUTE_EXECUTION_V3_DIR / "g3_route_execution_mandate_candidate_summary.csv"
    windows_path = ROUTE_EXECUTION_V3_DIR / "g3_route_execution_mandate_candidate_windows.csv"
    routes_path = ROUTE_EXECUTION_V3_DIR / "g3_route_execution_mandate_candidate_route_attribution.csv"
    guard_audit_path = ROUTE_EXECUTION_V3_DIR / "g3_v3_sector_index_guard_audit.csv"
    guard_windows_path = ROUTE_EXECUTION_V3_DIR / "g3_v3_sector_index_guard_windows.csv"
    goal_path = ROUTE_EXECUTION_V3_DIR / "g3_route_execution_mandate_goal_audit.csv"
    visibility_path = ROUTE_EXECUTION_V3_DIR / "g3_route_execution_mandate_visibility_audit.csv"
    policy_path = ROUTE_EXECUTION_V3_DIR / "g3_route_execution_mandate_policy_defs.csv"
    trades_path = ROUTE_EXECUTION_V3_DIR / "g3_route_execution_mandate_candidate_closed_trades.csv"

    summary = _read_json(summary_json_path)
    if not summary:
        summary = {
            "status": "missing",
            "candidate": "g3_route_execution_mandate_v3",
            "goal_complete": False,
            "message": "未找到 G3 V3 路由级执行约束候选包，需要先运行 gen3_package_route_execution_mandate_v3.py。",
        }

    return {
        "ok": True,
        "mode": "research_only",
        "candidate": "g3_route_execution_mandate_v3",
        "auto_order_allowed": False,
        "formal_buy_signal": False,
        "order_path_enabled": False,
        "summary": summary,
        "profiles": _read_csv_records(summary_csv_path, limit=20),
        "windows": _read_csv_records(windows_path, limit=80),
        "route_attribution": _read_csv_records(routes_path, limit=80),
        "sector_index_guard_audit": _read_csv_records(guard_audit_path, limit=30),
        "sector_index_guard_windows": _read_csv_records(guard_windows_path, limit=80),
        "goal_audit": _read_csv_records(goal_path, limit=20),
        "visibility_audit": _read_csv_records(visibility_path, limit=30),
        "policy_defs": _read_csv_records(policy_path, limit=20),
        "closed_trades": _read_csv_records(trades_path, limit=limit),
        "artifacts": {
            "report_dir": _path_status(ROUTE_EXECUTION_V3_DIR),
            "summary_json": _path_status(summary_json_path),
            "summary_csv": _path_status(summary_csv_path),
            "windows": _path_status(windows_path),
            "route_attribution": _path_status(routes_path),
            "sector_index_guard_audit": _path_status(guard_audit_path),
            "sector_index_guard_windows": _path_status(guard_windows_path),
            "goal_audit": _path_status(goal_path),
            "visibility_audit": _path_status(visibility_path),
            "closed_trades": _path_status(trades_path),
        },
        "guardrails": {
            "independent_from_g2_runtime": True,
            "research_only": True,
            "formal_buy_signal": False,
            "order_path_enabled": False,
            "auto_order_allowed": False,
            "requires_same_day_fill_audit_before_shadow_payload": True,
        },
    }

    latest_summary_path = RANGE_FILTERED_RUNTIME_DIR / "latest_summary.json"
    latest_candidates_path = RANGE_FILTERED_RUNTIME_DIR / "latest_candidates.csv"
    summary_path = RANGE_FILTERED_REPORT_DIR / "g3_range_filtered_shadow_summary.csv"
    route_summary_path = RANGE_FILTERED_REPORT_DIR / "g3_range_filtered_shadow_route_summary.csv"
    recent_path = RANGE_FILTERED_REPORT_DIR / "g3_range_filtered_shadow_recent_candidates.csv"
    isolation_path = RANGE_FILTERED_REPORT_DIR / "g3_range_filtered_shadow_isolation_manifest.csv"

    latest_summary = _read_json(latest_summary_path)
    summary = _read_first_record(summary_path)
    route_summary = _read_csv_records(route_summary_path, limit=50)
    candidates = _read_csv_records(latest_candidates_path, limit=limit)
    recent_candidates = _read_csv_records(recent_path, limit=limit)
    isolation = _read_csv_records(isolation_path, limit=50)

    if not latest_summary and not summary:
        latest_summary = {
            "diagnosis_code": "NO_G3_RANGE_FILTERED_SHADOW_REPORT",
            "diagnosis": "未找到 G3 range-filtered V2 影子观察产物，需要先运行 V2 shadow-only 脚本。",
            "today_shadow_candidates": 0,
            "auto_order_allowed_rows": 0,
            "formal_buy_signal_rows": 0,
            "order_path_enabled_rows": 0,
        }

    diagnosis = latest_summary.get("diagnosis") or summary.get("diagnosis", "")

    return {
        "ok": True,
        "mode": "range_filtered_v2_shadow_only_observe",
        "candidate": "g3_range_filtered_volume5_v2",
        "auto_order_allowed": False,
        "formal_buy_signal": False,
        "order_path_enabled": False,
        "message": diagnosis,
        "summary": latest_summary or summary,
        "csv_summary": summary,
        "route_summary": route_summary,
        "candidates": candidates,
        "recent_candidates": recent_candidates,
        "isolation": isolation,
        "refresh": refresh_result,
        "artifacts": {
            "runtime_dir": _path_status(RANGE_FILTERED_RUNTIME_DIR),
            "report_dir": _path_status(RANGE_FILTERED_REPORT_DIR),
            "latest_summary": _path_status(latest_summary_path),
            "latest_candidates": _path_status(latest_candidates_path),
            "summary": _path_status(summary_path),
            "route_summary": _path_status(route_summary_path),
            "recent_candidates": _path_status(recent_path),
            "isolation": _path_status(isolation_path),
        },
        "guardrails": {
            "independent_from_g2": True,
            "uses_range_filtered_candidate_package_v2": True,
            "absorbs_g2_volume5_strong_market_idea": True,
            "formal_buy_signal": False,
            "order_path_enabled": False,
            "auto_order_allowed": False,
            "requires_execution_pressure_review_before_live_trading": True,
            "severe_haircut2_not_yet_passed": True,
        },
    }
