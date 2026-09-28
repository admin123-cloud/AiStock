from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default  # noqa: E402
from utils.market_warehouse import clickhouse_client  # noqa: E402

DEFAULT_EVENT_DATASET = REPO_ROOT / "reports" / "gen2_event_study_full" / "v4_event_dataset.parquet"
DEFAULT_SIGNALS = REPO_ROOT / "reports" / "gen2_intraday_normal_backtest_realistic_exit" / "30m_before_confirm" / "signals.csv"
DEFAULT_TRADES = REPO_ROOT / "reports" / "gen2_intraday_normal_backtest_realistic_exit" / "30m_before_confirm" / "trades.csv"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_v4_factor_profile"

FACTOR_COLUMNS = [
    "mom5",
    "mom10",
    "mom20",
    "v4_score",
    "vol_ratio",
    "amt20",
    "vol10",
    "ma20_gap",
    "ma60_gap",
    "volatility20",
    "amplitude20",
    "turnover20",
    "turnover_z20",
    "amount_ratio5",
    "volume_ratio5",
    "intraday_strength",
    "close_position",
    "reversal3",
]

FACTOR_FAMILIES = {
    "momentum_trend": ["mom5", "mom10", "mom20", "v4_score", "ma20_gap", "ma60_gap"],
    "volume_price": ["vol_ratio", "amount_ratio5", "volume_ratio5", "intraday_strength", "close_position"],
    "liquidity_turnover": ["amt20", "turnover20", "turnover_z20"],
    "volatility_crowding": ["vol10", "volatility20", "amplitude20"],
    "reversal_repair": ["reversal3"],
}


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _load_events(path: Path, start_date: str, end_date: str) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d = df[(df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)].copy()
    d = d.dropna(subset=["trade_date", "code"]).copy()
    d["code"] = d["code"].astype(str)
    return d.reset_index(drop=True)


def _load_daily_factors(codes: list[str], start_date: str, end_date: str) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    quoted = ", ".join([f"'{code}'" for code in sorted(set(codes))])
    ch = clickhouse_client()
    start_ts = (pd.Timestamp(start_date) - pd.Timedelta(days=140)).strftime("%Y-%m-%d")
    df = ch.query_df(
        f"""
        SELECT code, trade_date, open, high, low, close, volume, amount, turnover_rate
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN '{start_ts}' AND '{end_date}'
        ORDER BY code, trade_date
        """
    )
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close", "volume", "amount", "turnover_rate"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    d = df.sort_values(["code", "trade_date"]).reset_index(drop=True)
    g = d.groupby("code", sort=False)
    ret = g["close"].pct_change()
    d["ma20"] = g["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    d["ma60"] = g["close"].transform(lambda s: s.rolling(60, min_periods=60).mean())
    d["ma20_gap"] = d["close"] / d["ma20"] - 1.0
    d["ma60_gap"] = d["close"] / d["ma60"] - 1.0
    d["volatility20"] = ret.groupby(d["code"]).transform(lambda s: s.rolling(20, min_periods=20).std())
    d["amplitude20"] = ((d["high"] - d["low"]) / d["close"]).groupby(d["code"]).transform(lambda s: s.rolling(20, min_periods=20).mean())
    d["turnover20"] = g["turnover_rate"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    turnover_mean20 = g["turnover_rate"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    turnover_std20 = g["turnover_rate"].transform(lambda s: s.rolling(20, min_periods=20).std())
    d["turnover_z20"] = (d["turnover_rate"] - turnover_mean20) / turnover_std20.replace(0, np.nan)
    d["amount_ratio5"] = d["amount"] / g["amount"].transform(lambda s: s.shift(1).rolling(5, min_periods=5).mean())
    d["volume_ratio5"] = d["volume"] / g["volume"].transform(lambda s: s.shift(1).rolling(5, min_periods=5).mean())
    d["intraday_strength"] = d["close"] / d["open"] - 1.0
    d["close_position"] = (d["close"] - d["low"]) / (d["high"] - d["low"]).replace(0, np.nan)
    d["reversal3"] = -1.0 * (d["close"] / g["close"].shift(3) - 1.0)
    keep = ["code", "trade_date"] + [c for c in FACTOR_COLUMNS if c not in {"mom5", "mom10", "mom20", "v4_score", "vol_ratio", "amt20", "vol10"}]
    return d[d["trade_date"] >= start_date][keep].reset_index(drop=True)


def _merge_factor_frame(base: pd.DataFrame, daily_factors: pd.DataFrame) -> pd.DataFrame:
    d = base.merge(daily_factors, on=["code", "trade_date"], how="left")
    for col in FACTOR_COLUMNS:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d


def _rank_group(rank: Any) -> str:
    try:
        r = int(float(rank))
    except Exception:
        return "unranked"
    if r <= 10:
        return "top10"
    if r <= 30:
        return "top30"
    if r <= 100:
        return "rank31_100"
    return "rank_gt100"


def _score_bucket(score: Any) -> str:
    try:
        s = float(score)
    except Exception:
        return "unknown"
    if s >= 0.85:
        return "score_085_plus"
    if s >= 0.75:
        return "score_075_085"
    return "score_lt075"


def _factor_summary(frame: pd.DataFrame, group_cols: list[str], out_path: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for keys, group in frame.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        row_base = {col: key for col, key in zip(group_cols, keys)}
        row_base["count"] = int(len(group))
        row_base["codes"] = int(group["code"].nunique()) if "code" in group.columns else 0
        for factor in FACTOR_COLUMNS:
            if factor not in group.columns:
                continue
            x = pd.to_numeric(group[factor], errors="coerce").dropna()
            if x.empty:
                continue
            row = dict(row_base)
            row.update(
                {
                    "factor": factor,
                    "mean": float(x.mean()),
                    "median": float(x.median()),
                    "p25": float(x.quantile(0.25)),
                    "p75": float(x.quantile(0.75)),
                    "coverage": float(len(x) / max(len(group), 1)),
                }
            )
            rows.append(row)
    out = pd.DataFrame(rows)
    out.to_csv(out_path, index=False, encoding="utf-8-sig")
    return out


def _load_signal_outcomes(signals_path: Path, trades_path: Path) -> pd.DataFrame:
    signals = pd.read_csv(signals_path)
    signals["trade_date"] = pd.to_datetime(signals["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    signals["entry_date"] = pd.to_datetime(signals["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    signals["confirm_datetime"] = pd.to_datetime(signals["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    signals["code"] = signals["code"].astype(str)
    trades = pd.read_csv(trades_path)
    if trades.empty:
        signals["trade_return"] = np.nan
        signals["trade_success"] = False
        return signals
    trades["buy_datetime"] = pd.to_datetime(trades["buy_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    trades["code"] = trades["code"].astype(str)
    trades["pnl"] = pd.to_numeric(trades["pnl"], errors="coerce")
    trades["capital"] = pd.to_numeric(trades["capital"], errors="coerce")
    agg = trades.groupby(["code", "buy_datetime"], as_index=False).agg(pnl=("pnl", "sum"), capital=("capital", "sum"))
    agg["trade_return"] = agg["pnl"] / agg["capital"].replace(0, np.nan)
    out = signals.merge(agg.rename(columns={"buy_datetime": "confirm_datetime"}), on=["code", "confirm_datetime"], how="left")
    out["trade_success"] = pd.to_numeric(out["trade_return"], errors="coerce") > 0
    out["fwd5_success"] = pd.to_numeric(out.get("entry_fwd_ret_5d"), errors="coerce") > 0
    out["fwd10_success"] = pd.to_numeric(out.get("entry_fwd_ret_10d"), errors="coerce") > 0
    return out


def _success_failure_table(frame: pd.DataFrame, outcome_col: str, out_path: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    ok = frame[frame[outcome_col] == True].copy()  # noqa: E712
    bad = frame[frame[outcome_col] == False].copy()  # noqa: E712
    for factor in FACTOR_COLUMNS:
        if factor not in frame.columns:
            continue
        x_ok = pd.to_numeric(ok[factor], errors="coerce").dropna()
        x_bad = pd.to_numeric(bad[factor], errors="coerce").dropna()
        if x_ok.empty or x_bad.empty:
            continue
        pooled = pd.concat([x_ok, x_bad])
        spread = float(x_ok.mean() - x_bad.mean())
        pooled_std = float(pooled.std())
        rows.append(
            {
                "outcome": outcome_col,
                "factor": factor,
                "success_count": int(len(x_ok)),
                "failure_count": int(len(x_bad)),
                "success_mean": float(x_ok.mean()),
                "failure_mean": float(x_bad.mean()),
                "mean_spread": spread,
                "effect_size": spread / pooled_std if pooled_std > 0 else np.nan,
                "success_median": float(x_ok.median()),
                "failure_median": float(x_bad.median()),
            }
        )
    out = pd.DataFrame(rows).sort_values("effect_size", key=lambda s: s.abs(), ascending=False)
    out.to_csv(out_path, index=False, encoding="utf-8-sig")
    return out


def _family_table(frame: pd.DataFrame, target_col: str, out_path: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    target = pd.to_numeric(frame[target_col], errors="coerce")

    def _rank_ic(x: pd.Series, y: pd.Series) -> float:
        valid = x.notna() & y.notna()
        if valid.sum() < 20:
            return np.nan
        xr = x[valid].rank(method="average")
        yr = y[valid].rank(method="average")
        return float(xr.corr(yr))

    for family, factors in FACTOR_FAMILIES.items():
        available = [factor for factor in factors if factor in frame.columns]
        if not available:
            continue
        family_scores = []
        for factor in available:
            x = pd.to_numeric(frame[factor], errors="coerce")
            valid = x.notna() & target.notna()
            corr = _rank_ic(x, target)
            q = x.rank(pct=True)
            family_scores.append(q)
            rows.append({"level": "factor", "family": family, "factor": factor, "target": target_col, "rank_ic": corr, "valid_count": int(valid.sum())})
        composite = pd.concat(family_scores, axis=1).mean(axis=1)
        valid = composite.notna() & target.notna()
        corr = _rank_ic(composite, target)
        rows.append({"level": "family", "family": family, "factor": "__family_mean_rank__", "target": target_col, "rank_ic": corr, "valid_count": int(valid.sum())})
    out = pd.DataFrame(rows).sort_values(["level", "rank_ic"], ascending=[True, False])
    out.to_csv(out_path, index=False, encoding="utf-8-sig")
    return out


def _write_report(output_dir: Path, payload: dict[str, Any], success_table: pd.DataFrame, family_table: pd.DataFrame) -> None:
    lines = [
        "# V4 / Pullback Factor Profile",
        "",
        "本报告先使用当前仓库可直接计算的 Alpha191 风格代理因子，而不是完整官方 Alpha191 公式。",
        "",
        "## Data",
        "",
        f"- Window: `{payload['start_date']}` to `{payload['end_date']}`",
        f"- V4 rows: `{payload['v4_rows']}`",
        f"- Pullback signal rows: `{payload['signal_rows']}`",
        f"- Pullback traded rows: `{payload['traded_signal_rows']}`",
        "",
        "## Success vs Failure",
        "",
        "| factor | success_mean | failure_mean | spread | effect |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for row in success_table.head(12).to_dict("records"):
        lines.append(
            f"| {row['factor']} | {row['success_mean']:.4f} | {row['failure_mean']:.4f} | {row['mean_spread']:.4f} | {row['effect_size']:.3f} |"
        )
    lines.extend(["", "## Factor Families", "", "| family | target | rank_ic | n |", "| --- | --- | ---: | ---: |"])
    fam = family_table[family_table["level"] == "family"].copy()
    for row in fam.sort_values("rank_ic", key=lambda s: s.abs(), ascending=False).to_dict("records"):
        lines.append(f"| {row['family']} | {row['target']} | {row['rank_ic']:.4f} | {int(row['valid_count'])} |")
    lines.extend(
        [
            "",
            "## First Read",
            "",
            "- V4 池本身的强弱不应该只看买点回测，而要看 V4 分层、排名分层和因子族暴露。",
            "- 当前回踩确认样本更适合用来找“成功回踩 vs 失败回踩”的差异，而不是直接再次调参。",
            "- 如果某个因子族在 trade_return 与 fwd_ret 上方向一致，才值得进入下一轮规则候选。",
        ]
    )
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def run(event_dataset: Path, signals_path: Path, trades_path: Path, output_dir: Path, start_date: str, end_date: str) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    events = _load_events(event_dataset, start_date, end_date)
    daily_factors = _load_daily_factors(events["code"].dropna().astype(str).unique().tolist(), start_date, end_date)
    v4 = _merge_factor_frame(events, daily_factors)
    v4["rank_group"] = v4["v4_rank"].apply(_rank_group)
    v4["score_bucket"] = v4["v4_score"].apply(_score_bucket)
    v4.to_parquet(output_dir / "v4_factor_labeled.parquet", index=False)
    v4.head(50000).to_csv(output_dir / "v4_factor_labeled_sample.csv", index=False, encoding="utf-8-sig")

    v4_summary = _factor_summary(v4, ["rank_group", "entry_pass"], output_dir / "v4_factor_summary_by_rank.csv")
    _factor_summary(v4, ["legacy_regime", "rank_group"], output_dir / "v4_factor_summary_by_regime.csv")

    signals = _load_signal_outcomes(signals_path, trades_path)
    pullback = _merge_factor_frame(signals, daily_factors)
    pullback.to_parquet(output_dir / "pullback_signal_factor_labeled.parquet", index=False)
    pullback.to_csv(output_dir / "pullback_signal_factor_labeled.csv", index=False, encoding="utf-8-sig")

    success_table = _success_failure_table(pullback[pullback["trade_return"].notna()].copy(), "trade_success", output_dir / "pullback_success_failure_trade.csv")
    _success_failure_table(pullback, "fwd5_success", output_dir / "pullback_success_failure_fwd5.csv")
    family_trade = _family_table(pullback[pullback["trade_return"].notna()].copy(), "trade_return", output_dir / "factor_family_trade_return.csv")
    _family_table(pullback, "entry_fwd_ret_5d", output_dir / "factor_family_fwd5_return.csv")
    _family_table(v4, "fwd_ret_5d", output_dir / "factor_family_v4_fwd5_return.csv")

    payload = {
        "schema_version": 1,
        "start_date": start_date,
        "end_date": end_date,
        "event_dataset": str(event_dataset),
        "signals": str(signals_path),
        "trades": str(trades_path),
        "v4_rows": int(len(v4)),
        "v4_codes": int(v4["code"].nunique()),
        "signal_rows": int(len(pullback)),
        "traded_signal_rows": int(pullback["trade_return"].notna().sum()),
        "factor_columns": FACTOR_COLUMNS,
        "outputs": {
            "v4_factor_labeled": "v4_factor_labeled.parquet",
            "v4_factor_summary_by_rank": "v4_factor_summary_by_rank.csv",
            "pullback_signal_factor_labeled": "pullback_signal_factor_labeled.parquet",
            "pullback_success_failure_trade": "pullback_success_failure_trade.csv",
            "factor_family_trade_return": "factor_family_trade_return.csv",
            "findings": "findings.md",
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(output_dir, payload, success_table, family_trade)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile V4 pool and pullback confirmations with Alpha191-style proxy factors.")
    parser.add_argument("--event-dataset", default=str(DEFAULT_EVENT_DATASET))
    parser.add_argument("--signals", default=str(DEFAULT_SIGNALS))
    parser.add_argument("--trades", default=str(DEFAULT_TRADES))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    args = parser.parse_args()
    payload = run(
        event_dataset=Path(args.event_dataset),
        signals_path=Path(args.signals),
        trades_path=Path(args.trades),
        output_dir=Path(args.output_dir),
        start_date=str(args.start_date),
        end_date=str(args.end_date),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
