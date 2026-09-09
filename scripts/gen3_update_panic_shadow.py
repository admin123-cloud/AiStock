from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[1]))

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_audit_panic_failure_env import _add_labels
from scripts.gen3_audit_panic_systemic_pause import _add_daily_context, _policy_masks, _select
from scripts.gen3_build_four_path_candidates import (
    _add_index_features,
    _add_stock_features,
    _build_market_context,
    _json_default,
    _load_index_daily,
    _load_stock_daily,
)
from scripts.gen3_research_panic_v2 import _select_variants
from scripts.gen3_validate_intraday_confirm import _confirm_signals, _load_minute_bars
from utils.market_warehouse import clickhouse_query_df


DEFAULT_OUTPUT_DIR = ROOT / "reports" / "gen3_panic_shadow"
DEFAULT_STATE_DIR = ROOT / "data" / "runtime" / "gen3_panic_shadow"
INDEX_CODE = "999999.SH"
CURRENT_INDEX_CODE_FALLBACK = "000001.SH"


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _date_text(value: Any) -> str:
    ts = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(ts) else ts.strftime("%Y-%m-%d")


from research.common.reporting import percent_text as _pct


def _latest_index_trade_date() -> str:
    dates: list[pd.Timestamp] = []
    for index_code in [INDEX_CODE, CURRENT_INDEX_CODE_FALLBACK]:
        d = clickhouse_query_df(
            """
            SELECT max(trade_date) AS trade_date
            FROM kline_daily
            WHERE code = %(index_code)s
            """,
            {"index_code": index_code},
        )
        if not d.empty and not pd.isna(d.iloc[0]["trade_date"]):
            dates.append(pd.Timestamp(d.iloc[0]["trade_date"]))
    return max(dates).strftime("%Y-%m-%d") if dates else ""


def _prev_index_trade_date(entry_date: str) -> str:
    dates: list[pd.Timestamp] = []
    for index_code in [INDEX_CODE, CURRENT_INDEX_CODE_FALLBACK]:
        d = clickhouse_query_df(
            """
            SELECT max(trade_date) AS prev_trade_date
            FROM kline_daily
            WHERE code = %(index_code)s
              AND trade_date < toDate(%(entry_date)s)
            """,
            {"index_code": index_code, "entry_date": entry_date},
        )
        if not d.empty and not pd.isna(d.iloc[0]["prev_trade_date"]):
            dates.append(pd.Timestamp(d.iloc[0]["prev_trade_date"]))
    return max(dates).strftime("%Y-%m-%d") if dates else ""


def _next_index_trade_date(trade_date: str) -> str:
    dates: list[pd.Timestamp] = []
    for index_code in [INDEX_CODE, CURRENT_INDEX_CODE_FALLBACK]:
        d = clickhouse_query_df(
            f"""
            SELECT min(trade_date) AS next_trade_date
            FROM kline_daily
            WHERE code = {_sql_literal(index_code)}
              AND trade_date > toDate({_sql_literal(trade_date)})
            """,
        )
        if not d.empty and not pd.isna(d.iloc[0]["next_trade_date"]):
            dates.append(pd.Timestamp(d.iloc[0]["next_trade_date"]))
    return min(dates).strftime("%Y-%m-%d") if dates else ""


def _load_index_daily_current_context(start_date: str, end_date: str) -> pd.DataFrame:
    last_error = ""
    for index_code in [INDEX_CODE, CURRENT_INDEX_CODE_FALLBACK]:
        try:
            df = clickhouse_query_df(
                """
                SELECT trade_date, open, high, low, close, volume, amount, turnover_rate
                FROM kline_daily
                WHERE code = %(index_code)s
                  AND trade_date BETWEEN subtractDays(toDate(%(start_date)s), 260) AND toDate(%(end_date)s)
                ORDER BY trade_date
                """,
                {"index_code": index_code, "start_date": start_date, "end_date": end_date},
            )
            if df.empty:
                last_error = f"No index daily data loaded for {index_code}"
                continue
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
            for col in ["open", "high", "low", "close", "volume", "amount", "turnover_rate"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            out = df.dropna(subset=["trade_date", "close"]).sort_values("trade_date").reset_index(drop=True)
            if not out.empty:
                return out
            last_error = f"No valid index daily rows for {index_code}"
        except Exception as exc:
            last_error = str(exc)
    raise RuntimeError(last_error or "No index daily data loaded")


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default.copy()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default.copy()
    except Exception:
        return default.copy()


def _build_daily_candidates(entry_date: str, top_n: int, min_amount20: float, max_codes: int) -> tuple[pd.DataFrame, dict[str, Any]]:
    trade_date = _prev_index_trade_date(entry_date)
    if not trade_date:
        return pd.DataFrame(), {"ok": False, "reason": "no_previous_trade_date", "entry_date": entry_date}

    stocks = _load_stock_daily(trade_date, trade_date, max_codes=max_codes)
    features = _add_stock_features(stocks)
    index = _load_index_daily_current_context(trade_date, trade_date)
    market_context = _build_market_context(features, _add_index_features(index), min_amount20=min_amount20)
    candidates = _select_variants(features, market_context, top_n=top_n, min_amount20=min_amount20)
    if candidates.empty:
        return candidates, {
            "ok": True,
            "trade_date": trade_date,
            "entry_date": entry_date,
            "daily_candidates": 0,
            "final_candidates": 0,
        }

    candidates["entry_date"] = entry_date
    candidates["trade_date"] = trade_date
    labeled = _add_labels(candidates)
    labeled = _add_daily_context(labeled)
    final_mask = _policy_masks(labeled)["pause_weak_no_capitulation"]
    final = labeled[final_mask].copy()
    final = _select(final, "pause_weak_no_capitulation", max_per_day=5)
    final["shadow_status"] = "daily_candidate"
    final["shadow_policy"] = "panic_v1_slot5_m30_close5_shadow"
    final["shadow_trade_date"] = trade_date
    return final.reset_index(drop=True), {
        "ok": True,
        "trade_date": trade_date,
        "entry_date": entry_date,
        "daily_candidates": int(len(labeled)),
        "final_candidates": int(len(final)),
        "max_codes": int(max_codes or 0),
        "min_amount20": float(min_amount20),
    }


def _attach_intraday_confirmation(candidates: pd.DataFrame, period: int) -> tuple[pd.DataFrame, dict[str, Any]]:
    if candidates.empty:
        return candidates.copy(), {"intraday_rows": 0, "confirmed": 0, "status": "no_candidate"}
    bars = _load_minute_bars(candidates, period)
    if bars.empty:
        out = candidates.copy()
        out["shadow_status"] = "daily_candidate_no_intraday_data"
        return out, {"intraday_rows": 0, "confirmed": 0, "status": "no_intraday_data"}

    confirmed = _confirm_signals(candidates, bars)
    key_cols = ["entry_date", "code", "g3_chain"]
    if confirmed.empty:
        out = candidates.copy()
        out["shadow_status"] = "daily_candidate_wait_confirm"
        return out, {"intraday_rows": int(len(bars)), "confirmed": 0, "status": "no_confirm"}

    confirm_cols = [
        "entry_date",
        "code",
        "g3_chain",
        "confirm_datetime",
        "entry_price",
        "confirm_rule",
        "bar_time",
        "bar_close_pos",
        "bar_ret",
        "amount_ratio3",
    ]
    out = candidates.merge(confirmed[[c for c in confirm_cols if c in confirmed.columns]], on=key_cols, how="left")
    out["shadow_status"] = out["confirm_datetime"].notna().map(lambda v: "intraday_confirmed" if v else "daily_candidate_wait_confirm")
    return out, {"intraday_rows": int(len(bars)), "confirmed": int(out["confirm_datetime"].notna().sum()), "status": "ok"}


def _append_ledger(ledger_path: Path, rows: pd.DataFrame) -> None:
    if rows.empty:
        return
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    out = rows.copy()
    out["recorded_at"] = pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S%z")
    if ledger_path.exists():
        old = pd.read_csv(ledger_path)
        all_rows = pd.concat([old, out], ignore_index=True)
        dedupe_cols = [c for c in ["entry_date", "code", "g3_chain", "shadow_status", "confirm_datetime"] if c in all_rows.columns]
        if dedupe_cols:
            all_rows = all_rows.drop_duplicates(subset=dedupe_cols, keep="last")
    else:
        all_rows = out
    all_rows.to_csv(ledger_path, index=False, encoding="utf-8-sig")


def _write_report(out_dir: Path, summary: dict[str, Any], rows: pd.DataFrame) -> None:
    display_cols = [
        "entry_date",
        "code",
        "name",
        "shadow_status",
        "confirm_datetime",
        "entry_price",
        "candidate_score",
        "chain_rank",
        "market_style",
        "g3_position_guard",
        "g3_repair_env_label",
        "big_down_rate",
        "range_pos60",
        "amount_ratio20",
    ]
    show = rows[[c for c in display_cols if c in rows.columns]].copy() if not rows.empty else pd.DataFrame()
    for col in ["big_down_rate", "range_pos60", "amount_ratio20"]:
        if col in show.columns:
            show[col] = show[col].map(_pct)

    lines = [
        "# G3 Panic 影子盘更新",
        "",
        "## 摘要",
        "",
        f"- 入场日：`{summary.get('entry_date', '')}`",
        f"- 日线信号日：`{summary.get('trade_date', '')}`",
        f"- 日线候选：`{summary.get('daily_candidates', 0)}`",
        f"- 最终候选：`{summary.get('final_candidates', 0)}`",
        f"- 30m 确认：`{summary.get('confirmed', 0)}`",
        f"- 状态：`{summary.get('status', '')}`",
        "",
        "## 影子候选",
        "",
    ]
    if show.empty:
        lines.append("本次没有 G3 Panic V1 影子候选。")
    else:
        lines.append(show.to_markdown(index=False))
    lines += [
        "",
        "## 纪律",
        "",
        "- 这是影子盘记录，不是正式买点，不自动下单。",
        "- `daily_candidate` 只代表前一交易日收盘后候选成立；只有 `intraday_confirmed` 才代表入场日 30m 可见确认成立。",
        "- 后续至少积累一批真实影子样本后，再评估是否进入小仓实盘。",
    ]
    out_dir.joinpath("shadow_update_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Update independent G3 panic shadow ledger.")
    parser.add_argument("--entry-date", default="", help="Entry date to monitor. Defaults to next index trading day after latest daily date when available.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--period", type=int, default=30, choices=[15, 30])
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--min-amount20", type=float, default=30000.0)
    parser.add_argument("--max-codes", type=int, default=0)
    args = parser.parse_args()

    latest_daily = _latest_index_trade_date()
    entry_date = args.entry_date.strip()
    if not entry_date:
        entry_date = latest_daily
    entry_date = _date_text(entry_date)
    if not entry_date:
        raise RuntimeError("Cannot resolve entry date.")

    out_dir = Path(args.output_dir) / "live_updates" / entry_date
    out_dir.mkdir(parents=True, exist_ok=True)
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    ledger_path = state_dir / "shadow_ledger.csv"

    candidates, daily_summary = _build_daily_candidates(
        entry_date=entry_date,
        top_n=int(args.top_n),
        min_amount20=float(args.min_amount20),
        max_codes=int(args.max_codes or 0),
    )
    rows, intra_summary = _attach_intraday_confirmation(candidates, int(args.period))
    rows.to_csv(out_dir / "shadow_candidates.csv", index=False, encoding="utf-8-sig")
    if not rows.empty:
        rows.to_parquet(out_dir / "shadow_candidates.parquet", index=False)
    _append_ledger(ledger_path, rows)

    summary = {
        **daily_summary,
        **intra_summary,
        "entry_date": entry_date,
        "latest_daily": latest_daily,
        "output_dir": str(out_dir),
        "state_dir": str(state_dir),
        "ledger_path": str(ledger_path),
        "g2_runtime_touched": False,
        "live_order_enabled": False,
    }
    state = _load_json(state_path, {"updates": []})
    updates = state.get("updates") if isinstance(state.get("updates"), list) else []
    updates.append(summary)
    state["updates"] = updates[-60:]
    state["latest"] = summary
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(out_dir, summary, rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
