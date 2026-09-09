from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

REPO_ROOT = _PROJECT_ROOT
sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_query_df

OUTPUT_DIR = _report_path() / "v4_dropout_forward_returns_1y"

V4_MIN_TURNOVER20 = 2e8
V4_MIN_VOL10 = 0.008
V4_MAX_VOL10 = 0.09
HORIZONS = list(range(3, 21))


@dataclass
class BacktestWindow:
    preload_start: str
    start_date: str
    end_date: str


def _ensure_output_dir() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _build_window() -> BacktestWindow:
    max_df = clickhouse_query_df("SELECT max(trade_date) AS end_date FROM kline_daily")
    if max_df.empty or pd.isna(max_df.iloc[0]["end_date"]):
        raise RuntimeError("kline_daily has no trade_date data")
    end_date = str(pd.Timestamp(max_df.iloc[0]["end_date"]).date())
    start_date = str((pd.Timestamp(end_date) - pd.DateOffset(years=1)).date())
    preload_start = str((pd.Timestamp(start_date) - pd.Timedelta(days=90)).date())
    return BacktestWindow(preload_start=preload_start, start_date=start_date, end_date=end_date)


def _load_history(window: BacktestWindow) -> pd.DataFrame:
    sql = """
    SELECT
        substr(k.code, 1, 6) AS code,
        s.name AS name,
        k.trade_date AS trade_date,
        k.close AS close,
        k.amount AS amount
    FROM kline_daily k
    JOIN stocks s ON s.code = k.code
    WHERE k.trade_date BETWEEN ?::DATE AND ?::DATE
      AND s.type = 'stock'
      AND s.quit = 0
      AND (s.st = 0 OR s.st IS NULL)
    ORDER BY code ASC, trade_date ASC
    """
    df = clickhouse_query_df(sql, [window.preload_start, window.end_date])
    if df.empty:
        raise RuntimeError("No daily history loaded")
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    for col in ["close", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["trade_date", "code", "close"]).copy()
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["amount"] = df["amount"].fillna(0.0) * 10000.0
    df = df.sort_values(["code", "trade_date"]).reset_index(drop=True)
    return df


def _build_v4_rank_pool(hist: pd.DataFrame) -> pd.DataFrame:
    d = hist.copy()
    g = d.groupby("code", sort=False)
    d["ret1_daily"] = g["close"].transform(lambda s: s / s.shift(1) - 1.0)
    d["mom5"] = g["close"].transform(lambda s: s / s.shift(5) - 1.0)
    d["mom10"] = g["close"].transform(lambda s: s / s.shift(10) - 1.0)
    d["ma10"] = g["close"].transform(lambda s: s.rolling(10, min_periods=10).mean())
    d["amt20"] = g["amount"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    d["amt5_prev"] = g["amount"].transform(lambda s: s.shift(1).rolling(5, min_periods=5).mean())
    d["vol_ratio"] = d["amount"] / d["amt5_prev"]
    d["vol10"] = g["ret1_daily"].transform(lambda s: s.rolling(10, min_periods=10).std())

    d = d.dropna(subset=["mom5", "mom10", "ma10", "amt20", "vol_ratio", "vol10"]).copy()
    d = d[(d["mom5"] > 0) & (d["close"] > d["ma10"]) & (d["amt20"] >= V4_MIN_TURNOVER20)].copy()
    d = d[(d["vol10"] >= V4_MIN_VOL10) & (d["vol10"] <= V4_MAX_VOL10)].copy()
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
    d = d.sort_values(["trade_date", "v4_score", "amt20", "mom5"], ascending=[True, False, False, False]).copy()
    d["v4_rank"] = d.groupby("trade_date").cumcount() + 1
    return d


def _build_event_rows(ranked: pd.DataFrame, top_n: int) -> pd.DataFrame:
    top = ranked[ranked["v4_rank"] <= top_n][["trade_date", "code", "name", "v4_rank"]].copy()
    if top.empty:
        return pd.DataFrame(columns=["event_date", "code", "name", "prev_rank", "current_rank", "drop_type"])
    date_list = sorted(ranked["trade_date"].dropna().unique().tolist())
    per_day = {dt: frame for dt, frame in ranked.groupby("trade_date")}
    top_sets = {dt: set(frame.loc[frame["v4_rank"] <= top_n, "code"].tolist()) for dt, frame in per_day.items()}
    top_rank_map = {
        dt: dict(zip(frame.loc[frame["v4_rank"] <= top_n, "code"], frame.loc[frame["v4_rank"] <= top_n, "v4_rank"]))
        for dt, frame in per_day.items()
    }
    rank_map = {dt: dict(zip(frame["code"], frame["v4_rank"])) for dt, frame in per_day.items()}
    name_map = {dt: dict(zip(frame["code"], frame["name"])) for dt, frame in per_day.items()}

    events: List[Dict[str, object]] = []
    for idx in range(1, len(date_list)):
        prev_dt = date_list[idx - 1]
        cur_dt = date_list[idx]
        prev_set = top_sets.get(prev_dt, set())
        cur_set = top_sets.get(cur_dt, set())
        dropped_codes = prev_set - cur_set
        if not dropped_codes:
            continue
        cur_rank_map = rank_map.get(cur_dt, {})
        cur_name_map = name_map.get(cur_dt, {})
        prev_top_rank = top_rank_map.get(prev_dt, {})
        prev_name_map = name_map.get(prev_dt, {})
        for code in sorted(dropped_codes):
            cur_rank = cur_rank_map.get(code)
            drop_type = "out_of_pool" if cur_rank is None else "still_in_pool_but_rank_down"
            events.append(
                {
                    "event_date": pd.Timestamp(cur_dt),
                    "code": code,
                    "name": cur_name_map.get(code) or prev_name_map.get(code) or code,
                    "prev_rank": int(prev_top_rank.get(code, 0)),
                    "current_rank": int(cur_rank) if cur_rank is not None else None,
                    "drop_type": drop_type,
                }
            )
    return pd.DataFrame(events)


def _attach_forward_returns(events: pd.DataFrame, hist: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return events
    px = hist[["code", "trade_date", "close"]].copy()
    px = px.sort_values(["code", "trade_date"]).reset_index(drop=True)
    g = px.groupby("code", sort=False)
    for h in HORIZONS:
        px[f"ret_{h}d"] = g["close"].shift(-h) / px["close"] - 1.0
    ret_cols = ["code", "trade_date"] + [f"ret_{h}d" for h in HORIZONS]
    merged = events.merge(px[ret_cols], left_on=["code", "event_date"], right_on=["code", "trade_date"], how="left")
    merged = merged.drop(columns=["trade_date"])
    return merged


def _summarize(events_with_ret: pd.DataFrame, top_n: int) -> pd.DataFrame:
    rows: List[Dict[str, object]] = []
    for h in HORIZONS:
        col = f"ret_{h}d"
        s = pd.to_numeric(events_with_ret[col], errors="coerce")
        valid = s.dropna()
        if valid.empty:
            rows.append({"top_n": top_n, "horizon_d": h, "samples": 0})
            continue
        rows.append(
            {
                "top_n": top_n,
                "horizon_d": h,
                "samples": int(valid.shape[0]),
                "avg_return_pct": round(float(valid.mean() * 100), 3),
                "median_return_pct": round(float(valid.median() * 100), 3),
                "win_rate_pct": round(float((valid > 0).mean() * 100), 2),
                "q25_return_pct": round(float(valid.quantile(0.25) * 100), 3),
                "q75_return_pct": round(float(valid.quantile(0.75) * 100), 3),
            }
        )
    return pd.DataFrame(rows)


def main() -> int:
    _ensure_output_dir()
    window = _build_window()
    hist = _load_history(window)
    ranked = _build_v4_rank_pool(hist)
    ranked_1y = ranked[
        (ranked["trade_date"] >= pd.Timestamp(window.start_date))
        & (ranked["trade_date"] <= pd.Timestamp(window.end_date))
    ].copy()
    if ranked_1y.empty:
        raise RuntimeError("No V4 ranked rows in 1Y window")

    all_summaries: List[pd.DataFrame] = []
    payload: Dict[str, object] = {
        "window": {"start_date": window.start_date, "end_date": window.end_date},
        "rules": {
            "mom5_gt_0": True,
            "close_gt_ma10": True,
            "amt20_min": V4_MIN_TURNOVER20,
            "vol10_range": [V4_MIN_VOL10, V4_MAX_VOL10],
            "weights": {"mom5": 0.35, "mom10": 0.20, "vol_ratio": 0.20, "amt20": 0.20, "vol10_low": 0.05},
        },
        "drop_stats": {},
    }

    for top_n in [30, 50]:
        events = _build_event_rows(ranked_1y, top_n=top_n)
        events = _attach_forward_returns(events, hist)
        summary = _summarize(events, top_n=top_n)
        all_summaries.append(summary)

        events_out = events.copy()
        events_out["event_date"] = events_out["event_date"].dt.strftime("%Y-%m-%d")
        events_out.to_csv(OUTPUT_DIR / f"drop_top{top_n}_events.csv", index=False, encoding="utf-8-sig")
        summary.to_csv(OUTPUT_DIR / f"drop_top{top_n}_summary.csv", index=False, encoding="utf-8-sig")

        payload["drop_stats"][f"top{top_n}"] = {
            "event_count": int(events.shape[0]),
            "drop_type_counts": events["drop_type"].value_counts(dropna=False).to_dict(),
            "summary": summary.to_dict(orient="records"),
        }

    merged_summary = pd.concat(all_summaries, ignore_index=True)
    merged_summary.to_csv(OUTPUT_DIR / "drop_top30_top50_summary.csv", index=False, encoding="utf-8-sig")
    (OUTPUT_DIR / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["window"], ensure_ascii=False))
    print(f"output_dir={OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
