from __future__ import annotations

import json
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
from scripts.gen2_compare_prev_low_exit_fills import _load_minute_bars  # noqa: E402
from scripts.gen2_update_live_shadow import _attach_g2_open_state, _load_events, _load_trade_dates  # noqa: E402


OFFICIAL_SOURCE = ROOT / "reports" / "gen2_v2_complete_strategy" / "sources" / "g2_v2_complete.parquet"
EVENT_DATASET = ROOT / "reports" / "gen2_event_study_full" / "v4_event_dataset.parquet"
STATE_DAILY = ROOT / "reports" / "gen2_open_state_research_full" / "g2_open_state_daily.csv"
OUT = ROOT / "reports" / "gen2_v2_b_class_small_probe"
START_DATE = "2024-07-09"
END_DATE = "2026-05-28"


VARIANTS = [
    {"name": "base_official", "b_filter": "none", "note": "正式 A 类信号，不加入 B 类。"},
    {"name": "b_entry50_w25", "b_filter": "entry50", "note": "B类：D-1 entry_pass 且 V4 排名 <=50，小仓 25%。"},
    {"name": "b_entry100_w25", "b_filter": "entry100", "note": "B类：D-1 entry_pass 且 V4 排名 <=100，小仓 25%。"},
    {"name": "b_pool50_w25", "b_filter": "pool50", "note": "B类：D-1 在评分池且 V4 排名 <=50，小仓 25%。"},
    {"name": "b_pool100_w25", "b_filter": "pool100", "note": "B类：D-1 在评分池且 V4 排名 <=100，小仓 25%。"},
]


def _next_date_map(trade_dates: list[str]) -> dict[str, str]:
    return {trade_dates[i]: trade_dates[i + 1] for i in range(len(trade_dates) - 1)}


def _load_bars_for_entries(codes: list[str], start_date: str, end_date: str) -> pd.DataFrame:
    bars = _load_minute_bars(codes, start_date, end_date, 30)
    if bars.empty:
        return bars
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    bars["entry_date"] = bars["datetime"].dt.strftime("%Y-%m-%d")
    bars["bar_time"] = bars["datetime"].dt.strftime("%H:%M:%S")
    bars = bars[bars["bar_time"].eq("10:30:00")].copy()
    keep = ["code", "entry_date", "datetime", "close", "amount"]
    return bars[[col for col in keep if col in bars.columns]].dropna(subset=["code", "entry_date", "close"])


def _build_b_candidates(filter_name: str) -> pd.DataFrame:
    if filter_name == "none":
        return pd.DataFrame()
    trade_dates = _load_trade_dates(START_DATE, END_DATE)
    next_map = _next_date_map(trade_dates)
    events = _load_events(EVENT_DATASET, START_DATE, END_DATE)
    if events.empty:
        return events
    events["trade_date"] = pd.to_datetime(events["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    events["entry_date"] = events["trade_date"].map(next_map)
    events = events.dropna(subset=["entry_date"]).copy()
    events = events[(events["entry_date"] >= START_DATE) & (events["entry_date"] <= END_DATE)].copy()
    events = _attach_g2_open_state(events, STATE_DAILY, "entry_date")
    events["v4_rank"] = pd.to_numeric(events["v4_rank"], errors="coerce").fillna(9999).astype(int)
    events["v4_score"] = pd.to_numeric(events["v4_score"], errors="coerce").fillna(0.0)
    for col in ["mom5", "mom10", "mom20", "vol_ratio", "vol10"]:
        events[col] = pd.to_numeric(events.get(col), errors="coerce")
    events["entry_pass"] = events.get("entry_pass", False).fillna(False).astype(bool)
    events["in_score_pool"] = events.get("in_score_pool", False).fillna(False).astype(bool)

    base = (
        events["g2_open_state"].isin(("NORMAL", "AGGRESSIVE"))
        & events["mom10"].ge(0.02)
        & events["mom20"].ge(0.03)
        & events["mom5"].le(0.14)
        & events["vol_ratio"].le(3.0)
        & events["vol10"].le(0.10)
    )
    if filter_name == "entry50":
        mask = base & events["entry_pass"] & events["v4_rank"].le(50)
    elif filter_name == "entry100":
        mask = base & events["entry_pass"] & events["v4_rank"].le(100)
    elif filter_name == "pool50":
        mask = base & events["in_score_pool"] & events["v4_rank"].le(50)
    elif filter_name == "pool100":
        mask = base & events["in_score_pool"] & events["v4_rank"].le(100)
    else:
        raise ValueError(f"Unknown B filter: {filter_name}")

    b = events[mask].copy()
    if b.empty:
        return b
    codes = b["code"].dropna().astype(str).unique().tolist()
    bars = _load_bars_for_entries(codes, START_DATE, END_DATE)
    if bars.empty:
        return pd.DataFrame()
    b = b.merge(bars, on=["code", "entry_date"], how="inner")
    if b.empty:
        return b
    b["confirm_datetime"] = pd.to_datetime(b["datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    b["entry_price"] = pd.to_numeric(b["close_y"] if "close_y" in b.columns else b["close"], errors="coerce")
    b["confirm_amount"] = pd.to_numeric(b.get("amount"), errors="coerce")
    b["confirm_amount_ma5_prev"] = np.nan
    b["source_family"] = "b_class_v4"
    b["signal_family"] = filter_name
    b["pattern"] = "pullback_restart_rank100"
    b["trigger_type"] = "bottom_fractal_break_high_vol"
    b["g2_v2_family_priority"] = 2
    b["g2_v2_buy_logic"] = f"{filter_name}_small_position"
    b["alpha191_position_weight"] = 0.25
    b["v4_rank_raw"] = b["v4_rank"]
    b["v4_score_raw"] = b["v4_score"]
    keep = [
        "trade_date",
        "entry_date",
        "code",
        "name",
        "v4_rank",
        "v4_score",
        "entry_price",
        "confirm_datetime",
        "confirm_amount",
        "confirm_amount_ma5_prev",
        "source_family",
        "signal_family",
        "pattern",
        "trigger_type",
        "g2_v2_family_priority",
        "g2_v2_buy_logic",
        "alpha191_position_weight",
        "v4_rank_raw",
        "v4_score_raw",
        "g2_open_state",
    ]
    return b[[col for col in keep if col in b.columns]].dropna(subset=["entry_price", "confirm_datetime"]).copy()


def _source_for_variant(spec: dict[str, Any]) -> Path:
    source_dir = OUT / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    official = pd.read_parquet(OFFICIAL_SOURCE)
    official["entry_date"] = pd.to_datetime(official["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    official["confirm_datetime"] = pd.to_datetime(official["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    official["alpha191_position_weight"] = 1.0
    if "g2_v2_family_priority" not in official.columns:
        official["g2_v2_family_priority"] = 0
    b = _build_b_candidates(str(spec["b_filter"]))
    if not b.empty:
        official_keys = set(zip(official["entry_date"].astype(str), official["code"].astype(str)))
        keys = list(zip(b["entry_date"].astype(str), b["code"].astype(str)))
        b = b[[key not in official_keys for key in keys]].copy()
    source = pd.concat([official, b], ignore_index=True, sort=False)
    source["v4_rank"] = pd.to_numeric(source["v4_rank"], errors="coerce").fillna(9999).astype(int)
    source["v4_score"] = pd.to_numeric(source["v4_score"], errors="coerce").fillna(0.0)
    source["g2_v2_family_priority"] = pd.to_numeric(source["g2_v2_family_priority"], errors="coerce").fillna(9).astype(int)
    source = source.sort_values(
        ["entry_date", "g2_v2_family_priority", "v4_score", "v4_rank", "confirm_datetime", "code"],
        ascending=[True, True, False, True, True, True],
    ).reset_index(drop=True)
    path = source_dir / f"{spec['name']}.parquet"
    source.to_parquet(path, index=False)
    source.to_csv(path.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    (source_dir / f"{spec['name']}_counts.json").write_text(
        json.dumps(
            {
                "variant": spec["name"],
                "official_rows": int(len(official)),
                "b_rows": int(len(b)) if not b.empty else 0,
                "total_rows": int(len(source)),
            },
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        ),
        encoding="utf-8",
    )
    return path


def _lot_summary(run_dir: Path) -> dict[str, Any]:
    trades_path = run_dir / "trades.csv"
    curve_path = run_dir / "equity_curve.csv"
    out: dict[str, Any] = {}
    if trades_path.exists():
        trades = pd.read_csv(trades_path)
        if not trades.empty:
            lot = trades.groupby(["buy_date", "code", "name"], dropna=False).agg(pnl=("pnl", "sum"), ret=("return", "sum"))
            out.update(
                {
                    "lot_count": int(len(lot)),
                    "lot_win_rate": float((lot["pnl"] > 0).mean()) if len(lot) else None,
                    "lot_avg_pnl": float(lot["pnl"].mean()) if len(lot) else None,
                }
            )
    if curve_path.exists():
        curve = pd.read_csv(curve_path)
        if not curve.empty:
            equity = pd.to_numeric(curve.get("equity"), errors="coerce")
            market = pd.to_numeric(curve.get("market_value"), errors="coerce")
            exposure = (market / equity).replace([np.inf, -np.inf], np.nan).fillna(0.0)
            out["avg_exposure"] = float(exposure.mean())
            out["invested_days_ratio"] = float((exposure > 0).mean())
    return out


def _run_variant(spec: dict[str, Any]) -> dict[str, Any]:
    source = _source_for_variant(spec)
    run_dir = OUT / "runs" / str(spec["name"])
    summary = _run_dynamic(source, run_dir, "stop_cd3_skip", START_DATE, END_DATE, sort_mode="g2_v2")
    counts_path = OUT / "sources" / f"{spec['name']}_counts.json"
    counts = json.loads(counts_path.read_text(encoding="utf-8")) if counts_path.exists() else {}
    return {**summary, **_lot_summary(run_dir), **counts, "variant": spec["name"], "note": spec["note"], "run_dir": str(run_dir)}


def _write_report(rows: list[dict[str, Any]]) -> None:
    rows = sorted(rows, key=lambda r: (float(r.get("total_return") or 0.0), float(r.get("max_drawdown") or -9.0)), reverse=True)
    lines = [
        "# G2 B类小仓探索",
        "",
        "本报告固定正式 A 类 `g2_v2_complete` 信号，并把若干 D-1 V4 强候选作为 B 类小仓加入。B 类默认 `alpha191_position_weight=0.25`，排序优先级低于 A 类。",
        "",
        "| 方案 | B类样本 | 交易 | 批次 | 平均仓位 | 总收益 | 超额 | 最大回撤 | 胜率 | 批次胜率 | 说明 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['variant']} | {row.get('b_rows', 0)} | {row.get('trade_count', '')} | {row.get('lot_count', '')} | "
            f"{_pct(row.get('avg_exposure'))} | {_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | "
            f"{_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {_pct(row.get('lot_win_rate'))} | {row.get('note', '')} |"
        )
    (OUT / "b_class_small_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = [_run_variant(spec) for spec in VARIANTS]
    df = pd.DataFrame(rows).sort_values(["total_return", "max_drawdown"], ascending=[False, False])
    df.to_csv(OUT / "summary.csv", index=False, encoding="utf-8-sig")
    (OUT / "summary.json").write_text(
        json.dumps({"schema_version": 1, "rows": df.where(pd.notna(df), None).to_dict("records")}, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    _write_report(rows)
    print(json.dumps({"output_dir": str(OUT), "rows": len(rows)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
