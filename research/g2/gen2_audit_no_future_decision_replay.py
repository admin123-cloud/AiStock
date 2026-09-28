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

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default, _load_trade_dates, _pct  # noqa: E402


DEFAULT_TIER_SIGNALS = (
    _report_path()
    / "gen2_preference_signal_tier_validation_tday_context"
    / "signals_with_preference_tiers.csv"
)
DEFAULT_PORTFOLIO_SUMMARY = _report_path() / "gen2_preference_tier_portfolio_backtest_tday_context" / "summary.csv"
DEFAULT_BACKTEST_DIR = _report_path() / "gen2_preference_tier_portfolio_backtest_tday_context" / "drop_c_risk" / "backtest"
DEFAULT_OUTPUT_DIR = _report_path() / "gen2_no_future_decision_replay_audit"

DECISION_INPUT_COLUMNS = [
    "overhead_pressure_score",
    "effective_pressure_score",
    "late_stage_risk_score",
    "wave_late_score",
    "downtrend_rebound_score",
    "volume_bear_distribution_score",
    "ma_chase_risk_score",
    "mom20",
    "volume_ratio",
    "preference_score_v2",
]
POST_EVAL_PREFIXES = ("post", "entry_fwd_ret", "entry_csi1000_fwd_ret", "entry_excess_vs_csi1000")


def _load_signals(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    d["code"] = d["code"].astype(str)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    if "intraday_normal_datetime" in d.columns:
        d["intraday_normal_datetime"] = pd.to_datetime(d["intraday_normal_datetime"], errors="coerce")
    for col in ["entry_price", "v4_rank", "v4_score", *DECISION_INPUT_COLUMNS]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["code", "entry_date", "confirm_datetime", "entry_price"]).reset_index(drop=True)


def _load_confirm_bars(signals: pd.DataFrame) -> pd.DataFrame:
    from utils.market_warehouse import clickhouse_client

    codes = sorted(signals["code"].dropna().astype(str).unique().tolist())
    if not codes:
        return pd.DataFrame()
    start_dt = pd.Timestamp(signals["confirm_datetime"].min()).strftime("%Y-%m-%d %H:%M:%S")
    end_dt = pd.Timestamp(signals["confirm_datetime"].max()).strftime("%Y-%m-%d %H:%M:%S")
    quoted = ", ".join(f"'{code}'" for code in codes)
    ch = clickhouse_client()
    bars = ch.query_df(
        f"""
        SELECT code, datetime, open, high, low, close, volume
        FROM kline_minute_30
        WHERE code IN ({quoted})
          AND datetime BETWEEN toDateTime('{start_dt}') AND toDateTime('{end_dt}')
        ORDER BY code, datetime
        """
    )
    if bars.empty:
        return bars
    bars["code"] = bars["code"].astype(str)
    bars["confirm_datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    for col in ["open", "high", "low", "close", "volume"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    return bars.dropna(subset=["code", "confirm_datetime", "close"]).reset_index(drop=True)


def _time_window_label(ts: pd.Timestamp) -> str:
    if pd.isna(ts):
        return "missing"
    hhmm = ts.strftime("%H:%M")
    if "09:30" <= hhmm <= "15:00":
        return "regular"
    return "outside_regular"


def _audit(signals: pd.DataFrame, start_date: str, end_date: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    trade_dates = set(_load_trade_dates(start_date, end_date))
    d = signals.copy()
    d["confirm_date"] = d["confirm_datetime"].dt.strftime("%Y-%m-%d")
    d["confirm_time_window"] = d["confirm_datetime"].map(_time_window_label)
    d["valid_trade_date"] = d["entry_date"].isin(trade_dates)
    d["confirm_same_as_entry_date"] = d["confirm_date"].eq(d["entry_date"])
    d["confirm_in_regular_time"] = d["confirm_time_window"].eq("regular")

    if "intraday_normal_datetime" in d.columns:
        d["intraday_normal_not_after_confirm"] = d["intraday_normal_datetime"].le(d["confirm_datetime"])
    else:
        d["intraday_normal_not_after_confirm"] = False

    bars = _load_confirm_bars(d)
    if bars.empty:
        d["confirm_bar_close"] = np.nan
        d["entry_price_matches_confirm_close"] = False
        d["entry_price_diff_pct"] = np.nan
    else:
        d = d.merge(
            bars[["code", "confirm_datetime", "close"]].rename(columns={"close": "confirm_bar_close"}),
            on=["code", "confirm_datetime"],
            how="left",
        )
        d["entry_price_diff_pct"] = d["entry_price"] / d["confirm_bar_close"] - 1.0
        d["entry_price_matches_confirm_close"] = d["entry_price_diff_pct"].abs().le(0.0005)

    d["decision_action"] = np.where(d["c_subtier"].eq("C-risk"), "reject", "allow")
    d["replay_order"] = d.sort_values(["entry_date", "confirm_datetime", "v4_rank", "v4_score", "code"], ascending=[True, True, True, False, True]).groupby("entry_date").cumcount() + 1

    post_cols = [col for col in d.columns if col.startswith(POST_EVAL_PREFIXES)]
    missing_inputs = [col for col in DECISION_INPUT_COLUMNS if col not in d.columns]
    checks = {
        "rows": int(len(d)),
        "allow_rows_drop_c_risk": int(d["decision_action"].eq("allow").sum()),
        "reject_rows_c_risk": int(d["decision_action"].eq("reject").sum()),
        "valid_trade_date_fail": int((~d["valid_trade_date"]).sum()),
        "confirm_date_fail": int((~d["confirm_same_as_entry_date"]).sum()),
        "confirm_time_fail": int((~d["confirm_in_regular_time"]).sum()),
        "intraday_normal_after_confirm_fail": int((~d["intraday_normal_not_after_confirm"]).sum()),
        "confirm_bar_missing": int(d["confirm_bar_close"].isna().sum()),
        "entry_price_mismatch": int((~d["entry_price_matches_confirm_close"]).sum()),
        "post_eval_columns_present_but_not_decision_inputs": post_cols,
        "decision_input_columns": DECISION_INPUT_COLUMNS,
        "missing_decision_inputs": missing_inputs,
    }
    return d, checks


def _load_portfolio_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    d = pd.read_csv(path)
    keep = d[d["key"].isin(["baseline_all", "drop_c_risk", "c_aggressive_only", "c_risk_only"])].copy()
    return keep.where(pd.notna(keep), None).to_dict("records")


def _build_daily_ledger(audited: pd.DataFrame, backtest_dir: Path) -> pd.DataFrame:
    allow = audited[audited["decision_action"].eq("allow")].copy()
    reject = audited[audited["decision_action"].eq("reject")].copy()
    rows = []
    all_dates = sorted(set(audited["entry_date"].dropna().astype(str)))
    trades_path = backtest_dir / "trades.csv"
    curve_path = backtest_dir / "equity_curve.csv"
    trades = pd.read_csv(trades_path) if trades_path.exists() else pd.DataFrame()
    curve = pd.read_csv(curve_path) if curve_path.exists() else pd.DataFrame()
    if not trades.empty:
        trades["buy_date"] = pd.to_datetime(trades["buy_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        trades["sell_date"] = pd.to_datetime(trades["sell_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if not curve.empty:
        curve["date"] = pd.to_datetime(curve["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        curve = curve.set_index("date")

    for date in all_dates:
        day_allow = allow[allow["entry_date"].eq(date)].sort_values("replay_order")
        day_reject = reject[reject["entry_date"].eq(date)].sort_values("replay_order")
        day_buys = trades[trades["buy_date"].eq(date)].copy() if not trades.empty else pd.DataFrame()
        bought_codes = ",".join(day_buys["code"].dropna().astype(str).drop_duplicates().tolist()) if not day_buys.empty else ""
        allowed_codes = ",".join(day_allow["code"].dropna().astype(str).head(5).tolist())
        rejected_codes = ",".join(day_reject["code"].dropna().astype(str).head(5).tolist())
        curve_row = curve.loc[date] if date in curve.index else None
        rows.append(
            {
                "date": date,
                "allow_signals": int(len(day_allow)),
                "reject_c_risk_signals": int(len(day_reject)),
                "executed_buy_legs": int(len(day_buys)),
                "allowed_top_codes": allowed_codes,
                "rejected_top_codes": rejected_codes,
                "executed_buy_codes": bought_codes,
                "holding_count_eod": None if curve_row is None else int(curve_row.get("holding_count", 0)),
                "strategy_equity_eod": None if curve_row is None else float(curve_row.get("strategy_equity", np.nan)),
                "drawdown_eod": None if curve_row is None else float(curve_row.get("drawdown", np.nan)),
            }
        )
    return pd.DataFrame(rows)


def _write_report(output_dir: Path, checks: dict[str, Any], portfolio_rows: list[dict[str, Any]], ledger: pd.DataFrame) -> None:
    lines = [
        "# G2 No-Future Decision Replay Audit",
        "",
        "Scope: audit the tday-context intraday NORMAL signal set and the first deployable decision candidate `drop_c_risk`.",
        "This report does not tune parameters. It checks whether the current decision objects are replay-safe enough for the next validation round.",
        "",
        "## Audit Checks",
        "",
        "| check | value |",
        "| --- | ---: |",
    ]
    for key in [
        "rows",
        "allow_rows_drop_c_risk",
        "reject_rows_c_risk",
        "valid_trade_date_fail",
        "confirm_date_fail",
        "confirm_time_fail",
        "intraday_normal_after_confirm_fail",
        "confirm_bar_missing",
        "entry_price_mismatch",
    ]:
        lines.append(f"| {key} | {checks.get(key)} |")

    lines.extend(
        [
            "",
            "## Decision Inputs",
            "",
            "The first decision candidate only uses entry-time/prior structural fields:",
            "",
            ", ".join(f"`{col}`" for col in checks["decision_input_columns"]),
            "",
            "Post-entry evaluation fields exist in the research file, but they are not decision inputs. They must stay out of live scoring.",
            "",
            f"Post-entry/evaluation columns present: {len(checks['post_eval_columns_present_but_not_decision_inputs'])}",
            "",
            "## Portfolio Reference",
            "",
            "| rule | signals | trades | total | excess_vs_csi1000 | max_dd | win |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in portfolio_rows:
        lines.append(
            f"| {row.get('key')} | {int(row.get('signal_count') or 0)} | {int(row.get('trade_count') or 0)} | "
            f"{_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} |"
        )

    if not ledger.empty:
        active_days = ledger[ledger["allow_signals"] > 0]
        buy_days = ledger[ledger["executed_buy_legs"] > 0]
        lines.extend(
            [
                "",
                "## Daily Replay Ledger",
                "",
                f"Signal days: {len(active_days)}",
                f"Buy days: {len(buy_days)}",
                f"Executed buy legs: {int(ledger['executed_buy_legs'].sum())}",
                "",
                "Daily ledger output: `daily_decision_ledger.csv`.",
            ]
        )

    lines.extend(
        [
            "",
            "## Stage Verdict",
            "",
            "The `drop_c_risk` decision set is suitable for the next validation step if all failure counts above are zero except normal floating-point entry-price tolerance noise.",
            "Next step: run a true daily replay that regenerates signals day by day instead of reading a prebuilt full-window signal file.",
        ]
    )
    (output_dir / "audit_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    signals = _load_signals(Path(args.tier_signals))
    audited, checks = _audit(signals, str(args.start_date), str(args.end_date))

    audited.to_csv(output_dir / "audited_signals.csv", index=False, encoding="utf-8-sig")
    audited[audited["decision_action"].eq("allow")].to_csv(output_dir / "drop_c_risk_allowed_signals.csv", index=False, encoding="utf-8-sig")
    audited[audited["decision_action"].eq("reject")].to_csv(output_dir / "c_risk_rejected_signals.csv", index=False, encoding="utf-8-sig")
    portfolio_rows = _load_portfolio_rows(Path(args.portfolio_summary))
    ledger = _build_daily_ledger(audited, Path(args.backtest_dir))
    ledger.to_csv(output_dir / "daily_decision_ledger.csv", index=False, encoding="utf-8-sig")
    _write_report(output_dir, checks, portfolio_rows, ledger)
    payload = {
        "schema_version": 1,
        "tier_signals": str(Path(args.tier_signals)),
        "portfolio_summary": str(Path(args.portfolio_summary)),
        "backtest_dir": str(Path(args.backtest_dir)),
        "start_date": str(args.start_date),
        "end_date": str(args.end_date),
        "checks": checks,
        "portfolio_rows": portfolio_rows,
        "outputs": {
            "report": "audit_report.md",
            "audited_signals": "audited_signals.csv",
            "allowed_signals": "drop_c_risk_allowed_signals.csv",
            "rejected_signals": "c_risk_rejected_signals.csv",
            "daily_ledger": "daily_decision_ledger.csv",
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit no-future readiness for G2 preference-tier decision replay.")
    parser.add_argument("--tier-signals", default=str(DEFAULT_TIER_SIGNALS))
    parser.add_argument("--portfolio-summary", default=str(DEFAULT_PORTFOLIO_SUMMARY))
    parser.add_argument("--backtest-dir", default=str(DEFAULT_BACKTEST_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
