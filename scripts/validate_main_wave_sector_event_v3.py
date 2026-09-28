from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.research_main_wave_sector_score import (
    _add_sector_feature_frame,
    _add_stock_features,
    _build_sector_daily,
    _latest_trade_date,
    _load_daily,
    _load_members,
    _parse_levels,
    _score_between,
)
from scripts.validate_main_wave_sector_score_history import (
    _compound,
    _load_all_trade_dates,
    _parse_horizons,
    _pick_rebalance_dates,
)
from scripts.validate_main_wave_sector_start_v2 import _add_v2_features


OUT_DIR = ROOT / "reports" / "main_wave_sector_event_v3_validation"


def _safe_float(value: Any, digits: int = 6) -> Any:
    if value is None:
        return None
    try:
        x = float(value)
    except Exception:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, digits)


def _pct(value: Any) -> str:
    x = _safe_float(value)
    if x is None:
        return ""
    return f"{x:.2%}"


def _score_between_series(values: pd.Series, low: float, high: float) -> pd.Series:
    if high <= low:
        return pd.Series(0.0, index=values.index)
    x = pd.to_numeric(values, errors="coerce")
    return ((x - low) / (high - low)).clip(lower=0.0, upper=1.0).fillna(0.0)


def _forward_return_map(sector_daily: pd.DataFrame, horizons: list[int]) -> dict[tuple[int, str, str, int], float]:
    d = sector_daily.copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d = d.dropna(subset=["trade_date"]).sort_values(["level", "sector_code", "trade_date"]).copy()
    out: dict[tuple[int, str, str, int], float] = {}
    for (level, sector_code), g in d.groupby(["level", "sector_code"]):
        g = g.sort_values("trade_date").reset_index(drop=True)
        for idx, row in g.iterrows():
            signal_date = str(row["trade_date"])
            for horizon in horizons:
                fwd = g.iloc[idx + 1 : idx + 1 + horizon]["sector_ret"]
                if len(fwd) >= horizon:
                    out[(int(level), str(sector_code), signal_date, int(horizon))] = _compound(fwd)
    return out


def _event_candidates(day: pd.DataFrame, max_events_per_day: int, min_event_score: float) -> pd.DataFrame:
    d = day.copy()
    for col in [
        "market_ret20",
        "market_ma20_ratio",
        "market_rise_ratio",
        "pre_range_60_20",
        "breakout_from_pre_high",
        "ret5",
        "ret10",
        "ret20",
        "ret60",
        "rank_ret5_pct",
        "rank_ret10_pct",
        "rank_ret20_pct",
        "rank_ret20_prev10",
        "rank_ret20_lift10",
        "ma20_ratio",
        "ma20_ratio_lift10",
        "ma60_ratio",
        "rise_ratio",
        "strong3_ratio",
        "new_high60_ratio",
        "amount_ratio5_20",
        "amount_ratio20_60",
        "amount_accel5_20_vs20_60",
    ]:
        d[col] = pd.to_numeric(d.get(col), errors="coerce")

    market_ok = (
        (d["market_ret20"] >= -0.035)
        & (d["market_ma20_ratio"] >= 0.32)
        & (d["market_rise_ratio"] >= 0.42)
    )
    base_ok = (
        (d["pre_range_60_20"].between(0.06, 0.36))
        & (d["breakout_from_pre_high"].between(0.00, 0.16))
        & (d["ret60"].between(-0.12, 0.35))
    )
    jump_ok = (
        (d["rank_ret20_pct"] >= 0.68)
        & (d["rank_ret20_prev10"] <= 0.62)
        & (d["rank_ret20_lift10"] >= 0.12)
    ) | (
        (d["rank_ret5_pct"] >= 0.78)
        & (d["rank_ret10_pct"] >= 0.72)
        & (d["rank_ret20_lift10"] >= 0.08)
    )
    ignition_ok = (
        (d["ret5"].between(0.015, 0.12))
        & (d["ret10"].between(0.025, 0.18))
        & (d["amount_ratio5_20"].between(1.05, 2.30))
        & (d["amount_accel5_20_vs20_60"] >= 0.95)
    )
    breadth_ok = (
        (d["rise_ratio"] >= 0.52)
        & (d["ma20_ratio"] >= 0.45)
        & (d["ma20_ratio_lift10"] >= 0.05)
        & (d["strong3_ratio"].between(0.035, 0.22))
    )
    not_hot = (
        (d["ret20"] <= 0.26)
        & (d["ret60"] <= 0.45)
        & (d["new_high60_ratio"] <= 0.28)
        & (d["strong3_ratio"] <= 0.24)
        & (d["amount_ratio5_20"] <= 2.50)
    )

    picked = d[market_ok & base_ok & jump_ok & ignition_ok & breadth_ok & not_hot].copy()
    if picked.empty:
        return picked

    picked["event_score"] = (
        20.0 * _score_between_series(picked["rank_ret20_lift10"], 0.08, 0.35)
        + 15.0 * _score_between_series(picked["ma20_ratio_lift10"], 0.05, 0.30)
        + 15.0 * _score_between_series(picked["amount_ratio5_20"], 1.05, 1.90)
        + 15.0 * _score_between_series(picked["breakout_from_pre_high"], 0.00, 0.12)
        + 15.0 * _score_between_series(picked["rank_ret10_pct"], 0.70, 0.95)
        + 10.0 * _score_between_series(picked["rise_ratio"], 0.52, 0.75)
        + 10.0 * _score_between_series(picked["strong3_ratio"], 0.035, 0.18)
    )
    picked["event_type"] = "main_wave_event_v3"
    if min_event_score > 0:
        picked = picked[picked["event_score"] >= float(min_event_score)].copy()
    if picked.empty:
        return picked
    return picked.sort_values(["event_score", "rank_ret20_lift10"], ascending=False).head(max_events_per_day)


def _validate_events(
    sector_daily: pd.DataFrame,
    features: pd.DataFrame,
    event_dates: list[str],
    horizons: list[int],
    max_events_per_day: int,
    min_event_score: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    fwd_map = _forward_return_map(sector_daily, horizons)
    f = features.copy()
    f["trade_date_text"] = pd.to_datetime(f["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    event_rows: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    for signal_date in event_dates:
        day = f[f["trade_date_text"].eq(signal_date)].drop(columns=["trade_date_text"], errors="ignore").copy()
        if day.empty:
            daily_rows.append({"signal_date": signal_date, "event_count": 0})
            continue
        events = _event_candidates(day, max_events_per_day=max_events_per_day, min_event_score=min_event_score)
        daily_rows.append({"signal_date": signal_date, "event_count": int(len(events))})
        for row in events.itertuples(index=False):
            for horizon in horizons:
                ret = fwd_map.get((int(row.level), str(row.sector_code), signal_date, horizon))
                if ret is None:
                    continue
                event_rows.append(
                    {
                        "signal_date": signal_date,
                        "horizon": int(horizon),
                        "level": int(row.level),
                        "sector_code": str(row.sector_code),
                        "sector_name": str(row.sector_name),
                        "event_score": _safe_float(row.event_score),
                        "event_type": str(row.event_type),
                        "forward_ret": _safe_float(ret),
                        "ret5": _safe_float(row.ret5),
                        "ret10": _safe_float(row.ret10),
                        "ret20": _safe_float(row.ret20),
                        "rank_ret20_lift10": _safe_float(row.rank_ret20_lift10),
                        "ma20_ratio_lift10": _safe_float(row.ma20_ratio_lift10),
                        "pre_range_60_20": _safe_float(row.pre_range_60_20),
                        "amount_ratio5_20": _safe_float(row.amount_ratio5_20),
                        "strong3_ratio": _safe_float(row.strong3_ratio),
                    }
                )
    return pd.DataFrame(daily_rows), pd.DataFrame(event_rows)


def _summary(events: pd.DataFrame, all_dates: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if events.empty:
        return rows
    for horizon, g in events.groupby("horizon"):
        vals = pd.to_numeric(g["forward_ret"], errors="coerce").dropna()
        rows.append(
            {
                "horizon": int(horizon),
                "events": int(len(vals)),
                "event_dates": int(g["signal_date"].nunique()),
                "avg_forward": _safe_float(vals.mean()),
                "median_forward": _safe_float(vals.median()),
                "win_rate": _safe_float((vals > 0).mean()),
                "best": _safe_float(vals.max()),
                "worst": _safe_float(vals.min()),
            }
        )
    return rows


def _write_outputs(result: dict[str, Any], daily: pd.DataFrame, events: pd.DataFrame) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    daily_path = OUT_DIR / "event_days.csv"
    events_path = OUT_DIR / "events.csv"
    summary_path = OUT_DIR / "summary.json"
    report_path = OUT_DIR / "REPORT.md"
    daily.to_csv(daily_path, index=False, encoding="utf-8-sig")
    events.to_csv(events_path, index=False, encoding="utf-8-sig")
    payload = dict(result)
    payload["event_days_csv"] = str(daily_path)
    payload["events_csv"] = str(events_path)
    payload["report"] = str(report_path)
    summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 主升浪事件触发器 V3 验证",
        "",
        f"- 验证区间：`{result['start_date']}` 到 `{result['end_date']}`",
        f"- 层级：`{result['levels']}`",
        f"- 检查日期数：`{result['checked_dates']}`",
        f"- 触发日期数：`{result['event_dates']}`",
        f"- 触发事件数：`{result['event_count']}`",
        f"- 年化触发次数估算：`{result['annualized_event_dates']}` 个交易日触发",
        "- 口径：允许无信号，只在市场环境、蓄势、强度跃迁、扩散启动、量能确认和不过热同时满足时触发。",
        "",
        "## 汇总",
        "",
        "| 持有天数 | 事件数 | 触发日期数 | 平均收益 | 中位收益 | 胜率 | 最好 | 最差 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result["summary"]:
        lines.append(
            f"| {row['horizon']} | {row['events']} | {row['event_dates']} | {_pct(row['avg_forward'])} | "
            f"{_pct(row['median_forward'])} | {_pct(row['win_rate'])} | {_pct(row['best'])} | {_pct(row['worst'])} |"
        )
    lines.extend(
        [
            "",
            "## 判断规则",
            "",
            "- 如果触发频率接近每季度 1 次，且 20/40 日收益明显为正，才接近主升浪板块能力。",
            "- 如果触发频率很高，说明仍是普通热度筛选。",
            "- 如果 5/10 日有效但 20/40 日无效，说明只是短线启动，不是主升浪。",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(str(report_path))
    print(str(events_path))
    print(str(daily_path))


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate sparse main-wave sector event trigger V3.")
    parser.add_argument("--start-date", default="2025-01-01")
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--levels", default="2")
    parser.add_argument("--lookback-days", type=int, default=140)
    parser.add_argument("--step-days", type=int, default=5)
    parser.add_argument("--horizons", default="5,10,20,40")
    parser.add_argument("--max-events-per-day", type=int, default=2)
    parser.add_argument("--min-event-score", type=float, default=0.0)
    parser.add_argument("--min-members", type=int, default=5)
    args = parser.parse_args()

    args.end_date = args.end_date or _latest_trade_date()
    levels = _parse_levels(args.levels)
    horizons = _parse_horizons(args.horizons)
    trade_dates = _load_all_trade_dates(args.end_date)
    load_start, event_dates = _pick_rebalance_dates(
        trade_dates,
        args.start_date,
        args.end_date,
        args.lookback_days,
        args.step_days,
        max(horizons),
    )
    members = _load_members(levels, args.min_members)
    daily = _add_stock_features(_load_daily(load_start, args.end_date))
    sector_daily = _build_sector_daily(daily, members)
    features = _add_v2_features(_add_sector_feature_frame(sector_daily))
    event_days, events = _validate_events(
        sector_daily,
        features,
        event_dates,
        horizons,
        args.max_events_per_day,
        args.min_event_score,
    )
    event_date_count = int(event_days["event_count"].gt(0).sum()) if not event_days.empty else 0
    annualized = round(event_date_count / max(len(event_days), 1) * 244, 2)
    result = {
        "start_date": args.start_date,
        "end_date": args.end_date,
        "load_start": load_start,
        "levels": levels,
        "lookback_days": args.lookback_days,
        "step_days": args.step_days,
        "horizons": horizons,
        "max_events_per_day": args.max_events_per_day,
        "min_event_score": args.min_event_score,
        "min_members": args.min_members,
        "checked_dates": int(len(event_days)),
        "event_dates": event_date_count,
        "event_count": int(len(events) / max(len(horizons), 1)) if not events.empty else 0,
        "annualized_event_dates": annualized,
        "input_rows": {
            "members": int(len(members)),
            "daily": int(len(daily)),
            "sector_daily": int(len(sector_daily)),
            "features": int(len(features)),
        },
        "summary": _summary(events, event_days),
    }
    _write_outputs(result, event_days, events)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
