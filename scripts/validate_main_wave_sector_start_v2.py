from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
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
    _score_inverse,
)
from scripts.validate_main_wave_sector_score_history import (
    _compound,
    _load_all_trade_dates,
    _parse_horizons,
    _pick_rebalance_dates,
)


OUT_DIR = ROOT / "reports" / "main_wave_sector_start_v2_validation"


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


def _add_v2_features(features: pd.DataFrame) -> pd.DataFrame:
    d = features.copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce")
    d = d.dropna(subset=["trade_date"]).sort_values(["level", "sector_code", "trade_date"]).copy()
    for col in [
        "ret5",
        "ret10",
        "ret20",
        "ret60",
        "rank_ret20_pct",
        "amount_ratio5_20",
        "amount_ratio20_60",
        "ma20_ratio",
        "ma60_ratio",
        "rise_ratio",
        "strong3_ratio",
        "new_high60_ratio",
    ]:
        d[col] = pd.to_numeric(d.get(col), errors="coerce")

    d["rank_ret5_pct"] = d.groupby(["level", "trade_date"])["ret5"].rank(pct=True, ascending=True)
    d["rank_ret10_pct"] = d.groupby(["level", "trade_date"])["ret10"].rank(pct=True, ascending=True)
    d["rank_ret20_prev10"] = d.groupby(["level", "sector_code"])["rank_ret20_pct"].shift(10)
    d["rank_ret20_lift10"] = d["rank_ret20_pct"] - d["rank_ret20_prev10"]
    d["ma20_ratio_prev10"] = d.groupby(["level", "sector_code"])["ma20_ratio"].shift(10)
    d["ma20_ratio_lift10"] = d["ma20_ratio"] - d["ma20_ratio_prev10"]
    d["amount_accel5_20_vs20_60"] = d["amount_ratio5_20"] / d["amount_ratio20_60"].replace(0, np.nan)
    d["ret20_over_ret60"] = d["ret20"] / d["ret60"].replace(0, np.nan)
    return d.reset_index(drop=True)


def _score_start_v2(target: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for row in target.itertuples(index=False):
        accumulation = 25.0 * (
            0.35 * _score_inverse(row.pre_range_60_20, 0.10, 0.35)
            + 0.25 * _score_between(row.breakout_from_pre_high, -0.01, 0.10)
            + 0.20 * _score_between(row.ma60_ratio, 0.35, 0.70)
            + 0.20 * _score_inverse(abs(float(row.ret60 or 0.0)), 0.15, 0.55)
        )
        ignition = 25.0 * (
            0.20 * _score_between(row.ret5, 0.01, 0.09)
            + 0.20 * _score_between(row.ret10, 0.02, 0.16)
            + 0.20 * _score_between(row.rank_ret5_pct, 0.55, 0.92)
            + 0.15 * _score_between(row.rank_ret10_pct, 0.55, 0.92)
            + 0.15 * _score_between(row.rank_ret20_lift10, 0.05, 0.35)
            + 0.10 * _score_between(row.amount_accel5_20_vs20_60, 1.00, 1.80)
        )
        breadth_start = 20.0 * (
            0.25 * _score_between(row.rise_ratio, 0.50, 0.72)
            + 0.20 * _score_between(row.strong3_ratio, 0.03, 0.16)
            + 0.20 * _score_between(row.ma20_ratio, 0.45, 0.75)
            + 0.20 * _score_between(row.ma20_ratio_lift10, 0.05, 0.30)
            + 0.15 * _score_between(row.new_high60_ratio, 0.01, 0.14)
        )
        not_overheated = 20.0 * (
            0.30 * _score_inverse(row.ret20, 0.14, 0.32)
            + 0.20 * _score_inverse(row.ret60, 0.22, 0.55)
            + 0.20 * _score_inverse(row.strong3_ratio, 0.18, 0.40)
            + 0.15 * _score_inverse(row.amount_ratio5_20, 1.80, 3.00)
            + 0.15 * _score_inverse(row.new_high60_ratio, 0.18, 0.40)
        )
        market_env = 10.0 * (
            0.40 * _score_between(row.market_ret20, -0.03, 0.06)
            + 0.35 * _score_between(row.market_ma20_ratio, 0.35, 0.68)
            + 0.25 * _score_between(row.market_rise_ratio, 0.40, 0.63)
        )
        late_penalty = 0.0
        late_penalty += 8.0 * _score_between(row.ret5, 0.12, 0.22)
        late_penalty += 8.0 * _score_between(row.ret20, 0.25, 0.45)
        late_penalty += 6.0 * _score_between(row.ret60, 0.40, 0.80)
        late_penalty += 5.0 * _score_between(row.strong3_ratio, 0.22, 0.45)
        late_penalty += 5.0 * _score_between(row.amount_ratio5_20, 2.20, 3.50)

        score = max(0.0, min(100.0, accumulation + ignition + breadth_start + not_overheated + market_env - late_penalty))
        bucket = "start_watch"
        if score >= 75:
            bucket = "start_confirmed"
        elif score >= 62:
            bucket = "start_candidate"
        elif score >= 50:
            bucket = "rotation_watch"

        item = row._asdict()
        item.update(
            {
                "start_v2_score": score,
                "start_v2_bucket": bucket,
                "accumulation_start_score": accumulation,
                "ignition_score": ignition,
                "breadth_start_score": breadth_start,
                "not_overheated_score": not_overheated,
                "market_env_score": market_env,
                "late_penalty": late_penalty,
            }
        )
        rows.append(item)
    out = pd.DataFrame(rows)
    return out.sort_values(["start_v2_score", "ignition_score"], ascending=False).reset_index(drop=True)


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


def _validate(
    sector_daily: pd.DataFrame,
    scored_features: pd.DataFrame,
    rebalance_dates: list[str],
    horizons: list[int],
    top_n: int,
    min_score: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    fwd_map = _forward_return_map(sector_daily, horizons)
    features = scored_features.copy()
    features["trade_date_text"] = pd.to_datetime(features["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    daily_rows: list[dict[str, Any]] = []
    pick_rows: list[dict[str, Any]] = []
    for signal_date in rebalance_dates:
        day = features[features["trade_date_text"].eq(signal_date)].drop(columns=["trade_date_text"], errors="ignore").copy()
        if day.empty:
            continue
        scored = _score_start_v2(day)
        top = scored.head(top_n).copy()
        if min_score > 0:
            top = top[top["start_v2_score"] >= min_score].copy()
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
                            "start_v2_score": float(row.start_v2_score),
                            "start_v2_bucket": str(row.start_v2_bucket),
                            "forward_ret": float(value),
                            "ret5": _safe_float(row.ret5),
                            "ret20": _safe_float(row.ret20),
                            "pre_range_60_20": _safe_float(row.pre_range_60_20),
                            "amount_ratio5_20": _safe_float(row.amount_ratio5_20),
                        }
                    )
            for row in bottom.itertuples(index=False):
                value = fwd_map.get((int(row.level), str(row.sector_code), signal_date, horizon))
                if value is not None:
                    bottom_vals.append(value)

            top_s = pd.Series(top_vals, dtype="float64")
            all_s = pd.Series(all_vals, dtype="float64")
            bottom_s = pd.Series(bottom_vals, dtype="float64")
            daily_rows.append(
                {
                    "signal_date": signal_date,
                    "horizon": horizon,
                    "selected_count": int(len(top_vals)),
                    "all_count": int(len(all_vals)),
                    "top_forward_avg": _safe_float(top_s.mean()),
                    "all_forward_avg": _safe_float(all_s.mean()),
                    "bottom_forward_avg": _safe_float(bottom_s.mean()),
                    "top_minus_all": _safe_float(top_s.mean() - all_s.mean()),
                    "top_minus_bottom": _safe_float(top_s.mean() - bottom_s.mean()),
                    "top_positive_ratio": _safe_float((top_s > 0).mean()) if len(top_s) else None,
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


def _write_outputs(result: dict[str, Any], daily: pd.DataFrame, picks: pd.DataFrame) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    daily_path = OUT_DIR / "daily_validation.csv"
    picks_path = OUT_DIR / "selected_sectors.csv"
    summary_path = OUT_DIR / "summary.json"
    report_path = OUT_DIR / "REPORT.md"
    daily.to_csv(daily_path, index=False, encoding="utf-8-sig")
    picks.to_csv(picks_path, index=False, encoding="utf-8-sig")
    payload = dict(result)
    payload["daily_validation_csv"] = str(daily_path)
    payload["selected_sectors_csv"] = str(picks_path)
    payload["report"] = str(report_path)
    summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 主升启动评分 V2 历史验证",
        "",
        f"- 验证区间：`{result['start_date']}` 到 `{result['end_date']}`",
        f"- 层级：`{result['levels']}`",
        f"- 再平衡日数量：`{result['rebalance_count']}`",
        f"- 规则：每个再平衡日选 `start_v2_score` Top `{result['top_n']}`，最低阈值 `{result['min_score']}`",
        "- 交易口径：信号日收盘后确认，未来收益从下一个交易日开始计算。",
        "- 数据限制：仍使用当前板块成分等权合成历史收益，存在历史成分偏差；本结果用于研究判断，不直接等同实盘。",
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
            "## 阶段判断",
            "",
            "- V2 不是沿用第一版强势评分，而是尝试识别“蓄势后刚启动”的板块。",
            "- 如果 10/20 日 `Top-全板块` 为正且跑赢比例超过 50%，说明主升启动识别具备继续接个股的价值。",
            "- 如果仍为负，说明当前日线板块层特征不足以稳定识别主升启动，需要转向盘中扩散、真实板块指数或个股成交承接。",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(str(report_path))
    print(str(daily_path))
    print(str(picks_path))


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate main-wave sector start V2.")
    parser.add_argument("--start-date", default="2025-01-01")
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--levels", default="2")
    parser.add_argument("--lookback-days", type=int, default=140)
    parser.add_argument("--step-days", type=int, default=20)
    parser.add_argument("--horizons", default="5,10,20,40")
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--min-score", type=float, default=0.0)
    parser.add_argument("--min-members", type=int, default=5)
    args = parser.parse_args()

    args.end_date = args.end_date or _latest_trade_date()
    levels = _parse_levels(args.levels)
    horizons = _parse_horizons(args.horizons)
    trade_dates = _load_all_trade_dates(args.end_date)
    load_start, rebalance_dates = _pick_rebalance_dates(
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
    daily_result, picks = _validate(sector_daily, features, rebalance_dates, horizons, args.top_n, args.min_score)
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
            "features": int(len(features)),
        },
        "summary": _summary(daily_result),
    }
    _write_outputs(result, daily_result, picks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
