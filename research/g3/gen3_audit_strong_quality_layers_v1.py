from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
OUT_DIR = _report_path() / "gen3_strong_quality_layers_v1"

STRONG_CANDIDATES = (
    _report_path()
    / "gen3_strong_v2_independent_source_v1"
    / "strong_v2_main_up_plus_weak04_candidates_no_forward.csv"
)
ROUTER_TRADES = (
    _report_path()
    / "gen3_dynamic_router_strong_absorb_g2_v1"
    / "plusweak_limit2"
    / "closed_trades.csv"
)

WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "recent_2024_06": ("2024-06-01", "2026-06-01"),
    "blind_2026ytd": ("2026-01-01", "2026-06-01"),
    "full": ("2020-01-01", "2026-06-01"),
}


def _key(df: pd.DataFrame, date_col: str = "entry_date") -> pd.Series:
    date = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
    return df["code"].astype(str) + "|" + date.astype(str)


def _num(s: pd.Series | Any) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _bucket_rank(v: Any) -> str:
    if pd.isna(v):
        return "rank_missing"
    x = float(v)
    if x <= 1:
        return "rank_1"
    if x <= 2:
        return "rank_2"
    if x <= 5:
        return "rank_3_5"
    return "rank_gt5"


def _bucket_l3(v: Any) -> str:
    if pd.isna(v):
        return "l3_missing"
    x = float(v)
    if x >= 0.50:
        return "l3_s3_ge50"
    if x >= 0.20:
        return "l3_s3_20_50"
    if x >= 0.05:
        return "l3_s3_05_20"
    return "l3_s3_lt05"


def _bucket_runup(v: Any) -> str:
    if pd.isna(v):
        return "runup_missing"
    x = float(v)
    if x <= 0.30:
        return "runup_le30"
    if x <= 0.60:
        return "runup_30_60"
    if x <= 1.00:
        return "runup_60_100"
    return "runup_gt100"


def _load() -> pd.DataFrame:
    trades = pd.read_csv(ROUTER_TRADES, low_memory=False)
    trades = trades[trades["route"].eq("strong_main")].copy()
    trades["entry_date"] = pd.to_datetime(trades["entry_date"], errors="coerce").dt.normalize()
    trades["key"] = _key(trades)
    trades["policy_net_ret"] = _num(trades["policy_net_ret"])
    trades["realized_pnl"] = _num(trades["realized_pnl"]).fillna(0.0)
    trades["stake"] = _num(trades["stake"])

    cand = pd.read_csv(STRONG_CANDIDATES, low_memory=False)
    cand["entry_date"] = pd.to_datetime(cand["entry_date"], errors="coerce").dt.normalize()
    cand["key"] = _key(cand)
    keep = [
        "key",
        "g3_market_style",
        "g3_route_weight",
        "g3_strong_score",
        "v4_rank",
        "v4_score",
        "score_volume5",
        "runup_from_60d_low",
        "sector_strong",
        "sector_score_bonus",
        "l3_s3",
        "l2_s3",
        "g2_v2_buy_logic",
        "signal_family",
        "market_breadth",
    ]
    cand = cand[[c for c in keep if c in cand.columns]].drop_duplicates("key")
    out = trades.merge(cand, on="key", how="left")
    for col in ["v4_rank", "l3_s3", "l2_s3", "runup_from_60d_low", "market_breadth", "g3_strong_score"]:
        if col in out.columns:
            out[col] = _num(out[col])
    out["rank_bucket"] = out["v4_rank"].map(_bucket_rank)
    out["l3_bucket"] = out["l3_s3"].map(_bucket_l3)
    out["runup_bucket"] = out["runup_from_60d_low"].map(_bucket_runup)
    out["sector_bucket"] = out["sector_strong"].fillna(False).astype(bool).map(lambda x: "sector_strong" if x else "sector_not_strong")
    out["style_bucket"] = out["g3_market_style"].fillna("style_missing").astype(str)
    return out


def _summarize_group(df: pd.DataFrame, feature: str, window: str, start: str, end: str) -> pd.DataFrame:
    part = df[df["entry_date"].between(pd.Timestamp(start), pd.Timestamp(end))].copy()
    rows = []
    for value, g in part.groupby(feature, dropna=False):
        net = _num(g["policy_net_ret"]).dropna()
        pnl = _num(g["realized_pnl"]).fillna(0.0)
        stake = _num(g["stake"]).fillna(0.0)
        rows.append(
            {
                "window": window,
                "feature": feature,
                "bucket": str(value),
                "trade_count": int(len(g)),
                "pnl_sum": float(pnl.sum()),
                "stake_sum": float(stake.sum()),
                "pnl_per_stake": float(pnl.sum() / stake.sum()) if float(stake.sum()) else None,
                "win_rate": float((net > 0).mean()) if not net.empty else None,
                "avg_trade_return": float(net.mean()) if not net.empty else None,
                "median_trade_return": float(net.median()) if not net.empty else None,
                "worst_trade": float(net.min()) if not net.empty else None,
                "best_trade": float(net.max()) if not net.empty else None,
            }
        )
    return pd.DataFrame(rows)


def _all_summaries(df: pd.DataFrame) -> pd.DataFrame:
    features = ["style_bucket", "sector_bucket", "rank_bucket", "l3_bucket", "runup_bucket"]
    parts = []
    for window, (start, end) in WINDOWS.items():
        for feature in features:
            parts.append(_summarize_group(df, feature, window, start, end))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def _top_bad_layers(summary: pd.DataFrame) -> pd.DataFrame:
    d = summary[summary["window"].isin(["valid_2024_2025", "recent_2024_06", "blind_2026ytd"])].copy()
    d = d[d["trade_count"].ge(5)].copy()
    return d.sort_values(["pnl_per_stake", "pnl_sum"], ascending=[True, True]).head(30)


def _top_good_layers(summary: pd.DataFrame) -> pd.DataFrame:
    d = summary[summary["window"].isin(["valid_2024_2025", "recent_2024_06", "blind_2026ytd"])].copy()
    d = d[d["trade_count"].ge(5)].copy()
    return d.sort_values(["pnl_per_stake", "pnl_sum"], ascending=[False, False]).head(30)


from research.common.reporting import percent_text as _pct


from research.common.reporting import markdown_table as _md_table


def _write_report(summary: pd.DataFrame, bad: pd.DataFrame, good: pd.DataFrame) -> None:
    focus_cols = [
        "window",
        "feature",
        "bucket",
        "trade_count",
        "pnl_sum",
        "pnl_per_stake",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "best_trade",
    ]
    lines = [
        "# G3 strong route 质量分层审计 v1",
        "",
        "## 边界",
        "",
        "- 样本来自 `plusweak_limit2` 的 strong_main 成交，不重新调参。",
        "- 只做分层观察，不能直接把最优桶当成正式参数。",
        "- 目标是找到 2024-2026 拖累层和稳定贡献层，为下一轮固定规则候选做准备。",
        "",
        "## 拖累层",
        "",
        _md_table(
            bad[focus_cols],
            pct_cols={"pnl_per_stake", "win_rate", "avg_trade_return", "worst_trade", "best_trade"},
        ),
        "",
        "## 贡献层",
        "",
        _md_table(
            good[focus_cols],
            pct_cols={"pnl_per_stake", "win_rate", "avg_trade_return", "worst_trade", "best_trade"},
        ),
        "",
        "## 下一步",
        "",
        "1. 对拖累层做交叉审计，确认是否是同一批交易重复出现在多个坏桶里。",
        "2. 优先测试固定的质量过滤：不追求最高收益，先压低 valid/recent 的大亏层。",
        "3. 过滤必须同时看 train/valid/blind，不能只救 2025-2026。",
        "",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = _load()
    summary = _all_summaries(df)
    bad = _top_bad_layers(summary)
    good = _top_good_layers(summary)
    df.to_csv(OUT_DIR / "strong_trades_enriched.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "quality_layer_summary.csv", index=False, encoding="utf-8-sig")
    bad.to_csv(OUT_DIR / "bad_layers.csv", index=False, encoding="utf-8-sig")
    good.to_csv(OUT_DIR / "good_layers.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "trade_count": int(len(df)),
                "bad_layers": bad.head(10).to_dict(orient="records"),
                "good_layers": good.head(10).to_dict(orient="records"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _write_report(summary, bad, good)
    print(json.dumps({"trade_count": int(len(df)), "out_dir": str(OUT_DIR)}, ensure_ascii=False, indent=2))
    print("\nBAD")
    print(bad.head(10).to_string(index=False))
    print("\nGOOD")
    print(good.head(10).to_string(index=False))


if __name__ == "__main__":
    main()
