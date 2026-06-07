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

from utils.market_warehouse import clickhouse_query_df


OUT_DIR = ROOT / "reports" / "behavior_theme_clusters_v1"


def _code6(value: Any) -> str:
    text = str(value or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    return text.zfill(6)[-6:]


def _is_a_stock_code(value: Any) -> bool:
    text = str(value or "").strip().upper()
    if "." not in text:
        return False
    code, market = text.split(".", 1)
    if market == "SH":
        return code.startswith(("600", "601", "603", "605", "688"))
    if market == "SZ":
        return code.startswith(("000", "001", "002", "003", "300", "301"))
    if market == "BJ":
        return code.startswith(("4", "8", "920"))
    return False


def _latest_trade_dates(target_date: str | None, lookback_days: int) -> list[str]:
    where = ""
    params: list[Any] = []
    if target_date:
        where = "WHERE trade_date <= ?"
        params.append(target_date)
    df = clickhouse_query_df(
        f"""
        SELECT DISTINCT trade_date
        FROM kline_daily
        {where}
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        [*params, int(lookback_days)],
    )
    if df.empty:
        raise RuntimeError("no trade dates from kline_daily")
    return sorted(pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d").dropna().tolist())


def _load_daily(dates: list[str]) -> pd.DataFrame:
    start, end = dates[0], dates[-1]
    df = clickhouse_query_df(
        """
        SELECT
            code,
            trade_date,
            close,
            change_pct,
            amount
        FROM kline_daily
        WHERE trade_date >= ?
          AND trade_date <= ?
          AND close > 0
        ORDER BY code, trade_date
        """,
        [start, end],
    )
    if df.empty:
        raise RuntimeError("daily query returned no rows")
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df[df["code"].map(_is_a_stock_code)].copy()
    df["code6"] = df["code"].map(_code6)
    for col in ["close", "change_pct", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["ret"] = df["change_pct"]
    if df["ret"].abs().median() > 1.0:
        df["ret"] = df["ret"] / 100.0
    return df.dropna(subset=["trade_date", "code6", "close", "ret"])


def _load_names(codes6: list[str]) -> pd.DataFrame:
    if not codes6:
        return pd.DataFrame(columns=["code6", "name"])
    placeholders = ",".join(["?"] * len(codes6))
    df = clickhouse_query_df(
        f"""
        SELECT code, name
        FROM stocks
        WHERE substring(code, 1, 6) IN ({placeholders})
        """,
        codes6,
    )
    if df.empty:
        return pd.DataFrame(columns=["code6", "name"])
    df = df.copy()
    df["code6"] = df["code"].map(_code6)
    return df[["code6", "name"]].drop_duplicates("code6")


def _load_sector_labels(codes6: list[str]) -> pd.DataFrame:
    if not codes6:
        return pd.DataFrame(columns=["code6", "level", "sector_code", "sector_name"])
    placeholders = ",".join(["?"] * len(codes6))
    df = clickhouse_query_df(
        f"""
        SELECT
            ss.stock_code AS stock_code,
            s.level AS level,
            s.code AS sector_code,
            s.name AS sector_name
        FROM sector_stocks ss
        INNER JOIN sectors s ON ss.sector_code = s.code
        WHERE s.type = 'industry'
          AND substring(ss.stock_code, 1, 6) IN ({placeholders})
        """,
        codes6,
    )
    if df.empty:
        return pd.DataFrame(columns=["code6", "level", "sector_code", "sector_name"])
    df = df.copy()
    df["code6"] = df["stock_code"].map(_code6)
    df["level"] = pd.to_numeric(df["level"], errors="coerce").astype("Int64")
    return df[["code6", "level", "sector_code", "sector_name"]].drop_duplicates()


def _make_stock_features(daily: pd.DataFrame, dates: list[str]) -> pd.DataFrame:
    close = daily.pivot_table(index="code6", columns="trade_date", values="close", aggfunc="last").reindex(columns=dates)
    ret = daily.pivot_table(index="code6", columns="trade_date", values="ret", aggfunc="last").reindex(columns=dates)
    amount = daily.pivot_table(index="code6", columns="trade_date", values="amount", aggfunc="last").reindex(columns=dates)
    valid_days = close.notna().sum(axis=1)
    last_close = close.iloc[:, -1]
    first_close = close.ffill(axis=1).iloc[:, 0]
    ret_all = last_close / first_close - 1.0
    ret5 = last_close / close.ffill(axis=1).iloc[:, max(0, len(dates) - 6)] - 1.0
    ret10 = last_close / close.ffill(axis=1).iloc[:, max(0, len(dates) - 11)] - 1.0
    strong_days = (ret >= 0.03).sum(axis=1)
    rise_days = (ret > 0).sum(axis=1)
    amount_median = amount.median(axis=1)
    amount_ratio = amount.iloc[:, -5:].median(axis=1) / amount.iloc[:, : max(5, len(dates) // 2)].median(axis=1)
    features = pd.DataFrame(
        {
            "code6": close.index,
            "valid_days": valid_days,
            "ret_window": ret_all,
            "ret10": ret10,
            "ret5": ret5,
            "strong_days": strong_days,
            "rise_days": rise_days,
            "amount_median": amount_median,
            "amount_ratio": amount_ratio.replace([np.inf, -np.inf], np.nan),
        }
    ).reset_index(drop=True)
    for col in ["ret_window", "ret10", "ret5", "amount_ratio"]:
        features[col] = pd.to_numeric(features[col], errors="coerce")
    features["candidate_score"] = (
        features["ret_window"].rank(pct=True).fillna(0) * 35
        + features["ret10"].rank(pct=True).fillna(0) * 25
        + features["ret5"].rank(pct=True).fillna(0) * 15
        + features["strong_days"].rank(pct=True).fillna(0) * 15
        + features["amount_ratio"].clip(0, 5).rank(pct=True).fillna(0) * 10
    )
    return features


def _select_candidates(features: pd.DataFrame, min_valid_days: int, max_stocks: int, min_amount: float) -> pd.DataFrame:
    f = features.copy()
    f = f[f["valid_days"] >= int(min_valid_days)].copy()
    f = f[f["amount_median"].fillna(0) >= float(min_amount)].copy()
    q_ret = f["ret_window"].quantile(0.65)
    q_score = f["candidate_score"].quantile(0.70)
    picked = f[
        (f["ret_window"] >= q_ret)
        | (f["ret10"] >= f["ret10"].quantile(0.70))
        | (f["candidate_score"] >= q_score)
        | (f["strong_days"] >= 3)
    ].copy()
    picked = picked[picked["ret_window"].fillna(-1) > -0.05].copy()
    return picked.sort_values("candidate_score", ascending=False).head(int(max_stocks)).reset_index(drop=True)


def _build_clusters(daily: pd.DataFrame, candidates: pd.DataFrame, dates: list[str], min_corr: float, min_size: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    ret = daily.pivot_table(index="code6", columns="trade_date", values="ret", aggfunc="last").reindex(columns=dates)
    codes = candidates["code6"].astype(str).tolist()
    if len(codes) < int(min_size):
        return pd.DataFrame(), pd.DataFrame()
    mat = ret.reindex(codes).fillna(0.0).to_numpy(dtype=float)
    if len(codes) == 0:
        return pd.DataFrame(), pd.DataFrame()
    mat = mat - mat.mean(axis=1, keepdims=True)
    std = mat.std(axis=1, keepdims=True)
    std[std == 0] = 1.0
    z = mat / std
    corr = np.corrcoef(z)
    corr = np.nan_to_num(corr, nan=0.0, posinf=0.0, neginf=0.0)

    score_map = dict(zip(candidates["code6"], candidates["candidate_score"]))
    assigned: set[str] = set()
    clusters: list[dict[str, Any]] = []
    members: list[dict[str, Any]] = []
    sorted_codes = candidates.sort_values("candidate_score", ascending=False)["code6"].astype(str).tolist()
    idx_map = {code: i for i, code in enumerate(codes)}
    cluster_id = 0
    for seed in sorted_codes:
        if seed in assigned:
            continue
        i = idx_map[seed]
        related = []
        for code in codes:
            if code in assigned:
                continue
            j = idx_map[code]
            if corr[i, j] >= float(min_corr):
                related.append((code, float(corr[i, j]), float(score_map.get(code, 0))))
        if len(related) < int(min_size):
            continue
        related = sorted(related, key=lambda x: (x[2], x[1]), reverse=True)[:80]
        cluster_id += 1
        for code, seed_corr, _ in related:
            assigned.add(code)
            members.append({"cluster_id": cluster_id, "code6": code, "seed_code6": seed, "seed_corr": seed_corr})
        clusters.append({"cluster_id": cluster_id, "seed_code6": seed, "member_count": len(related)})
    return pd.DataFrame(clusters), pd.DataFrame(members)


def _annotate(clusters: pd.DataFrame, members: pd.DataFrame, features: pd.DataFrame, names: pd.DataFrame, sectors: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if clusters.empty or members.empty:
        return clusters, members
    m = members.merge(features, on="code6", how="left").merge(names, on="code6", how="left")
    level2 = sectors[sectors["level"].eq(2)][["code6", "sector_name"]].rename(columns={"sector_name": "l2_sector_name"})
    level3 = sectors[sectors["level"].eq(3)][["code6", "sector_name"]].rename(columns={"sector_name": "l3_sector_name"})
    m = m.merge(level2, on="code6", how="left").merge(level3, on="code6", how="left")
    m["display_name"] = m["name"].fillna(m["code6"])

    rows = []
    for cid, part in m.groupby("cluster_id"):
        top_members = part.sort_values("candidate_score", ascending=False).head(12)
        l2 = top_members["l2_sector_name"].dropna().astype(str).value_counts().head(4)
        l3 = top_members["l3_sector_name"].dropna().astype(str).value_counts().head(5)
        label_parts = []
        if not l2.empty:
            label_parts.append("/".join(l2.index.tolist()[:3]))
        if not l3.empty:
            label_parts.append("/".join(l3.index.tolist()[:3]))
        theme_label = " + ".join(label_parts) if label_parts else f"行为主题簇{cid}"
        rows.append(
            {
                "cluster_id": int(cid),
                "theme_label": theme_label,
                "member_count": int(len(part)),
                "avg_ret_window": float(part["ret_window"].mean()),
                "median_ret_window": float(part["ret_window"].median()),
                "avg_ret10": float(part["ret10"].mean()),
                "avg_ret5": float(part["ret5"].mean()),
                "avg_amount_ratio": float(part["amount_ratio"].mean()),
                "strong_days_avg": float(part["strong_days"].mean()),
                "top_l2": "、".join([f"{k}({v})" for k, v in l2.items()]),
                "top_l3": "、".join([f"{k}({v})" for k, v in l3.items()]),
                "top_members": "、".join([f"{r.display_name}({r.code6})" for r in top_members.itertuples(index=False)]),
            }
        )
    summary = pd.DataFrame(rows)
    summary["theme_score"] = (
        summary["avg_ret_window"].rank(pct=True).fillna(0) * 35
        + summary["avg_ret10"].rank(pct=True).fillna(0) * 25
        + summary["avg_ret5"].rank(pct=True).fillna(0) * 15
        + summary["avg_amount_ratio"].clip(0, 5).rank(pct=True).fillna(0) * 10
        + summary["member_count"].rank(pct=True).fillna(0) * 15
    )
    summary = summary.sort_values("theme_score", ascending=False).reset_index(drop=True)
    m = m.merge(summary[["cluster_id", "theme_label", "theme_score"]], on="cluster_id", how="left")
    return summary, m.sort_values(["theme_score", "cluster_id", "candidate_score"], ascending=[False, True, False])


def _write_outputs(result: dict[str, Any], clusters: pd.DataFrame, members: pd.DataFrame) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cluster_path = OUT_DIR / "theme_clusters.csv"
    member_path = OUT_DIR / "theme_cluster_members.csv"
    json_path = OUT_DIR / "summary.json"
    report_path = OUT_DIR / "REPORT.md"
    clusters.to_csv(cluster_path, index=False, encoding="utf-8-sig")
    members.to_csv(member_path, index=False, encoding="utf-8-sig")
    payload = {
        **result,
        "theme_clusters_csv": str(cluster_path),
        "theme_cluster_members_csv": str(member_path),
        "report": str(report_path),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 行为主题簇识别 V1 research_only",
        "",
        f"- 目标日期：`{result['target_date']}`",
        f"- 观察窗口：最近 {result['lookback_days']} 个交易日，实际 `{result['start_date']}~{result['target_date']}`",
        f"- 候选股票数：{result['candidate_count']}",
        f"- 主题簇数量：{result['cluster_count']}",
        "- 方法：不依赖行业/概念名称，先用日收益行为相关性聚类，再用现有行业标签辅助命名。",
        "- 用途：研究 PCB、光模块、商业航天这类跨行业主线的早期发现；暂不进入正式 G2/V4。",
        "- 缺口：本地暂未沉淀概念/题材成分表，所以当前命名主要依赖行业标签和成分股列表。",
        "",
        "## 主题簇摘要",
        "",
        "| 排名 | 主题标签 | 成员数 | 主题分 | 窗口均涨 | 10日均涨 | 5日均涨 | 量能扩张 | 代表成员 |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for i, row in enumerate(clusters.itertuples(index=False), 1):
        lines.append(
            f"| {i} | {row.theme_label} | {int(row.member_count)} | {float(row.theme_score):.2f} | "
            f"{float(row.avg_ret_window):.2%} | {float(row.avg_ret10):.2%} | {float(row.avg_ret5):.2%} | "
            f"{float(row.avg_amount_ratio):.2f} | {row.top_members} |"
        )
    lines.extend(
        [
            "",
            "## 解读",
            "",
            "- 如果一个簇的成员横跨多个 L2/L3 行业，但涨幅和量能同步，往往更像题材主线而不是传统行业主线。",
            "- 第一版重点看“能不能发现资金抱团的股票群”，后续再接入概念表或人工主题词，把簇命名为 PCB、光模块、商业航天等。",
            "- 下一步应验证：`G2候选 + 行为主题簇加分` 是否优于 `G2 + 行业扩散加分`。",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(str(report_path))
    print(str(cluster_path))
    print(str(member_path))
    print(f"cluster_count={len(clusters)} candidate_count={result['candidate_count']}")
    if not clusters.empty:
        print(clusters[["theme_label", "member_count", "theme_score", "avg_ret_window", "top_members"]].head(10).to_string(index=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate behavior-based theme clusters.")
    parser.add_argument("--target-date", default=None)
    parser.add_argument("--lookback-days", type=int, default=20)
    parser.add_argument("--load-days", type=int, default=35)
    parser.add_argument("--min-amount", type=float, default=1_000_000)
    parser.add_argument("--max-stocks", type=int, default=700)
    parser.add_argument("--min-corr", type=float, default=0.58)
    parser.add_argument("--min-size", type=int, default=5)
    args = parser.parse_args()

    dates = _latest_trade_dates(args.target_date, max(int(args.load_days), int(args.lookback_days) + 5))
    dates = dates[-int(args.lookback_days) :]
    daily = _load_daily(dates)
    features = _make_stock_features(daily, dates)
    candidates = _select_candidates(features, max(10, int(args.lookback_days) - 3), args.max_stocks, args.min_amount)
    clusters_raw, members_raw = _build_clusters(daily, candidates, dates, args.min_corr, args.min_size)
    codes6 = sorted(candidates["code6"].astype(str).unique().tolist())
    names = _load_names(codes6)
    sectors = _load_sector_labels(codes6)
    clusters, members = _annotate(clusters_raw, members_raw, features, names, sectors)

    result = {
        "target_date": dates[-1],
        "start_date": dates[0],
        "lookback_days": int(args.lookback_days),
        "load_days": int(args.load_days),
        "min_amount": float(args.min_amount),
        "max_stocks": int(args.max_stocks),
        "min_corr": float(args.min_corr),
        "min_size": int(args.min_size),
        "candidate_count": int(len(candidates)),
        "cluster_count": int(len(clusters)),
        "research_only": True,
    }
    _write_outputs(result, clusters, members)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
