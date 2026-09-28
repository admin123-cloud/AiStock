from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
import sys
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

REPO_ROOT = _PROJECT_ROOT
sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_query_df


REPORT_DIR = _report_path() / "v4_dropout_forward_returns_1y"
HORIZONS = [3, 5, 10, 20]


def _load_events(top_n: int) -> pd.DataFrame:
    path = REPORT_DIR / f"drop_top{top_n}_events.csv"
    df = pd.read_csv(path)
    df["event_date"] = pd.to_datetime(df["event_date"], errors="coerce")
    return df


def _load_market_stage() -> pd.DataFrame:
    idx = clickhouse_query_df(
        """
        SELECT trade_date, close
        FROM kline_daily
        WHERE code = '000300.SH'
        ORDER BY trade_date ASC
        """
    )
    if idx.empty:
        raise RuntimeError("No index data for 000300.SH")
    idx["trade_date"] = pd.to_datetime(idx["trade_date"], errors="coerce")
    idx["close"] = pd.to_numeric(idx["close"], errors="coerce")
    idx = idx.dropna(subset=["trade_date", "close"]).copy()
    idx = idx.sort_values("trade_date").reset_index(drop=True)
    idx["ma20"] = idx["close"].rolling(20, min_periods=20).mean()
    idx["ma60"] = idx["close"].rolling(60, min_periods=60).mean()
    idx["mom20"] = idx["close"] / idx["close"].shift(20) - 1.0

    def _stage(row: pd.Series) -> str:
        close = row.get("close")
        ma20 = row.get("ma20")
        ma60 = row.get("ma60")
        mom20 = row.get("mom20")
        if pd.notna(close) and pd.notna(ma20) and pd.notna(ma60) and pd.notna(mom20):
            if close > ma20 and ma20 > ma60 and mom20 > 0:
                return "trend_up"
            if close < ma20 and ma20 < ma60 and mom20 < 0:
                return "trend_down"
        return "range"

    idx["market_stage"] = idx.apply(_stage, axis=1)
    return idx[["trade_date", "market_stage"]]


def _summarize(df: pd.DataFrame, group_cols: List[str]) -> pd.DataFrame:
    rows: List[Dict[str, object]] = []
    grouped = df.groupby(group_cols, dropna=False, sort=True)
    for keys, g in grouped:
        if not isinstance(keys, tuple):
            keys = (keys,)
        key_map = dict(zip(group_cols, keys))
        for h in HORIZONS:
            col = f"ret_{h}d"
            s = pd.to_numeric(g[col], errors="coerce").dropna()
            if s.empty:
                continue
            row: Dict[str, object] = dict(key_map)
            row.update(
                {
                    "horizon_d": h,
                    "samples": int(s.shape[0]),
                    "avg_return_pct": round(float(s.mean() * 100), 3),
                    "median_return_pct": round(float(s.median() * 100), 3),
                    "win_rate_pct": round(float((s > 0).mean() * 100), 2),
                    "q25_return_pct": round(float(s.quantile(0.25) * 100), 3),
                    "q75_return_pct": round(float(s.quantile(0.75) * 100), 3),
                }
            )
            rows.append(row)
    return pd.DataFrame(rows)


def main() -> int:
    market_stage = _load_market_stage()
    all_rows: List[pd.DataFrame] = []
    by_stage_rows: List[pd.DataFrame] = []

    payload: Dict[str, object] = {"topn": {}}

    for top_n in [30, 50]:
        ev = _load_events(top_n)
        ev = ev.merge(market_stage, left_on="event_date", right_on="trade_date", how="left")
        ev["market_stage"] = ev["market_stage"].fillna("range")
        ev = ev.drop(columns=["trade_date"])
        ev["top_n"] = top_n

        part1 = _summarize(ev, ["top_n", "drop_type"])
        part2 = _summarize(ev, ["top_n", "drop_type", "market_stage"])
        all_rows.append(part1)
        by_stage_rows.append(part2)

        payload["topn"][str(top_n)] = {
            "event_count": int(ev.shape[0]),
            "drop_type_counts": ev["drop_type"].value_counts(dropna=False).to_dict(),
            "market_stage_counts": ev["market_stage"].value_counts(dropna=False).to_dict(),
        }

    summary_drop_type = pd.concat(all_rows, ignore_index=True).sort_values(["top_n", "drop_type", "horizon_d"])
    summary_by_stage = pd.concat(by_stage_rows, ignore_index=True).sort_values(
        ["top_n", "drop_type", "market_stage", "horizon_d"]
    )

    summary_drop_type.to_csv(REPORT_DIR / "drop_split_by_type_summary.csv", index=False, encoding="utf-8-sig")
    summary_by_stage.to_csv(REPORT_DIR / "drop_split_by_type_stage_summary.csv", index=False, encoding="utf-8-sig")
    (REPORT_DIR / "drop_split_meta.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("saved:", REPORT_DIR / "drop_split_by_type_summary.csv")
    print("saved:", REPORT_DIR / "drop_split_by_type_stage_summary.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
