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
    _score_rows,
)
from utils.market_warehouse import clickhouse_query_df


OUT_DIR = ROOT / "reports" / "main_wave_sector_score_validation"


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


def _compound(values: pd.Series) -> float:
    vals = pd.to_numeric(values, errors="coerce").dropna()
    if vals.empty:
        return float("nan")
    return float((1.0 + vals).prod() - 1.0)


def _parse_int_list(text: str) -> list[int]:
    out: list[int] = []
    for item in str(text).split(","):
        item = item.strip()
        if item:
            out.append(int(item))
    return sorted(set(out))


def _parse_horizons(text: str) -> list[int]:
    horizons = _parse_int_list(text)
    return [x for x in horizons if x > 0] or [5, 10, 20, 40]


def _load_all_trade_dates(end_date: str) -> list[str]:
    df = clickhouse_query_df(
        """
        SELECT DISTINCT trade_date
        FROM kline_daily
        WHERE trade_date <= ?
        ORDER BY trade_date
        """,
        [end_date],
    )
    if df.empty:
        raise RuntimeError("no trade dates found")
    dates = pd.to_datetime(df["trade_date"], errors="coerce").dropna().sort_values()
    return [d.strftime("%Y-%m-%d") for d in dates]


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
                if len(fwd) < horizon:
                    continue
                out[(int(level), str(sector_code), signal_date, int(horizon))] = _compound(fwd)
    return out


def _pick_rebalance_dates(
    dates: list[str],
    start_date: str,
    end_date: str,
    lookback_days: int,
    step_days: int,
    max_horizon: int,
) -> tuple[str, list[str]]:
    if start_date not in dates:
        valid = [d for d in dates if d >= start_date]
        if not valid:
            raise RuntimeError(f"start_date {start_date} is after available dates")
        start_date = valid[0]
    start_idx = dates.index(start_date)
    first_idx = max(start_idx, lookback_days)
    last_idx = max(0, len(dates) - max_horizon - 1)
    selected = [dates[i] for i in range(first_idx, last_idx + 1, max(1, step_days)) if dates[i] <= end_date]
    if not selected:
        raise RuntimeError("no rebalance dates after lookback/horizon filters")
    load_start_idx = max(0, first_idx - lookback_days)
    return dates[load_start_idx], selected


def _validate(
    sector_daily: pd.DataFrame,
    sector_features: pd.DataFrame,
    rebalance_dates: list[str],
    horizons: list[int],
    top_n: int,
    min_score: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    fwd_map = _forward_return_map(sector_daily, horizons)
    daily_rows: list[dict[str, Any]] = []
    pick_rows: list[dict[str, Any]] = []
    features = sector_features.copy()
    features["trade_date_text"] = pd.to_datetime(features["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for signal_date in rebalance_dates:
        target_features = features[features["trade_date_text"] == signal_date].drop(columns=["trade_date_text"], errors="ignore").copy()
        if target_features.empty:
            continue
        scored = _score_rows(target_features)
        scored = scored.sort_values("main_wave_score", ascending=False).reset_index(drop=True)
        top = scored.head(top_n).copy()
        if min_score > 0:
            top = top[top["main_wave_score"] >= min_score].copy()
        bottom = scored.tail(top_n).copy()

        for horizon in horizons:
            all_vals: list[float] = []
            top_vals: list[float] = []
            bottom_vals: list[float] = []
            for row in scored.itertuples(index=False):
                value = fwd_map.get((int(row.level), str(row.sector_code), signal_date, horizon))
                if value is not None:
                    all_vals.append(value)
            for row in top.itertuples(index=False):
                value = fwd_map.get((int(row.level), str(row.sector_code), signal_date, horizon))
                if value is not None:
                    top_vals.append(value)
                    pick_rows.append(
                        {
                            "signal_date": signal_date,
                            "horizon": horizon,
                            "level": int(row.level),
                            "sector_code": str(row.sector_code),
                            "sector_name": str(row.sector_name),
                            "main_wave_score": float(row.main_wave_score),
                            "main_wave_bucket": str(row.main_wave_bucket),
                            "forward_ret": float(value),
                        }
                    )
            for row in bottom.itertuples(index=False):
                value = fwd_map.get((int(row.level), str(row.sector_code), signal_date, horizon))
                if value is not None:
                    bottom_vals.append(value)

            all_avg = pd.Series(all_vals, dtype="float64").mean()
            top_avg = pd.Series(top_vals, dtype="float64").mean()
            bottom_avg = pd.Series(bottom_vals, dtype="float64").mean()
            daily_rows.append(
                {
                    "signal_date": signal_date,
                    "horizon": horizon,
                    "selected_count": int(len(top_vals)),
                    "all_count": int(len(all_vals)),
                    "top_forward_avg": _safe_float(top_avg),
                    "all_forward_avg": _safe_float(all_avg),
                    "bottom_forward_avg": _safe_float(bottom_avg),
                    "top_minus_all": _safe_float(top_avg - all_avg),
                    "top_minus_bottom": _safe_float(top_avg - bottom_avg),
                    "top_positive_ratio": _safe_float((pd.Series(top_vals) > 0).mean()) if top_vals else None,
                }
            )
    return pd.DataFrame(daily_rows), pd.DataFrame(pick_rows)


def _summary(daily: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for horizon, g in daily.groupby("horizon"):
        g = g[pd.to_numeric(g["selected_count"], errors="coerce").fillna(0) > 0].copy()
        if g.empty:
            continue
        rows.append(
            {
                "horizon": int(horizon),
                "samples": int(len(g)),
                "avg_top_forward": _safe_float(g["top_forward_avg"].mean()),
                "avg_all_forward": _safe_float(g["all_forward_avg"].mean()),
                "avg_bottom_forward": _safe_float(g["bottom_forward_avg"].mean()),
                "avg_top_minus_all": _safe_float(g["top_minus_all"].mean()),
                "avg_top_minus_bottom": _safe_float(g["top_minus_bottom"].mean()),
                "hit_rate_top_beats_all": _safe_float((g["top_minus_all"] > 0).mean()),
                "hit_rate_top_beats_bottom": _safe_float((g["top_minus_bottom"] > 0).mean()),
                "avg_top_positive_ratio": _safe_float(g["top_positive_ratio"].mean()),
            }
        )
    return rows


def _pct(value: Any) -> str:
    x = _safe_float(value)
    if x is None:
        return ""
    return f"{x:.2%}"


def _write_report(result: dict[str, Any], daily: pd.DataFrame, picks: pd.DataFrame) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    daily_path = OUT_DIR / "daily_validation.csv"
    picks_path = OUT_DIR / "selected_sectors.csv"
    json_path = OUT_DIR / "summary.json"
    report_path = OUT_DIR / "REPORT.md"

    daily.to_csv(daily_path, index=False, encoding="utf-8-sig")
    picks.to_csv(picks_path, index=False, encoding="utf-8-sig")
    payload = dict(result)
    payload["daily_validation_csv"] = str(daily_path)
    payload["selected_sectors_csv"] = str(picks_path)
    payload["report"] = str(report_path)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 主升浪板块评分历史验证",
        "",
        f"- 验证区间：`{result['start_date']}` 到 `{result['end_date']}`",
        f"- 再平衡日数量：`{result['rebalance_count']}`",
        f"- 选板块规则：每个再平衡日选评分 Top `{result['top_n']}`，最低分阈值 `{result['min_score']}`",
        f"- 板块层级：`{result['levels']}`",
        "- 口径提醒：当前验证使用当前 `sector_stocks` 成分映射合成历史板块收益，存在历史成分幸存者/重分类偏差；结论只能先用于研究方向判断。",
        "- 交易口径：信号日收盘后已知评分，未来收益从下一个交易日开始计算，避免把信号日涨幅放进结果。",
        "",
        "## 汇总",
        "",
        "| 持有天数 | 样本数 | Top未来收益 | 全板块未来收益 | Bottom未来收益 | Top-全板块 | Top-Bottom | 跑赢全板块比例 | Top正收益比例 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result["summary"]:
        lines.append(
            f"| {row['horizon']} | {row['samples']} | {_pct(row['avg_top_forward'])} | "
            f"{_pct(row['avg_all_forward'])} | {_pct(row['avg_bottom_forward'])} | "
            f"{_pct(row['avg_top_minus_all'])} | {_pct(row['avg_top_minus_bottom'])} | "
            f"{_pct(row['hit_rate_top_beats_all'])} | {_pct(row['avg_top_positive_ratio'])} |"
        )
    lines.extend(
        [
            "",
            "## 结论读法",
            "",
            "- 如果 `Top-全板块` 在 10/20/40 日为正，且跑赢比例高于 50%，说明评分具备板块主线排序价值。",
            "- 如果只在 5 日为正、20/40 日转弱，说明模型更像短线热度，不是主升浪识别。",
            "- 如果 `Top-Bottom` 明显为正但 `Top-全板块` 不强，说明评分能排除弱板块，但进攻力还不够。",
            "- 下一层验证应把 `selected_sectors.csv` 接到个股池，比较高分板块内强势股与全市场强势股的收益差异。",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(str(report_path))
    print(str(daily_path))
    print(str(picks_path))


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate main-wave sector score history.")
    parser.add_argument("--start-date", default="2025-01-01")
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--levels", default="1,2,3")
    parser.add_argument("--lookback-days", type=int, default=140)
    parser.add_argument("--step-days", type=int, default=20)
    parser.add_argument("--horizons", default="5,10,20,40")
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--min-score", type=float, default=0.0)
    parser.add_argument("--min-members", type=int, default=5)
    args = parser.parse_args()

    args.end_date = args.end_date or _latest_trade_date()
    levels = _parse_int_list(args.levels)
    horizons = _parse_horizons(args.horizons)
    dates = _load_all_trade_dates(args.end_date)
    load_start, rebalance_dates = _pick_rebalance_dates(
        dates,
        args.start_date,
        args.end_date,
        args.lookback_days,
        args.step_days,
        max(horizons),
    )
    members = _load_members(levels, args.min_members)
    daily = _add_stock_features(_load_daily(load_start, args.end_date))
    sector_daily = _build_sector_daily(daily, members)
    sector_features = _add_sector_feature_frame(sector_daily)
    daily_result, picks = _validate(sector_daily, sector_features, rebalance_dates, horizons, args.top_n, args.min_score)
    result = {
        "start_date": args.start_date,
        "end_date": args.end_date,
        "load_start": load_start,
        "levels": levels,
        "lookback_days": args.lookback_days,
        "step_days": args.step_days,
        "horizons": horizons,
        "top_n": args.top_n,
        "min_score": args.min_score,
        "min_members": args.min_members,
        "rebalance_count": len(rebalance_dates),
        "rebalance_dates": rebalance_dates,
        "input_rows": {
            "members": int(len(members)),
            "daily": int(len(daily)),
            "sector_daily": int(len(sector_daily)),
            "sector_features": int(len(sector_features)),
        },
        "summary": _summary(daily_result),
    }
    _write_report(result, daily_result, picks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
