from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.generate_behavior_theme_clusters_v1 import _code6, _latest_trade_dates
from utils.market_warehouse import clickhouse_query_df


OUT_DIR = ROOT / "reports" / "behavior_theme_candidate_source_backtest_v1"


def _pct(x: float) -> str:
    if pd.isna(x):
        return ""
    return f"{float(x):.2%}"


def _load_daily(start_date: str, end_date: str) -> pd.DataFrame:
    df = clickhouse_query_df(
        """
        SELECT
            code,
            trade_date,
            open,
            high,
            low,
            close,
            change_pct,
            amount
        FROM kline_daily
        WHERE trade_date >= ?
          AND trade_date <= ?
        ORDER BY code, trade_date
        """,
        [start_date, end_date],
    )
    if df.empty:
        raise RuntimeError("kline_daily query returned no rows")
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["code6"] = df["code"].map(_code6).astype(str).str.zfill(6)
    df = df[df["code6"].str.match(r"^(00|30|60|68)\d{4}$", na=False)].copy()
    for col in ["open", "high", "low", "close", "change_pct", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["trade_date", "code6", "close"])


def _latest_before_or_equal(dates: list[str], date: str) -> str:
    usable = [d for d in dates if d <= date]
    if not usable:
        raise RuntimeError(f"no trading date before {date}")
    return usable[-1]


def _future_date(dates: list[str], date: str, horizon: int) -> str | None:
    if date not in dates:
        return None
    idx = dates.index(date)
    if idx + horizon >= len(dates):
        return None
    return dates[idx + horizon]


def _window_features(window: pd.DataFrame, target_date: str, max_stocks: int, min_amount: float) -> pd.DataFrame:
    rows = []
    for code6, part in window.groupby("code6"):
        part = part.sort_values("trade_date")
        if len(part) < 18:
            continue
        close = part["close"].astype(float)
        amount = part["amount"].astype(float)
        if close.iloc[0] <= 0:
            continue
        ret_window = float(close.iloc[-1] / close.iloc[0] - 1.0)
        ret10 = float(close.iloc[-1] / close.iloc[-11] - 1.0) if len(close) >= 11 and close.iloc[-11] > 0 else np.nan
        ret5 = float(close.iloc[-1] / close.iloc[-6] - 1.0) if len(close) >= 6 and close.iloc[-6] > 0 else np.nan
        amount_median = float(amount.tail(20).median())
        if not amount_median or amount_median < min_amount:
            continue
        amount_ratio = float(amount.tail(5).median() / amount.tail(20).median()) if amount.tail(20).median() > 0 else np.nan
        rows.append(
            {
                "trade_date": target_date,
                "code6": code6,
                "ret_window": ret_window,
                "ret10": ret10,
                "ret5": ret5,
                "strong_days": int((part["change_pct"] >= 3.0).sum()),
                "rise_days": int((part["change_pct"] > 0).sum()),
                "amount_median": amount_median,
                "amount_ratio": amount_ratio,
            }
        )
    feat = pd.DataFrame(rows)
    if feat.empty:
        return feat
    feat["base_score"] = (
        feat["ret_window"].rank(pct=True) * 35
        + feat["ret10"].rank(pct=True).fillna(0) * 20
        + feat["ret5"].rank(pct=True).fillna(0) * 10
        + feat["strong_days"].rank(pct=True) * 15
        + feat["rise_days"].rank(pct=True) * 10
        + feat["amount_ratio"].clip(0, 4).rank(pct=True).fillna(0) * 10
    )
    return feat.sort_values("base_score", ascending=False).head(max_stocks).reset_index(drop=True)


def _make_clusters(window: pd.DataFrame, feat: pd.DataFrame, min_corr: float, min_size: int) -> pd.DataFrame:
    if feat.empty:
        return pd.DataFrame()
    pivot = window[window["code6"].isin(set(feat["code6"]))].pivot_table(
        index="trade_date",
        columns="code6",
        values="change_pct",
        aggfunc="last",
    )
    corr = pivot.corr(min_periods=12)
    used: set[str] = set()
    clusters = []
    seeds = feat["code6"].tolist()
    feat_idx = feat.set_index("code6")
    for seed in seeds:
        if seed in used or seed not in corr.columns:
            continue
        peers = corr[seed].dropna()
        members = peers[peers >= min_corr].index.tolist()
        members = [c for c in members if c not in used]
        if len(members) < min_size:
            continue
        sub = feat_idx.loc[members].copy()
        theme_score = float(sub["base_score"].mean())
        clusters.append(
            {
                "cluster_id": len(clusters) + 1,
                "seed_code6": seed,
                "members": members,
                "member_count": len(members),
                "theme_score": theme_score,
                "avg_ret_window": float(sub["ret_window"].mean()),
            }
        )
        used.update(members)
    return pd.DataFrame(clusters)


def _candidate_rows(trade_date: str, clusters: pd.DataFrame, feat: pd.DataFrame, top_n: int, score_threshold: float) -> pd.DataFrame:
    if clusters.empty or feat.empty:
        return pd.DataFrame()
    feat_idx = feat.set_index("code6")
    rows = []
    for row in clusters.itertuples(index=False):
        members = list(row.members)
        sub = feat_idx.loc[members].copy()
        sub["theme_score"] = float(row.theme_score)
        sub["cluster_id"] = int(row.cluster_id)
        sub["theme_candidate_score"] = sub["theme_score"] * 0.45 + sub["base_score"] * 0.55
        sub = sub.sort_values("theme_candidate_score", ascending=False).head(top_n)
        for code6, r in sub.iterrows():
            if float(r["theme_candidate_score"]) < score_threshold:
                continue
            rows.append(
                {
                    "trade_date": trade_date,
                    "cluster_id": int(row.cluster_id),
                    "code6": code6,
                    "theme_score": float(row.theme_score),
                    "base_score": float(r["base_score"]),
                    "theme_candidate_score": float(r["theme_candidate_score"]),
                    "ret_window": float(r["ret_window"]),
                    "ret10": float(r["ret10"]) if pd.notna(r["ret10"]) else np.nan,
                    "ret5": float(r["ret5"]) if pd.notna(r["ret5"]) else np.nan,
                    "amount_median": float(r["amount_median"]),
                }
            )
    return pd.DataFrame(rows)


def _attach_forward_returns(candidates: pd.DataFrame, daily: pd.DataFrame, dates: list[str], horizons: list[int]) -> pd.DataFrame:
    if candidates.empty:
        return candidates
    price = daily.pivot_table(index="trade_date", columns="code6", values="close", aggfunc="last")
    out = candidates.copy()
    for h in horizons:
        vals = []
        for r in out.itertuples(index=False):
            fd = _future_date(dates, r.trade_date, h)
            if fd is None or r.code6 not in price.columns:
                vals.append(np.nan)
                continue
            now = price.at[r.trade_date, r.code6] if r.trade_date in price.index else np.nan
            fut = price.at[fd, r.code6] if fd in price.index else np.nan
            ret = float(fut / now - 1.0) if pd.notna(now) and pd.notna(fut) and now > 0 else np.nan
            if pd.notna(ret) and abs(ret) > 2.0:
                ret = np.nan
            vals.append(ret)
        out[f"ret_fwd_{h}d"] = vals
    return out


def _summarize(candidates: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    if candidates.empty:
        rows = []
        for name in ["all_theme_candidates", "score_ge_55", "score_ge_60", "top3_per_day"]:
            row = {"bucket": name, "trade_count": 0, "date_count": 0}
            for h in horizons:
                row[f"avg_{h}d"] = np.nan
                row[f"median_{h}d"] = np.nan
                row[f"win_{h}d"] = np.nan
                row[f"n_{h}d"] = 0
            rows.append(row)
        return pd.DataFrame(rows)
    rows = []
    for name, part in [
        ("all_theme_candidates", candidates),
        ("score_ge_55", candidates[candidates["theme_candidate_score"] >= 55]),
        ("score_ge_60", candidates[candidates["theme_candidate_score"] >= 60]),
        ("top3_per_day", candidates.sort_values("theme_candidate_score", ascending=False).groupby("trade_date").head(3)),
    ]:
        row = {"bucket": name, "trade_count": int(len(part)), "date_count": int(part["trade_date"].nunique()) if not part.empty else 0}
        for h in horizons:
            col = f"ret_fwd_{h}d"
            s = part[col].dropna() if col in part.columns else pd.Series(dtype=float)
            row[f"avg_{h}d"] = float(s.mean()) if len(s) else np.nan
            row[f"median_{h}d"] = float(s.median()) if len(s) else np.nan
            row[f"win_{h}d"] = float((s > 0).mean()) if len(s) else np.nan
            row[f"n_{h}d"] = int(len(s))
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest behavior theme candidate source.")
    parser.add_argument("--start-date", default="2024-07-01")
    parser.add_argument("--end-date", default="2026-05-29")
    parser.add_argument("--lookback-days", type=int, default=20)
    parser.add_argument("--load-days", type=int, default=90)
    parser.add_argument("--step", type=int, default=5)
    parser.add_argument("--max-stocks", type=int, default=700)
    parser.add_argument("--min-amount", type=float, default=1_000_000)
    parser.add_argument("--min-corr", type=float, default=0.58)
    parser.add_argument("--min-size", type=int, default=5)
    parser.add_argument("--top-n-per-cluster", type=int, default=5)
    parser.add_argument("--score-threshold", type=float, default=55)
    args = parser.parse_args()

    all_dates = _latest_trade_dates(args.end_date, 800)
    start_trade = _latest_before_or_equal(all_dates, args.start_date)
    signal_dates = [d for d in all_dates if start_trade <= d <= args.end_date][:: max(1, int(args.step))]
    load_start = all_dates[max(0, all_dates.index(signal_dates[0]) - args.load_days)]
    load_end = _future_date(all_dates, signal_dates[-1], 60) or signal_dates[-1]
    daily = _load_daily(load_start, load_end)

    candidate_frames = []
    cluster_frames = []
    for d in signal_dates:
        idx = all_dates.index(d)
        win_dates = all_dates[max(0, idx - args.lookback_days + 1) : idx + 1]
        window = daily[daily["trade_date"].isin(win_dates)].copy()
        feat = _window_features(window, d, args.max_stocks, args.min_amount)
        clusters = _make_clusters(window, feat, args.min_corr, args.min_size)
        if not clusters.empty:
            c2 = clusters.copy()
            c2["trade_date"] = d
            c2["members"] = c2["members"].map(lambda x: ",".join(x))
            cluster_frames.append(c2)
        cand = _candidate_rows(d, clusters, feat, args.top_n_per_cluster, args.score_threshold)
        if not cand.empty:
            candidate_frames.append(cand)

    candidates = pd.concat(candidate_frames, ignore_index=True) if candidate_frames else pd.DataFrame()
    clusters_out = pd.concat(cluster_frames, ignore_index=True) if cluster_frames else pd.DataFrame()
    horizons = [5, 10, 20, 60]
    candidates = _attach_forward_returns(candidates, daily, all_dates, horizons)
    summary = _summarize(candidates, horizons)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candidates_path = OUT_DIR / "theme_candidate_trades.csv"
    clusters_path = OUT_DIR / "theme_clusters_rolling.csv"
    summary_path = OUT_DIR / "summary.csv"
    report_path = OUT_DIR / "REPORT.md"
    json_path = OUT_DIR / "summary.json"
    candidates.to_csv(candidates_path, index=False, encoding="utf-8-sig")
    clusters_out.to_csv(clusters_path, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")

    payload = {
        "start_date": args.start_date,
        "end_date": args.end_date,
        "signal_start": signal_dates[0] if signal_dates else None,
        "signal_end": signal_dates[-1] if signal_dates else None,
        "step": args.step,
        "signal_date_count": len(signal_dates),
        "candidate_count": int(len(candidates)),
        "cluster_count": int(len(clusters_out)),
        "lookback_days": args.lookback_days,
        "min_corr": args.min_corr,
        "min_size": args.min_size,
        "score_threshold": args.score_threshold,
        "research_only": True,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 行为主题候选源滚动回测 V1",
        "",
        f"- 回测区间：`{payload['start_date']}` 到 `{payload['end_date']}`",
        f"- 信号日期：`{payload['signal_start']}` 到 `{payload['signal_end']}`，每 {payload['step']} 个交易日取样一次",
        f"- 信号日期数：{payload['signal_date_count']}",
        f"- 候选笔数：{payload['candidate_count']}",
        f"- 主题簇事件数：{payload['cluster_count']}",
        f"- 参数：lookback_days={args.lookback_days}, min_corr={args.min_corr}, min_size={args.min_size}, score_threshold={args.score_threshold}",
        "- 约束：只使用信号日前 20 个交易日行为数据，前瞻收益只用于回测统计，不进入选股特征。",
        "- 定位：研究用主线候选源，不是正式买点，不包含滑点、手续费、涨跌停排队和盘中可见性处理。",
        "",
        "## 分组表现",
        "",
        "| 分组 | 笔数 | 日期数 | 5日均值 | 5日胜率 | 10日均值 | 10日胜率 | 20日均值 | 20日胜率 | 60日均值 | 60日胜率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in summary.itertuples(index=False):
        lines.append(
            f"| {r.bucket} | {int(r.trade_count)} | {int(r.date_count)} | "
            f"{_pct(r.avg_5d)} | {_pct(r.win_5d)} | {_pct(r.avg_10d)} | {_pct(r.win_10d)} | "
            f"{_pct(r.avg_20d)} | {_pct(r.win_20d)} | {_pct(r.avg_60d)} | {_pct(r.win_60d)} |"
        )
    lines.extend(
        [
            "",
            "## 初步解释",
            "",
            "- 如果 20日/60日表现明显强于 5日/10日，说明它更适合作为主线跟踪池，而不是短打买点。",
            "- 如果高分组优于全量组，说明主题候选分数有排序价值；如果没有，则需要重新设计风险惩罚和热度阈值。",
            "- 后续要接入 G2/V4 时，应先作为观察池加权或候选来源，不应直接放入正式买入规则。",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(str(report_path))
    print(str(summary_path))
    print(f"candidate_count={len(candidates)} signal_date_count={len(signal_dates)} cluster_count={len(clusters_out)}")
    print(summary.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
