from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = _PROJECT_ROOT
sys.path.insert(0, str(REPO_ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default  # noqa: E402
from utils.market_warehouse import clickhouse_client  # noqa: E402

DEFAULT_TRADES = _report_path() / "gen2_pullback_profit_extension_sweep" / "baseline_hold10_prevlow" / "trades.csv"
DEFAULT_OUTPUT_DIR = _report_path() / "gen2_exit_opportunity_after_sell"


def _load_future_daily(codes: list[str], start_date: str, end_date: str) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    quoted = ", ".join([f"'{code}'" for code in sorted(set(codes))])
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT code, trade_date, close
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY code, trade_date
        """
    )
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna(subset=["code", "trade_date", "close"]).reset_index(drop=True)


def _summarize(frame: pd.DataFrame, group_col: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, group in frame.groupby(group_col, dropna=False):
        row: dict[str, Any] = {group_col: key, "count": int(len(group))}
        for col in ["future_max_ret_10d", "future_max_ret_20d", "future_min_ret_10d", "future_min_ret_20d"]:
            x = pd.to_numeric(group[col], errors="coerce").dropna()
            if x.empty:
                continue
            row[f"{col}_mean"] = float(x.mean())
            row[f"{col}_median"] = float(x.median())
            row[f"{col}_gt10pct_rate"] = float((x > 0.10).mean())
            row[f"{col}_lt_minus5pct_rate"] = float((x < -0.05).mean())
        rows.append(row)
    return pd.DataFrame(rows).sort_values("count", ascending=False)


def run(trades_path: Path, output_dir: Path, end_date: str) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    trades = pd.read_csv(trades_path)
    if trades.empty:
        raise RuntimeError("Trades file is empty.")
    trades["sell_date"] = pd.to_datetime(trades["sell_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    trades["code"] = trades["code"].astype(str)
    trades["sell_price"] = pd.to_numeric(trades["sell_price"], errors="coerce")
    exits = trades[~trades["exit_reason"].isin(["take_profit_partial_30m"])].dropna(subset=["sell_date", "code", "sell_price"]).copy()
    start_date = exits["sell_date"].min()
    future = _load_future_daily(exits["code"].dropna().astype(str).unique().tolist(), str(start_date), end_date)
    future_by_code = {code: g.sort_values("trade_date").copy() for code, g in future.groupby("code", sort=False)}

    rows: list[dict[str, Any]] = []
    for row in exits.to_dict("records"):
        code = str(row["code"])
        sell_date = str(row["sell_date"])
        sell_price = float(row["sell_price"])
        daily = future_by_code.get(code, pd.DataFrame())
        after = daily[daily["trade_date"] > sell_date].head(20).copy() if not daily.empty else pd.DataFrame()
        rec = dict(row)
        for n in [10, 20]:
            w = after.head(n)
            if w.empty or sell_price <= 0:
                rec[f"future_max_ret_{n}d"] = np.nan
                rec[f"future_min_ret_{n}d"] = np.nan
            else:
                rec[f"future_max_ret_{n}d"] = float(w["close"].max() / sell_price - 1.0)
                rec[f"future_min_ret_{n}d"] = float(w["close"].min() / sell_price - 1.0)
        rows.append(rec)
    out = pd.DataFrame(rows)
    out.to_csv(output_dir / "exit_opportunity.csv", index=False, encoding="utf-8-sig")
    by_reason = _summarize(out, "exit_reason")
    by_reason.to_csv(output_dir / "summary_by_exit_reason.csv", index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "trades": str(trades_path),
        "end_date": end_date,
        "exit_rows": int(len(out)),
        "summary_by_exit_reason": by_reason.replace({np.nan: None}).to_dict("records"),
        "outputs": {"detail": "exit_opportunity.csv", "by_reason": "summary_by_exit_reason.csv"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze future opportunity after G2 exits.")
    parser.add_argument("--trades", default=str(DEFAULT_TRADES))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--end-date", default="2026-05-21")
    args = parser.parse_args()
    payload = run(Path(args.trades), Path(args.output_dir), str(args.end_date))
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
