from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_available, clickhouse_query_df  # noqa: E402

OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_event_study"
HORIZONS = (1, 2, 3, 5, 10, 20)
V4_ENTRY_SCORE = 0.75
V4_MIN_TURNOVER20 = 2e8
V4_MIN_VOL10 = 0.008
V4_MAX_VOL10 = 0.09
INDEX_CODE = "999999.SH"
INDEX_NAME = "上证指数"


def _sql_string_literal(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def _sql_in_list(values: List[str]) -> str:
    return "(" + ",".join(_sql_string_literal(v) for v in values) + ")"


def _chunks(values: List[str], size: int) -> List[List[str]]:
    return [values[idx : idx + size] for idx in range(0, len(values), size)]


def _write_parquet_atomic(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        df.to_parquet(tmp_path, index=False)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _normalize_amount_to_yuan(series: pd.Series) -> pd.Series:
    return series.astype(float) * 10000.0


def _build_breadth_map(history: pd.DataFrame) -> Dict[str, float]:
    if history.empty:
        return {}
    target = history.dropna(subset=["ma20"]).copy()
    target = target[target["amt20"] >= 3e8]
    if target.empty:
        return {}
    target["above_ma20"] = (target["close"] > target["ma20"]).astype(int)
    daily = target.groupby("trade_date", sort=True).agg(total=("code", "count"), above=("above_ma20", "sum")).reset_index()
    daily["breadth"] = np.where(daily["total"] > 0, daily["above"] / daily["total"], np.nan)
    return dict(zip(daily["trade_date"], daily["breadth"]))


def _load_index_history(start_date: str, end_date: str) -> tuple[str, str, pd.DataFrame]:
    if not clickhouse_available():
        return "", "", pd.DataFrame()
    index_name = INDEX_NAME
    try:
        name_df = clickhouse_query_df("SELECT name FROM stocks WHERE code = ? LIMIT 1", [INDEX_CODE])
        if name_df is not None and not name_df.empty and "name" in name_df.columns:
            index_name = str(name_df["name"].iloc[0] or INDEX_NAME)
    except Exception:
        index_name = INDEX_NAME
    df = clickhouse_query_df(
        """
        SELECT code, trade_date, open, high, low, close, turnover_rate
        FROM kline_daily
        WHERE code = ?
          AND trade_date BETWEEN ?::DATE AND ?::DATE
        ORDER BY trade_date
        """,
        [INDEX_CODE, start_date, end_date],
    )
    if df is None or df.empty:
        return "", "", pd.DataFrame()
    df["trade_date"] = df["trade_date"].astype(str).str[:10]
    df["name"] = index_name
    return INDEX_CODE, index_name, df


def _resolve_regime(index_row: Dict[str, Any], breadth: Optional[float]) -> tuple[str, List[str]]:
    close = _safe_float(index_row.get("close"))
    ma20 = _safe_float(index_row.get("ma20"))
    ma60 = _safe_float(index_row.get("ma60"))
    mom20 = _safe_float(index_row.get("mom20"))
    if None in (close, ma20, ma60, mom20):
        return "range", ["incomplete index context"]
    above_ma20 = close > ma20
    above_ma60 = close > ma60
    ma20_above_ma60 = ma20 > ma60
    positive_momentum = mom20 > 0
    if above_ma20 and above_ma60 and ma20_above_ma60 and positive_momentum:
        return "trend_up", ["index above MA20/MA60 with positive momentum"]
    if (not above_ma20) and (not above_ma60) and (not ma20_above_ma60) and (not positive_momentum):
        return "trend_down", ["index below MA20/MA60 with negative momentum"]
    return "range", ["mixed index context"]


def _date_str(value: Any) -> str:
    return str(pd.Timestamp(value).date())


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def _load_stock_history(start_date: str, end_date: str) -> pd.DataFrame:
    if not clickhouse_available():
        raise RuntimeError("ClickHouse is required for the G2 event dataset builder.")
    stock_df = clickhouse_query_df(
        """
        SELECT code, name
        FROM stocks
        WHERE type = 'stock'
          AND COALESCE(st, 0) = 0
          AND COALESCE(quit, 0) = 0
        ORDER BY code
        """
    )
    if stock_df is None or stock_df.empty:
        return pd.DataFrame()
    stock_df = stock_df.dropna(subset=["code"]).copy()
    stock_df["code"] = stock_df["code"].astype(str)
    stock_df["name"] = stock_df["name"].astype(str) if "name" in stock_df.columns else ""

    frames: List[pd.DataFrame] = []
    codes = stock_df["code"].drop_duplicates().tolist()
    for batch in _chunks(codes, 800):
        if not batch:
            continue
        code_filter = _sql_in_list(batch)
        part = clickhouse_query_df(
            f"""
            SELECT code, trade_date, open, high, low, close, amount
            FROM kline_daily
            WHERE code IN {code_filter}
              AND trade_date BETWEEN ?::DATE AND ?::DATE
            ORDER BY code, trade_date
            """,
            [start_date, end_date],
        )
        if part is not None and not part.empty:
            frames.append(part)

    df = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["code"] = df["code"].astype(str)
    df = df.merge(stock_df[["code", "name"]].drop_duplicates("code"), on="code", how="left")
    df["trade_date"] = df["trade_date"].astype(str).str[:10]
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["amount"] = _normalize_amount_to_yuan(pd.to_numeric(df["amount"], errors="coerce"))
    df = df.dropna(subset=["code", "trade_date", "close"])
    return df.sort_values(["code", "trade_date"]).reset_index(drop=True)


def _add_daily_features(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy().replace([np.inf, -np.inf], np.nan)
    g = d.groupby("code", sort=False)
    d["ret1_daily"] = g["close"].transform(lambda s: s.pct_change())
    d["ma10"] = g["close"].transform(lambda s: s.rolling(10, min_periods=10).mean())
    d["ma20"] = g["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    d["mom5"] = g["close"].transform(lambda s: s / s.shift(5) - 1.0)
    d["mom10"] = g["close"].transform(lambda s: s / s.shift(10) - 1.0)
    d["mom20"] = g["close"].transform(lambda s: s / s.shift(20) - 1.0)
    d["amt20"] = g["amount"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    d["amt5_prev"] = g["amount"].transform(lambda s: s.shift(1).rolling(5, min_periods=5).mean())
    d["vol_ratio"] = d["amount"] / d["amt5_prev"]
    d["vol10"] = g["ret1_daily"].transform(lambda s: s.rolling(10, min_periods=10).std())
    for horizon in HORIZONS:
        future_close = g["close"].shift(-horizon)
        d[f"fwd_ret_{horizon}d"] = future_close / d["close"] - 1.0
        future_max_close = g["close"].transform(lambda s, h=horizon: s.shift(-1).rolling(h, min_periods=1).max().shift(-(h - 1)))
        future_min_close = g["close"].transform(lambda s, h=horizon: s.shift(-1).rolling(h, min_periods=1).min().shift(-(h - 1)))
        d[f"mfe_close_{horizon}d"] = future_max_close / d["close"] - 1.0
        d[f"mae_close_{horizon}d"] = future_min_close / d["close"] - 1.0
    return d


def _rank_v4_pool(df: pd.DataFrame, max_rank: int) -> pd.DataFrame:
    required = ["code", "mom5", "mom10", "vol_ratio", "amt20", "vol10", "ma10", "close"]
    d = df.dropna(subset=required).copy()
    d = d[(d["mom5"] > 0) & (d["close"] > d["ma10"]) & (d["amt20"] >= V4_MIN_TURNOVER20)]
    d = d[(d["vol10"] >= V4_MIN_VOL10) & (d["vol10"] <= V4_MAX_VOL10)]
    if d.empty:
        return d
    for col in ["mom5", "mom10", "vol_ratio", "amt20"]:
        d[f"r_{col}"] = d.groupby("trade_date")[col].rank(pct=True)
    d["r_vol10_low"] = 1.0 - d.groupby("trade_date")["vol10"].rank(pct=True)
    d["v4_score"] = (
        0.35 * d["r_mom5"]
        + 0.20 * d["r_mom10"]
        + 0.20 * d["r_vol_ratio"]
        + 0.20 * d["r_amt20"]
        + 0.05 * d["r_vol10_low"]
    )
    d["v4_rank"] = d.groupby("trade_date")["v4_score"].rank(method="first", ascending=False).astype(int)
    d["entry_pass"] = d["v4_score"] >= V4_ENTRY_SCORE
    d["in_score_pool"] = True
    if int(max_rank) > 0:
        d = d[d["v4_rank"] <= int(max_rank)].copy()
    return d.sort_values(["trade_date", "v4_rank", "code"]).reset_index(drop=True)


def _add_rank_change_features(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return events
    d = events.sort_values(["code", "trade_date"]).copy()
    g = d.groupby("code", sort=False)
    d["previous_signal_date"] = g["trade_date"].shift(1)
    d["previous_rank"] = g["v4_rank"].shift(1)
    d["previous_score"] = g["v4_score"].shift(1)
    d["calendar_gap_days"] = (
        pd.to_datetime(d["trade_date"], errors="coerce") - pd.to_datetime(d["previous_signal_date"], errors="coerce")
    ).dt.days
    d["rank_change"] = d["previous_rank"] - d["v4_rank"]
    d["score_change"] = d["v4_score"] - d["previous_score"]
    d["is_new_in_pool"] = d["previous_rank"].isna()
    d["rank_change_status"] = np.select(
        [
            d["is_new_in_pool"],
            d["rank_change"] > 0,
            d["rank_change"] < 0,
            d["rank_change"].eq(0),
        ],
        ["new", "up", "down", "flat"],
        default="unknown",
    )
    d["pool_streak"] = g.cumcount() + 1
    break_mask = d["calendar_gap_days"].fillna(1) > 10
    streak_group = break_mask.groupby(d["code"]).cumsum()
    d["pool_streak"] = d.groupby(["code", streak_group]).cumcount() + 1
    return d.sort_values(["trade_date", "v4_rank", "code"]).reset_index(drop=True)


def _build_market_context(history: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    if history.empty:
        return pd.DataFrame()
    breadth = _build_breadth_map(history.rename(columns={"trade_date": "trade_date"}))
    _, index_name, idx = _load_index_history(start_date, end_date)
    if idx is None or idx.empty:
        return pd.DataFrame()
    idx = idx.copy()
    idx["trade_date"] = idx["trade_date"].astype(str).str[:10]
    idx["close"] = pd.to_numeric(idx["close"], errors="coerce")
    idx["index_ma20"] = idx["close"].rolling(20, min_periods=20).mean()
    idx["index_ma60"] = idx["close"].rolling(60, min_periods=60).mean()
    idx["index_mom20"] = idx["close"] / idx["close"].shift(20) - 1.0
    rows: List[Dict[str, Any]] = []
    for _, row in idx.iterrows():
        trade_date = str(row.get("trade_date"))
        payload = {
            "close": _safe_float(row.get("close")),
            "ma20": _safe_float(row.get("index_ma20")),
            "ma60": _safe_float(row.get("index_ma60")),
            "mom20": _safe_float(row.get("index_mom20")),
        }
        b = _safe_float(breadth.get(trade_date))
        regime, _ = _resolve_regime(payload, b)
        rows.append(
            {
                "trade_date": trade_date,
                "index_name": index_name,
                "index_close": payload["close"],
                "index_ma20": payload["ma20"],
                "index_ma60": payload["ma60"],
                "index_mom20": payload["mom20"],
                "market_breadth": b,
                "index_close_ge_ma20": bool(payload["close"] is not None and payload["ma20"] is not None and payload["close"] >= payload["ma20"]),
                "legacy_regime": regime,
            }
        )
    return pd.DataFrame(rows)


def _summarize(events: pd.DataFrame, args: argparse.Namespace, coverage: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "schema_version": 1,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "max_rank": int(args.max_rank),
        "rank_scope": "full_score_pool" if int(args.max_rank) <= 0 else f"top_{int(args.max_rank)}",
        "entry_score": V4_ENTRY_SCORE,
        "rows": int(len(events)),
        "signal_days": int(events["trade_date"].nunique()) if not events.empty else 0,
        "unique_stocks": int(events["code"].nunique()) if not events.empty else 0,
        "coverage": coverage,
        "outputs": {
            "dataset_parquet": "v4_event_dataset.parquet",
            "dataset_csv": "v4_event_dataset.csv",
            "summary_json": "summary.json",
        },
    }
    if events.empty:
        return summary

    def agg_frame(group_cols: List[str], name: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        grouped = events.groupby(group_cols, dropna=False, observed=True)
        for keys, sub in grouped:
            key_values = keys if isinstance(keys, tuple) else (keys,)
            item = {col: key_values[idx] for idx, col in enumerate(group_cols)}
            item["sample_count"] = int(len(sub))
            for horizon in HORIZONS:
                col = f"fwd_ret_{horizon}d"
                valid = sub[col].dropna()
                item[f"avg_ret_{horizon}d"] = _safe_float(valid.mean())
                item[f"median_ret_{horizon}d"] = _safe_float(valid.median())
                item[f"win_rate_{horizon}d"] = _safe_float((valid > 0).mean()) if len(valid) else None
            rows.append(item)
        rows.sort(key=lambda x: (-_safe_int(x.get("sample_count")), str(x)))
        summary[name] = rows
        return rows

    events = events.copy()
    events["rank_bucket"] = pd.cut(
        events["v4_rank"],
        bins=[0, 5, 10, 20, 30, 50, 9999],
        labels=["1-5", "6-10", "11-20", "21-30", "31-50", "50+"],
        right=True,
    )
    events["score_bucket"] = pd.cut(
        events["v4_score"],
        bins=[-np.inf, 0.75, 0.80, 0.85, 0.90, np.inf],
        labels=["<0.75", "0.75-0.80", "0.80-0.85", "0.85-0.90", "0.90+"],
        right=False,
    )
    agg_frame(["rank_bucket"], "by_rank_bucket")
    agg_frame(["entry_pass"], "by_entry_pass")
    agg_frame(["rank_change_status"], "by_rank_change_status")
    agg_frame(["legacy_regime"], "by_legacy_regime")
    agg_frame(["score_bucket"], "by_score_bucket")
    return summary


def build_dataset(args: argparse.Namespace) -> Dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    feature_start = _date_str(pd.Timestamp(args.start_date) - pd.Timedelta(days=160))
    label_end = _date_str(pd.Timestamp(args.end_date) + pd.Timedelta(days=45))
    raw = _load_stock_history(feature_start, label_end)
    if raw.empty:
        raise RuntimeError(f"No stock daily rows loaded for {feature_start}~{label_end}")

    coverage = {
        "raw_start_date": str(raw["trade_date"].min()),
        "raw_end_date": str(raw["trade_date"].max()),
        "raw_rows": int(len(raw)),
        "raw_stock_count": int(raw["code"].nunique()),
        "feature_start_date": feature_start,
        "label_end_date": label_end,
    }
    featured = _add_daily_features(raw)
    events = _rank_v4_pool(featured, int(args.max_rank))
    events = events[(events["trade_date"] >= args.start_date) & (events["trade_date"] <= args.end_date)].copy()
    events = _add_rank_change_features(events)
    market = _build_market_context(featured, feature_start, label_end)
    if not market.empty:
        events = events.merge(market, on="trade_date", how="left")

    ordered_cols = [
        "trade_date",
        "code",
        "name",
        "v4_rank",
        "v4_score",
        "entry_pass",
        "in_score_pool",
        "rank_change_status",
        "previous_signal_date",
        "previous_rank",
        "rank_change",
        "score_change",
        "pool_streak",
        "close",
        "mom5",
        "mom10",
        "mom20",
        "vol_ratio",
        "amt20",
        "vol10",
        "r_mom5",
        "r_mom10",
        "r_vol_ratio",
        "r_amt20",
        "r_vol10_low",
        "index_close",
        "index_ma20",
        "index_ma60",
        "index_mom20",
        "market_breadth",
        "index_close_ge_ma20",
        "legacy_regime",
    ]
    metric_cols: List[str] = []
    for horizon in HORIZONS:
        metric_cols.extend([f"fwd_ret_{horizon}d", f"mfe_close_{horizon}d", f"mae_close_{horizon}d"])
    keep_cols = [col for col in ordered_cols + metric_cols if col in events.columns]
    events = events[keep_cols].sort_values(["trade_date", "v4_rank", "code"]).reset_index(drop=True)

    parquet_path = output_dir / "v4_event_dataset.parquet"
    csv_path = output_dir / "v4_event_dataset.csv"
    _write_parquet_atomic(events, parquet_path)
    if not bool(getattr(args, "skip_csv", False)):
        events.to_csv(csv_path, index=False, encoding="utf-8-sig")

    summary = _summarize(events, args, coverage)
    if bool(getattr(args, "skip_csv", False)):
        summary["outputs"]["dataset_csv"] = None
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build G2 V4 reference-pool event dataset.")
    parser.add_argument("--start-date", default="2026-01-05")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--max-rank", type=int, default=0, help="0 keeps the full V4 score pool; positive values keep top N only.")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    parser.add_argument("--skip-csv", action="store_true", help="Write Parquet and summary only; useful for full-history datasets.")
    args = parser.parse_args()
    if not args.end_date:
        args.end_date = _date_str(pd.Timestamp.today())
    return args


def main() -> int:
    args = parse_args()
    summary = build_dataset(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
