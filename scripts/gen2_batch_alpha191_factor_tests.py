from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from api.alpha191_engine import Alpha191FormulaError, Alpha191UnsupportedError
from api.gen2_factor import (
    _add_factor,
    _add_forward_returns,
    _daily_ic,
    _date_or_default,
    _estimate_prewarm_days,
    _json_safe,
    _latest_trade_date,
    _load_daily_ohlcv,
    _quantile_spread,
    build_gen2_factor_registry,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_alpha191_factor_tests"


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        val = float(value)
    except Exception:
        return None
    if not math.isfinite(val):
        return None
    return val


def _fmt_pct(value: Any, digits: int = 2) -> str:
    val = _safe_float(value)
    return "--" if val is None else f"{val * 100:.{digits}f}%"


def _fmt_num(value: Any, digits: int = 3) -> str:
    val = _safe_float(value)
    return "--" if val is None else f"{val:.{digits}f}"


def _bucket_monotonicity(buckets: List[Dict[str, Any]]) -> Optional[float]:
    if len(buckets) < 3:
        return None
    x = pd.Series([row.get("bucket") for row in buckets], dtype=float)
    y = pd.Series([row.get("mean_return") for row in buckets], dtype=float)
    corr = x.corr(y)
    return _safe_float(corr)


def _long_leg_from_spread(spread: Dict[str, Any]) -> Dict[str, Any]:
    spread_mean = _safe_float(spread.get("top_bottom_spread"))
    direction = "high" if spread_mean is None or spread_mean >= 0 else "low"
    buckets = {int(row.get("bucket")): _safe_float(row.get("mean_return")) for row in spread.get("buckets") or []}
    long_mean_return = buckets.get(5) if direction == "high" else buckets.get(1)
    spread_positive = _safe_float(spread.get("spread_positive_ratio"))
    return {
        "direction": direction,
        "long_mean_return": long_mean_return,
        "long_positive_ratio": None,
        "edge_mean": abs(spread_mean) if spread_mean is not None else None,
        "edge_positive_ratio": spread_positive if direction == "high" else (1.0 - spread_positive if spread_positive is not None else None),
    }


def _score_row(row: Dict[str, Any]) -> float:
    edge = abs(_safe_float(row.get("top_bottom_spread")) or 0.0)
    ic = abs(_safe_float(row.get("mean_ic")) or 0.0)
    mono = abs(_safe_float(row.get("bucket_monotonicity")) or 0.0)
    edge_win = _safe_float(row.get("edge_positive_ratio")) or 0.0
    coverage = _safe_float(row.get("factor_coverage")) or 0.0
    return edge * 100.0 + ic * 10.0 + mono * 0.2 + edge_win * 0.2 + coverage * 0.05


def _rating(row: Dict[str, Any]) -> str:
    score = _safe_float(row.get("effectiveness_score")) or 0.0
    days = int(row.get("ic_days") or 0)
    coverage = _safe_float(row.get("factor_coverage")) or 0.0
    if days < 120 or coverage < 0.35:
        return "insufficient"
    if score >= 0.75:
        return "strong"
    if score >= 0.45:
        return "medium"
    if score >= 0.25:
        return "watch"
    return "weak"


def _evaluate_factor_horizon(
    factor_df: pd.DataFrame,
    meta: Dict[str, Any],
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
    horizon: int,
    min_symbols: int,
) -> Dict[str, Any]:
    target = f"fwd_{horizon}d"
    eval_df = factor_df[(factor_df["date"] >= start_ts) & (factor_df["date"] <= end_ts)].copy()
    valid = eval_df.dropna(subset=["factor_value", target]).copy()
    if valid.empty:
        return {
            "factor_id": meta.get("id"),
            "horizon": horizon,
            "available": False,
            "message": "no valid samples",
        }

    ic = _daily_ic(valid, target, min_symbols=min_symbols)
    spread = _quantile_spread(valid, target, min_symbols=min_symbols)
    long_leg = _long_leg_from_spread(spread)
    buckets = spread.get("buckets") or []
    row = {
        "factor_id": meta.get("id"),
        "factor_no": meta.get("no"),
        "name": meta.get("name"),
        "theme": meta.get("theme"),
        "horizon": horizon,
        "available": True,
        "direction": long_leg["direction"],
        "valid_rows": int(len(valid)),
        "trade_days": int(valid["date"].nunique()),
        "symbols": int(valid["code"].nunique()),
        "factor_coverage": float(valid["factor_value"].notna().mean()),
        "ic_days": ic.get("days"),
        "mean_ic": ic.get("mean_ic"),
        "ic_ir": ic.get("ic_ir"),
        "ic_positive_ratio": ic.get("positive_ratio"),
        "top_bottom_spread": spread.get("top_bottom_spread"),
        "spread_positive_ratio": spread.get("spread_positive_ratio"),
        "edge_mean": long_leg.get("edge_mean"),
        "edge_positive_ratio": long_leg.get("edge_positive_ratio"),
        "long_mean_return": long_leg.get("long_mean_return"),
        "long_positive_ratio": long_leg.get("long_positive_ratio"),
        "bucket_monotonicity": _bucket_monotonicity(buckets),
        "bucket_returns": json.dumps([{k: _json_safe(v) for k, v in item.items()} for item in buckets], ensure_ascii=False),
        "message": "",
    }
    row["effectiveness_score"] = _score_row(row)
    row["rating"] = _rating(row)
    return row


def run_batch(
    start_date: Optional[str],
    end_date: Optional[str],
    horizons: Iterable[int],
    min_symbols: int,
    output_dir: Path,
    factor_pattern: Optional[str] = None,
    resume: bool = True,
) -> Dict[str, Any]:
    latest = _latest_trade_date()
    if not latest:
        raise RuntimeError("No kline_daily trade date found")
    end_ts = _date_or_default(end_date, pd.Timestamp(latest))
    start_ts = _date_or_default(start_date, end_ts - pd.DateOffset(years=2))

    registry = build_gen2_factor_registry()
    factors = registry.get("factors") or []
    if factor_pattern:
        regex = re.compile(factor_pattern, re.IGNORECASE)
        factors = [item for item in factors if regex.search(str(item.get("id") or ""))]
    implemented = [item for item in factors if item.get("backend_status") == "implemented"]
    skipped = [item for item in factors if item.get("backend_status") != "implemented"]
    max_prewarm = max([_estimate_prewarm_days(item) for item in implemented] or [420])
    horizons = sorted({int(x) for x in horizons})

    print(
        f"[{datetime.now():%H:%M:%S}] loading daily bars {start_ts:%Y-%m-%d}..{end_ts:%Y-%m-%d}, "
        f"prewarm={max_prewarm}, factors={len(factors)}, horizons={horizons}",
        flush=True,
    )
    daily = _load_daily_ohlcv(
        start_ts.strftime("%Y-%m-%d"),
        end_ts.strftime("%Y-%m-%d"),
        horizon=max(horizons),
        prewarm_days=max_prewarm,
    )
    if daily.empty:
        raise RuntimeError("Daily OHLCV data is empty")
    print(
        f"[{datetime.now():%H:%M:%S}] loaded {len(daily):,} rows, "
        f"{daily['date'].nunique():,} days, {daily['code'].nunique():,} symbols",
        flush=True,
    )
    base_df = _add_forward_returns(daily, horizons=horizons)

    rows: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    progress_path = output_dir / "alpha191_recent_2y_factor_tests.progress.csv"
    done_factors = set()
    if resume and progress_path.exists():
        old = pd.read_csv(progress_path)
        if not old.empty and "factor_id" in old.columns:
            rows.extend(old.to_dict("records"))
            done_factors = set(old["factor_id"].dropna().astype(str).unique())
            print(f"[{datetime.now():%H:%M:%S}] resume: {len(done_factors)} factors already done", flush=True)

    for idx, meta in enumerate(factors, start=1):
        factor_id = meta.get("id")
        if factor_id in done_factors:
            continue
        print(f"[{datetime.now():%H:%M:%S}] {idx:03d}/{len(factors):03d} {factor_id} start", flush=True)
        if meta.get("backend_status") != "implemented":
            missing = ",".join(meta.get("backend_missing_fields") or [])
            for horizon in horizons:
                rows.append(
                    {
                        "factor_id": factor_id,
                        "factor_no": meta.get("no"),
                        "name": meta.get("name"),
                        "theme": meta.get("theme"),
                        "horizon": horizon,
                        "available": False,
                        "rating": "needs_data_source",
                        "message": f"needs extra data source: {missing}",
                    }
                )
            pd.DataFrame(rows).to_csv(progress_path, index=False, encoding="utf-8-sig")
            continue
        try:
            factor_df = _add_factor(base_df, meta)
        except (Alpha191FormulaError, Alpha191UnsupportedError, Exception) as exc:
            errors.append({"factor_id": factor_id, "error": f"{type(exc).__name__}: {exc}"})
            for horizon in horizons:
                rows.append(
                    {
                        "factor_id": factor_id,
                        "factor_no": meta.get("no"),
                        "name": meta.get("name"),
                        "theme": meta.get("theme"),
                        "horizon": horizon,
                        "available": False,
                        "rating": "error",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )
            pd.DataFrame(rows).to_csv(progress_path, index=False, encoding="utf-8-sig")
            continue
        for horizon in horizons:
            rows.append(_evaluate_factor_horizon(factor_df, meta, start_ts, end_ts, horizon, min_symbols))
        pd.DataFrame(rows).to_csv(progress_path, index=False, encoding="utf-8-sig")
        print(f"[{datetime.now():%H:%M:%S}] {idx:03d}/{len(factors):03d} {factor_id} done", flush=True)

    result_df = pd.DataFrame(rows)
    csv_path = output_dir / "alpha191_recent_2y_factor_tests.csv"
    result_df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    available = result_df[result_df["available"] == True].copy()  # noqa: E712
    ranking = available.sort_values(["effectiveness_score", "edge_mean"], ascending=[False, False]).copy()
    ranking_path = output_dir / "alpha191_recent_2y_factor_ranking.csv"
    ranking.to_csv(ranking_path, index=False, encoding="utf-8-sig")

    best_by_factor = pd.DataFrame()
    if not available.empty:
        best_by_factor = available.sort_values(["factor_id", "effectiveness_score"], ascending=[True, False]).groupby("factor_id", as_index=False).head(1)
        best_by_factor = best_by_factor.sort_values(["effectiveness_score", "edge_mean"], ascending=[False, False])
        best_by_factor.to_csv(output_dir / "alpha191_recent_2y_best_by_factor.csv", index=False, encoding="utf-8-sig")

    md_path = output_dir / "findings.md"
    _write_markdown(md_path, start_ts, end_ts, daily, registry, result_df, ranking, best_by_factor, errors)

    return {
        "output_dir": str(output_dir),
        "csv": str(csv_path),
        "ranking": str(ranking_path),
        "findings": str(md_path),
        "rows": int(len(result_df)),
        "available_rows": int(len(available)),
        "errors": errors,
    }


def _write_markdown(
    path: Path,
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
    daily: pd.DataFrame,
    registry: Dict[str, Any],
    result_df: pd.DataFrame,
    ranking: pd.DataFrame,
    best_by_factor: pd.DataFrame,
    errors: List[Dict[str, Any]],
) -> None:
    lines: List[str] = []
    lines.append("# G2 Alpha191 最近两年单因子测试")
    lines.append("")
    lines.append(f"- 测试区间: {start_ts:%Y-%m-%d} 至 {end_ts:%Y-%m-%d}")
    lines.append(f"- 加载行情: {len(daily):,} 行，{daily['date'].nunique():,} 个交易日，{daily['code'].nunique():,} 只股票")
    lines.append("- 决策边界: T 日收盘后生成因子，只评估 T+1/T+3/T+5 收益，不用于盘中买点读取当日完整日线")
    lines.append(f"- 因子覆盖: {registry.get('summary')}")
    lines.append("")
    lines.append("## 结论口径")
    lines.append("")
    lines.append("- `direction=high` 表示高因子值组更强，`direction=low` 表示低因子值组更强。")
    lines.append("- `edge_mean` 是按最佳方向后的五分组高低组合收益差，越大代表分层赚钱能力越强。")
    lines.append("- `mean_ic`/`ic_ir` 评价排序有效性，`bucket_monotonicity` 评价五分组单调性。")
    lines.append("- `rating` 是粗筛标签，只用于进入下一轮 G2 组合验证，不直接等于实盘可用。")
    lines.append("")

    if not best_by_factor.empty:
        lines.append("## 每个因子最佳周期 Top 30")
        lines.append("")
        lines.append("|Rank|因子|周期|方向|评级|Edge|多头均值|Edge胜率|MeanIC|ICIR|单调性|")
        lines.append("|---:|---|---:|---|---|---:|---:|---:|---:|---:|---:|")
        for rank, (_, row) in enumerate(best_by_factor.head(30).iterrows(), start=1):
            lines.append(
                "|{rank}|{factor}|T+{h}|{direction}|{rating}|{edge}|{long_ret}|{edge_win}|{ic}|{icir}|{mono}|".format(
                    rank=rank,
                    factor=row.get("factor_id"),
                    h=int(row.get("horizon") or 0),
                    direction=row.get("direction"),
                    rating=row.get("rating"),
                    edge=_fmt_pct(row.get("edge_mean")),
                    long_ret=_fmt_pct(row.get("long_mean_return")),
                    edge_win=_fmt_pct(row.get("edge_positive_ratio")),
                    ic=_fmt_num(row.get("mean_ic"), 4),
                    icir=_fmt_num(row.get("ic_ir"), 2),
                    mono=_fmt_num(row.get("bucket_monotonicity"), 2),
                )
            )
        lines.append("")

    if not ranking.empty:
        lines.append("## 全部因子-周期 Top 30")
        lines.append("")
        lines.append("|Rank|因子|周期|方向|评级|Edge|多头均值|Edge胜率|MeanIC|ICIR|有效天数|")
        lines.append("|---:|---|---:|---|---|---:|---:|---:|---:|---:|---:|")
        for rank, (_, row) in enumerate(ranking.head(30).iterrows(), start=1):
            lines.append(
                "|{rank}|{factor}|T+{h}|{direction}|{rating}|{edge}|{long_ret}|{edge_win}|{ic}|{icir}|{days}|".format(
                    rank=rank,
                    factor=row.get("factor_id"),
                    h=int(row.get("horizon") or 0),
                    direction=row.get("direction"),
                    rating=row.get("rating"),
                    edge=_fmt_pct(row.get("edge_mean")),
                    long_ret=_fmt_pct(row.get("long_mean_return")),
                    edge_win=_fmt_pct(row.get("edge_positive_ratio")),
                    ic=_fmt_num(row.get("mean_ic"), 4),
                    icir=_fmt_num(row.get("ic_ir"), 2),
                    days=int(row.get("ic_days") or 0),
                )
            )
        lines.append("")

    skipped = result_df[result_df["available"] == False].copy()  # noqa: E712
    if not skipped.empty:
        lines.append("## 未完成/需补数据")
        lines.append("")
        for _, row in skipped[["factor_id", "message"]].drop_duplicates().iterrows():
            lines.append(f"- {row.get('factor_id')}: {row.get('message')}")
        lines.append("")

    if errors:
        lines.append("## 运行错误")
        lines.append("")
        for item in errors:
            lines.append(f"- {item['factor_id']}: {item['error']}")
        lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch test GTJA Alpha191 factors over the recent two years.")
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--horizons", default="1,3,5")
    parser.add_argument("--min-symbols", type=int, default=80)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--factor-pattern", default=None, help="Regex filter, e.g. Alpha00[1-9]")
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()

    horizons = [int(x.strip()) for x in args.horizons.split(",") if x.strip()]
    payload = run_batch(
        start_date=args.start_date,
        end_date=args.end_date,
        horizons=horizons,
        min_symbols=args.min_symbols,
        output_dir=Path(args.output_dir),
        factor_pattern=args.factor_pattern,
        resume=not args.no_resume,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
