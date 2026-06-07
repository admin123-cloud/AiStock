from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.gen2_strategy import GEN2_OPEN_RULE_V1  # noqa: E402
from scripts.gen2_backtest_open_v1_portfolio import _json_default  # noqa: E402
from scripts.gen2_filter_signals_by_intraday_normal import (  # noqa: E402
    DEFAULT_STATE_DAILY,
    _build_intraday_state,
    _filter_with_state,
)
from scripts.gen2_test_30m_fractal_restart import (  # noqa: E402
    DEFAULT_EVENT_DATASET,
    _add_search_end,
    _find_fractal_triggers,
    _load_contexts,
    _load_minute_bars,
    _load_trade_dates,
)


DEFAULT_REFERENCE = (
    ROOT
    / "reports"
    / "gen2_intraday_normal_signal_filters_tday_context"
    / "signals_intraday_normal_30m_before_confirm.parquet"
)
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "gen2_daily_signal_replay_repro"


def _load_reference(path: Path, start_date: str, end_date: str) -> pd.DataFrame:
    d = pd.read_parquet(path)
    rule = GEN2_OPEN_RULE_V1
    d = d[
        (d["pattern"].eq(rule["pattern"]))
        & (d["g2_open_state"].eq(rule["g2_open_state"]))
        & (d["trigger_type"].eq(rule["trigger_type"]))
    ].copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    d = d[(d["entry_date"] >= start_date) & (d["entry_date"] <= end_date)].copy()
    return d.dropna(subset=["entry_date", "code", "confirm_datetime"]).reset_index(drop=True)


def _signal_key(df: pd.DataFrame) -> set[str]:
    if df.empty:
        return set()
    return set(
        df["code"].astype(str)
        + "|"
        + pd.to_datetime(df["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
        + "|"
        + df["pattern"].astype(str)
        + "|"
        + df["trigger_type"].astype(str)
    )


def _replay_one_day(
    target_date: str,
    trade_dates: list[str],
    event_dataset: Path,
    state_daily: Path,
    search_days: int,
    entry_start_lag_days: int,
    allow_same_day_context: bool,
) -> pd.DataFrame:
    idx = trade_dates.index(target_date)
    context_start_idx = max(0, idx - int(search_days))
    context_end_idx = max(0, idx - int(entry_start_lag_days))
    if context_end_idx < context_start_idx:
        return pd.DataFrame()
    context_start = trade_dates[context_start_idx]
    context_end = trade_dates[context_end_idx]

    contexts = _load_contexts(event_dataset, state_daily, context_start, context_end)
    contexts = _add_search_end(contexts, trade_dates, search_days, entry_start_lag_days=entry_start_lag_days)
    context_date_mask = contexts["trade_date"] <= target_date if allow_same_day_context else contexts["trade_date"] < target_date
    contexts = contexts[(contexts["entry_start_date"] <= target_date) & (contexts["search_end_date"] >= target_date) & context_date_mask].copy()
    if contexts.empty:
        return pd.DataFrame()

    codes = contexts["code"].dropna().astype(str).unique().tolist()
    minute_bars = _load_minute_bars(codes, context_start, target_date)
    triggers = _find_fractal_triggers(contexts, minute_bars)
    if triggers.empty:
        return triggers
    triggers["entry_date"] = pd.to_datetime(triggers["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    triggers = triggers[triggers["entry_date"].eq(target_date)].copy()
    rule = GEN2_OPEN_RULE_V1
    triggers = triggers[
        (triggers["pattern"].eq(rule["pattern"]))
        & (triggers["trigger_type"].eq(rule["trigger_type"]))
    ].copy()
    if triggers.empty:
        return triggers

    state = _build_intraday_state(triggers, state_daily, 30)
    filtered = _filter_with_state(triggers, state, "before_confirm")
    if filtered.empty:
        return filtered
    return filtered[
        (filtered["pattern"].eq(rule["pattern"]))
        & (filtered["g2_open_state"].eq(rule["g2_open_state"]))
        & (filtered["trigger_type"].eq(rule["trigger_type"]))
    ].copy()


def _write_report(output_dir: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "# G2 Daily Signal Replay Reproducibility",
        "",
        "Goal: regenerate each signal day using only prior daily context plus the target day's 30m bars, then compare with the full-window reference signal file.",
        "",
        "| metric | value |",
        "| --- | ---: |",
        f"| checked_days | {summary['checked_days']} |",
        f"| matched_days | {summary['matched_days']} |",
        f"| mismatch_days | {summary['mismatch_days']} |",
        f"| reference_signals | {summary['reference_signals']} |",
        f"| replay_signals | {summary['replay_signals']} |",
        f"| missing_signals | {summary['missing_signals']} |",
        f"| extra_signals | {summary['extra_signals']} |",
        "",
        "## Mismatches",
        "",
        "| date | reference | replay | missing | extra |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    mismatches = [row for row in rows if row["missing"] or row["extra"] or row["reference_count"] != row["replay_count"]]
    if not mismatches:
        lines.append("| none | 0 | 0 | 0 | 0 |")
    else:
        for row in mismatches[:50]:
            lines.append(
                f"| {row['date']} | {row['reference_count']} | {row['replay_count']} | {row['missing']} | {row['extra']} |"
            )
    (output_dir / "replay_repro_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ref = _load_reference(Path(args.reference), str(args.start_date), str(args.end_date))
    trade_dates = _load_trade_dates(str(args.start_date), str(args.end_date))
    signal_dates = sorted(ref["entry_date"].dropna().astype(str).unique().tolist())
    if args.max_days:
        signal_dates = signal_dates[: int(args.max_days)]

    rows: list[dict[str, Any]] = []
    replay_parts: list[pd.DataFrame] = []
    for date in signal_dates:
        ref_day = ref[ref["entry_date"].eq(date)].copy()
        replay_day = _replay_one_day(
            target_date=date,
            trade_dates=trade_dates,
            event_dataset=Path(args.event_dataset),
            state_daily=Path(args.state_daily),
            search_days=int(args.search_days),
            entry_start_lag_days=int(args.entry_start_lag_days),
            allow_same_day_context=bool(args.allow_same_day_context),
        )
        if not replay_day.empty:
            replay_parts.append(replay_day)
        ref_keys = _signal_key(ref_day)
        replay_keys = _signal_key(replay_day)
        rows.append(
            {
                "date": date,
                "reference_count": int(len(ref_keys)),
                "replay_count": int(len(replay_keys)),
                "missing": int(len(ref_keys - replay_keys)),
                "extra": int(len(replay_keys - ref_keys)),
                "missing_keys": ";".join(sorted(ref_keys - replay_keys)[:20]),
                "extra_keys": ";".join(sorted(replay_keys - ref_keys)[:20]),
            }
        )

    replay = pd.concat(replay_parts, ignore_index=True) if replay_parts else pd.DataFrame()
    detail = pd.DataFrame(rows)
    detail.to_csv(output_dir / "daily_replay_compare.csv", index=False, encoding="utf-8-sig")
    if not replay.empty:
        replay.to_parquet(output_dir / "daily_replayed_signals.parquet", index=False)
        replay.to_csv(output_dir / "daily_replayed_signals.csv", index=False, encoding="utf-8-sig")

    summary = {
        "schema_version": 1,
        "reference": str(Path(args.reference)),
        "event_dataset": str(Path(args.event_dataset)),
        "state_daily": str(Path(args.state_daily)),
        "start_date": str(args.start_date),
        "end_date": str(args.end_date),
        "search_days": int(args.search_days),
        "entry_start_lag_days": int(args.entry_start_lag_days),
        "allow_same_day_context": bool(args.allow_same_day_context),
        "checked_days": int(len(detail)),
        "matched_days": int(((detail["missing"] == 0) & (detail["extra"] == 0) & (detail["reference_count"] == detail["replay_count"])).sum()) if not detail.empty else 0,
        "mismatch_days": int(((detail["missing"] != 0) | (detail["extra"] != 0) | (detail["reference_count"] != detail["replay_count"])).sum()) if not detail.empty else 0,
        "reference_signals": int(detail["reference_count"].sum()) if not detail.empty else 0,
        "replay_signals": int(detail["replay_count"].sum()) if not detail.empty else 0,
        "missing_signals": int(detail["missing"].sum()) if not detail.empty else 0,
        "extra_signals": int(detail["extra"].sum()) if not detail.empty else 0,
        "outputs": {
            "report": "replay_repro_report.md",
            "daily_compare": "daily_replay_compare.csv",
            "daily_replayed_signals": "daily_replayed_signals.parquet",
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(output_dir, summary, rows)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay G2 tday-context intraday NORMAL signals day by day and compare with full-window reference.")
    parser.add_argument("--reference", default=str(DEFAULT_REFERENCE))
    parser.add_argument("--event-dataset", default=str(DEFAULT_EVENT_DATASET))
    parser.add_argument("--state-daily", default=str(DEFAULT_STATE_DAILY))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--search-days", type=int, default=2)
    parser.add_argument("--entry-start-lag-days", type=int, default=1)
    parser.add_argument("--allow-same-day-context", action="store_true")
    parser.add_argument("--max-days", type=int, default=0)
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
