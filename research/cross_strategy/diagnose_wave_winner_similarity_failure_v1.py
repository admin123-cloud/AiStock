from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_wave_winner_similarity_scheduler_v1 import BASE_VARIANTS, SOURCE_DIR, _load_candidate_pool, _score_today, _winner_profile  # noqa: E402
from utils.paths import report_path  # noqa: E402


SCHED_DIR = report_path("wave_style_model_scheduler_v1")
OUT_DIR = report_path("wave_winner_similarity_scheduler_v1")
WINNER = "scheduler_focus_240d_score120_aggr25"


def _pct(value: object) -> str:
    try:
        return f"{float(value):.2%}"
    except Exception:
        return ""


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, limit: int | None = None) -> str:
    d = df.head(limit).copy() if limit else df.copy()
    for col in pct_cols or set():
        if col in d.columns:
            d[col] = d[col].map(_pct)
    return d.to_markdown(index=False)


def run() -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = pd.read_csv(SCHED_DIR / WINNER / "closed_trades.csv", encoding="utf-8-sig")
    trades["entry_date"] = pd.to_datetime(trades["entry_date"], errors="coerce").dt.normalize()
    trades["policy_exit_date"] = pd.to_datetime(trades["policy_exit_date"], errors="coerce").dt.normalize()
    for col in ["net_ret", "realized_pnl"]:
        trades[col] = pd.to_numeric(trades.get(col), errors="coerce")

    pool = _load_candidate_pool(SOURCE_DIR, BASE_VARIANTS)
    rows: list[dict[str, Any]] = []
    for _, trade in trades.sort_values("entry_date").iterrows():
        day = trade["entry_date"]
        profile = _winner_profile(
            pool,
            day,
            lookback_days=240,
            min_winners=12,
            winner_ret=0.20,
            top_n=60,
            recent_weight=False,
        )
        if profile is None:
            continue
        todays = pool[(pool["entry_date"].eq(day)) & (pool["variant"].eq(trade["selected_variant"]))].copy()
        if todays.empty:
            continue
        scored = _score_today(todays, profile)
        hit = scored[scored["code_raw"].astype(str).eq(str(trade["code_raw"]))]
        if hit.empty:
            continue
        selected = hit.iloc[0]
        top = scored.iloc[0]
        rows.append(
            {
                "entry_date": day,
                "code_raw": trade["code_raw"],
                "stock_name": trade["stock_name"],
                "variant": trade["selected_variant"],
                "template_label": trade["template_label"],
                "net_ret": trade["net_ret"],
                "realized_pnl": trade["realized_pnl"],
                "similarity_score": selected["similarity_score"],
                "similarity_rank_score": selected["similarity_rank_score"],
                "similarity_rank": int(scored.index.get_loc(hit.index[0]) + 1),
                "day_candidates": int(len(scored)),
                "top_by_similarity": top["code_raw"],
                "top_by_similarity_name": top.get("stock_name", ""),
                "top_by_similarity_ret": top["net_ret"],
            }
        )

    scored_trades = pd.DataFrame(rows)
    scored_trades.to_csv(OUT_DIR / "score120_trade_similarity_diagnostic.csv", index=False, encoding="utf-8-sig")
    if scored_trades.empty:
        raise RuntimeError("no scored trades produced")

    corr = float(scored_trades["net_ret"].corr(scored_trades["similarity_rank_score"]))
    scored_trades["similarity_bucket"] = pd.qcut(
        scored_trades["similarity_rank_score"].rank(method="first"),
        4,
        labels=["Q1_low", "Q2", "Q3", "Q4_high"],
    )
    bucket = (
        scored_trades.groupby("similarity_bucket", observed=False)
        .agg(
            trades=("net_ret", "size"),
            avg_ret=("net_ret", "mean"),
            win_rate=("net_ret", lambda x: float((x > 0).mean())),
            pnl=("realized_pnl", "sum"),
            worst_ret=("net_ret", "min"),
            avg_rank=("similarity_rank", "mean"),
        )
        .reset_index()
    )
    bucket["pnl"] = bucket["pnl"].round(0)
    bucket.to_csv(OUT_DIR / "score120_similarity_bucket_summary.csv", index=False, encoding="utf-8-sig")

    replacement = {
        "selected_avg_ret": float(scored_trades["net_ret"].mean()),
        "selected_win_rate": float((scored_trades["net_ret"] > 0).mean()),
        "top_similarity_avg_ret": float(scored_trades["top_by_similarity_ret"].mean()),
        "top_similarity_win_rate": float((scored_trades["top_by_similarity_ret"] > 0).mean()),
    }
    worst_high = scored_trades.sort_values(["similarity_rank_score", "net_ret"], ascending=[False, True]).head(30)
    worst_high.to_csv(OUT_DIR / "score120_high_similarity_bad_cases.csv", index=False, encoding="utf-8-sig")

    lines = [
        "# 波段赢家相似度失败诊断 v1",
        "",
        "## 结论",
        "",
        f"- 在 `{WINNER}` 已成交的 {len(scored_trades)} 笔交易中，静态日线赢家相似度与未来收益相关性为 `{corr:.4f}`，基本没有预测力。",
        f"- 原策略已成交样本均值 {_pct(replacement['selected_avg_ret'])}、胜率 {_pct(replacement['selected_win_rate'])}；若粗暴改成当天相似度第一名，均值仅 {_pct(replacement['top_similarity_avg_ret'])}、胜率 {_pct(replacement['top_similarity_win_rate'])}。",
        "- 这说明“像上一波赢家”不能只看静态形态距离；需要加入主线扩散、所处波段阶段、资金承接和修复确认。",
        "",
        "## 相似度分桶",
        "",
        _md_table(bucket, {"avg_ret", "win_rate", "worst_ret"}),
        "",
        "## 高相似度坏样本",
        "",
        _md_table(
            worst_high[
                [
                    "entry_date",
                    "code_raw",
                    "stock_name",
                    "variant",
                    "template_label",
                    "net_ret",
                    "realized_pnl",
                    "similarity_rank_score",
                    "similarity_rank",
                    "day_candidates",
                    "top_by_similarity",
                    "top_by_similarity_ret",
                ]
            ],
            {"net_ret", "top_by_similarity_ret"},
            limit=20,
        ),
        "",
        "## 下一版方向",
        "",
        "- 保留 `score120_aggr25` 作为交易开关，不再用静态相似度替代它。",
        "- 新增“阶段相似度”：区分启动初段、主升中段、高位震荡、二次修复，而不是把所有赢家压成一个中心画像。",
        "- 新增“主线扩散确认”：只有当候选所属行业/主题在最近 5-10 日有足够扩散和强度延续时，才允许用相似度加分。",
        "- 新增“资金承接确认”：用 30m/日线量价承接过滤掉高位形似赢家但资金开始退潮的样本。",
    ]
    (OUT_DIR / "DIAGNOSTIC_CN.md").write_text("\n".join(lines), encoding="utf-8")

    result = {
        "status": "completed",
        "out_dir": str(OUT_DIR),
        "scored_trades": int(len(scored_trades)),
        "correlation": corr,
        **replacement,
    }
    (OUT_DIR / "diagnostic_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    print(json.dumps(run(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
