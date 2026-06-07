from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.generate_behavior_theme_clusters_v1 import _code6, _latest_trade_dates
from utils.market_warehouse import clickhouse_query_df


OUT_DIR = ROOT / "reports" / "behavior_theme_candidate_source_v1"
THEME_DIR = ROOT / "reports" / "behavior_theme_clusters_v1"
THEME_CLUSTERS = THEME_DIR / "theme_clusters.csv"
THEME_MEMBERS = THEME_DIR / "theme_cluster_members.csv"
G2_CURRENT = ROOT / "reports" / "gen2_v2_live_smoke_current" / "source.csv"


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig")


def _load_theme() -> tuple[pd.DataFrame, pd.DataFrame]:
    clusters = _read_csv(THEME_CLUSTERS)
    members = _read_csv(THEME_MEMBERS)
    if clusters.empty or members.empty:
        raise RuntimeError("behavior theme clusters are missing; run generate_behavior_theme_clusters_v1.py first")
    return clusters, members


def _load_daily(codes6: list[str], end_date: str, lookback_days: int) -> pd.DataFrame:
    dates = _latest_trade_dates(end_date, int(lookback_days))
    start, end = dates[0], dates[-1]
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
        [start, end],
    )
    if df.empty:
        raise RuntimeError("daily query returned no rows for theme members")
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["code6"] = df["code"].map(_code6)
    df = df[df["code6"].isin(set(codes6))].copy()
    if df.empty:
        raise RuntimeError("daily query returned no matching rows for normalized theme member codes")
    for col in ["open", "high", "low", "close", "change_pct", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["trade_date", "code6", "close"])


def _stock_features(daily: pd.DataFrame, target_date: str) -> pd.DataFrame:
    rows = []
    for code6, part in daily.groupby("code6"):
        part = part.sort_values("trade_date").copy()
        if part.empty:
            continue
        latest = part.iloc[-1]
        close = part["close"].astype(float)
        high = part["high"].astype(float)
        low = part["low"].astype(float)
        amount = part["amount"].astype(float)

        def ret_n(n: int) -> float:
            if len(close) <= n:
                return np.nan
            base = close.iloc[-(n + 1)]
            return float(close.iloc[-1] / base - 1.0) if base > 0 else np.nan

        low60 = float(low.tail(60).min()) if len(low) else np.nan
        high20 = float(high.tail(20).max()) if len(high) else np.nan
        ma20 = float(close.tail(20).mean()) if len(close) >= 5 else np.nan
        ma60 = float(close.tail(60).mean()) if len(close) >= 20 else np.nan
        amount20 = float(amount.tail(20).median()) if len(amount) >= 5 else np.nan
        amount60 = float(amount.tail(60).median()) if len(amount) >= 20 else np.nan
        last_close = float(close.iloc[-1])
        rows.append(
            {
                "code6": str(code6),
                "trade_date": target_date,
                "close": last_close,
                "ret60": ret_n(60),
                "ret20": ret_n(20),
                "ret10": ret_n(10),
                "ret5": ret_n(5),
                "change_pct": float(latest["change_pct"]) if pd.notna(latest["change_pct"]) else np.nan,
                "amount": float(latest["amount"]) if pd.notna(latest["amount"]) else np.nan,
                "amount_ratio20_60": amount20 / amount60 if amount60 and amount60 > 0 else np.nan,
                "runup_from_60d_low": last_close / low60 - 1.0 if low60 and low60 > 0 else np.nan,
                "drawdown_from_20d_high": last_close / high20 - 1.0 if high20 and high20 > 0 else np.nan,
                "close_above_ma20": bool(last_close >= ma20) if pd.notna(ma20) else False,
                "ma20_above_ma60": bool(ma20 >= ma60) if pd.notna(ma20) and pd.notna(ma60) else False,
            }
        )
    return pd.DataFrame(rows)


def _score_candidates(members: pd.DataFrame, clusters: pd.DataFrame, features: pd.DataFrame) -> pd.DataFrame:
    m = members.copy()
    m["code6"] = m["code6"].astype(str).str.zfill(6)
    clusters_keep = clusters[["cluster_id", "theme_label", "theme_score", "member_count", "avg_ret_window", "top_members"]].copy()
    out = m.merge(clusters_keep, on=["cluster_id", "theme_label", "theme_score"], how="left", suffixes=("", "_cluster"))
    out = out.merge(features, on="code6", how="left", suffixes=("_member", ""))

    for col in [
        "candidate_score",
        "theme_score",
        "ret60",
        "ret20",
        "ret10",
        "ret5",
        "amount_ratio20_60",
        "runup_from_60d_low",
        "drawdown_from_20d_high",
    ]:
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out["momentum_score"] = (
        out["ret20"].rank(pct=True).fillna(0) * 30
        + out["ret10"].rank(pct=True).fillna(0) * 25
        + out["ret5"].rank(pct=True).fillna(0) * 15
    )
    out["volume_score"] = out["amount_ratio20_60"].clip(0, 4).rank(pct=True).fillna(0) * 15
    out["structure_score"] = (
        out["close_above_ma20"].astype(float) * 8
        + out["ma20_above_ma60"].astype(float) * 7
    )
    out["risk_penalty"] = 0.0
    out.loc[out["runup_from_60d_low"] > 1.2, "risk_penalty"] += 10.0
    out.loc[out["drawdown_from_20d_high"] < -0.12, "risk_penalty"] += 8.0
    out.loc[out["ret5"] > 0.22, "risk_penalty"] += 8.0
    out["theme_candidate_score"] = (
        out["theme_score"].fillna(0) * 0.25
        + out["candidate_score"].fillna(0) * 0.20
        + out["momentum_score"].fillna(0) * 0.25
        + out["volume_score"].fillna(0) * 0.15
        + out["structure_score"].fillna(0) * 0.15
        - out["risk_penalty"].fillna(0)
    )
    out["risk_label"] = "normal"
    out.loc[out["runup_from_60d_low"] > 1.2, "risk_label"] = "high_runup"
    out.loc[out["drawdown_from_20d_high"] < -0.12, "risk_label"] = "weak_after_high"
    out.loc[out["ret5"] > 0.22, "risk_label"] = "short_term_hot"
    out["research_action"] = "observe_only"
    out.loc[(out["theme_candidate_score"] >= 55) & out["risk_label"].eq("normal"), "research_action"] = "priority_watch"
    out.loc[out["risk_label"].ne("normal"), "research_action"] = "wait_pullback_or_confirm"

    g2 = _read_csv(G2_CURRENT)
    if not g2.empty and "code" in g2.columns:
        g2_codes = set(g2["code"].map(_code6).astype(str).str.zfill(6))
        out["in_g2_current"] = out["code6"].isin(g2_codes)
    else:
        out["in_g2_current"] = False

    out = out.sort_values(["theme_candidate_score", "theme_score", "candidate_score"], ascending=False).reset_index(drop=True)
    out["rank"] = np.arange(1, len(out) + 1)
    return out


def _write_outputs(target_date: str, candidates: pd.DataFrame, clusters: pd.DataFrame, args: argparse.Namespace) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cand_path = OUT_DIR / "theme_candidate_source.csv"
    cluster_path = OUT_DIR / "theme_clusters_used.csv"
    report_path = OUT_DIR / "REPORT.md"
    json_path = OUT_DIR / "summary.json"
    candidates.to_csv(cand_path, index=False, encoding="utf-8-sig")
    clusters.to_csv(cluster_path, index=False, encoding="utf-8-sig")

    payload = {
        "target_date": target_date,
        "candidate_count": int(len(candidates)),
        "theme_count": int(clusters["cluster_id"].nunique() if not clusters.empty else 0),
        "priority_watch_count": int((candidates["research_action"] == "priority_watch").sum()) if not candidates.empty else 0,
        "candidate_csv": str(cand_path),
        "cluster_csv": str(cluster_path),
        "report": str(report_path),
        "research_only": True,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def pct(x: Any) -> str:
        if pd.isna(x):
            return ""
        return f"{float(x):.2%}"

    lines = [
        "# 行为主题簇独立候选源 V1 research_only",
        "",
        f"- 目标日期：`{target_date}`",
        f"- 主题数：{payload['theme_count']}",
        f"- 候选数：{payload['candidate_count']}",
        f"- 优先观察数：{payload['priority_watch_count']}",
        "- 用途：从主题主线里反向选股，作为 G2/V4 之外的研究候选源；不是正式买点。",
        "- 解释：`priority_watch` 只是优先观察，仍需等待正式买点、盘中确认和市场门禁。",
        "",
        "## 候选排名",
        "",
        "| 排名 | 股票 | 主题 | 分数 | 动作 | 风险 | 20日 | 10日 | 5日 | 60日低点涨幅 | 20日高点回撤 | G2当前 |",
        "|---:|---|---|---:|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in candidates.head(int(args.top_n)).itertuples(index=False):
        lines.append(
            f"| {int(row.rank)} | {row.display_name} `{row.code6}` | {row.theme_label} | "
            f"{float(row.theme_candidate_score):.2f} | {row.research_action} | {row.risk_label} | "
            f"{pct(row.ret20)} | {pct(row.ret10)} | {pct(row.ret5)} | "
            f"{pct(row.runup_from_60d_low)} | {pct(row.drawdown_from_20d_high)} | {bool(row.in_g2_current)} |"
        )
    lines.extend(
        [
            "",
            "## 结论",
            "",
            "- 这张表解决的是“主线里谁值得盯”，不是“现在能不能买”。",
            "- 如果候选已经处于 `short_term_hot` 或 `high_runup`，更适合等分歧回踩或 G2/V4 正式确认，不适合直接追。",
            "- 下一步可以把 `priority_watch` 与 G2/V4 当日观察池交叉，形成主题主线观察池。",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(str(report_path))
    print(str(cand_path))
    print(f"candidate_count={len(candidates)} priority_watch_count={payload['priority_watch_count']}")
    cols = ["rank", "display_name", "code6", "theme_label", "theme_candidate_score", "research_action", "risk_label"]
    print(candidates[cols].head(int(args.top_n)).to_string(index=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate independent candidate source from behavior theme clusters.")
    parser.add_argument("--target-date", default=None)
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--top-n", type=int, default=30)
    args = parser.parse_args()

    clusters, members = _load_theme()
    target_date = args.target_date or str(clusters.get("target_date", pd.Series(dtype=str)).dropna().max() or "")
    if target_date.lower() in {"nan", "nat", "none", "null"}:
        target_date = ""
    if not target_date:
        summary_path = THEME_DIR / "summary.json"
        if summary_path.exists():
            target_date = str(json.loads(summary_path.read_text(encoding="utf-8")).get("target_date", ""))
    if not target_date:
        target_date = _latest_trade_dates(None, 1)[-1]

    codes6 = sorted(members["code6"].astype(str).str.zfill(6).unique().tolist())
    daily = _load_daily(codes6, target_date, args.lookback_days)
    features = _stock_features(daily, target_date)
    candidates = _score_candidates(members, clusters, features)
    _write_outputs(target_date, candidates, clusters, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
