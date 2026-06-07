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

from scripts.generate_behavior_theme_clusters_v1 import _code6


THEME_SOURCE = ROOT / "reports" / "behavior_theme_candidate_source_v1" / "theme_candidate_source.csv"
G2_SOURCE = ROOT / "reports" / "gen2_v2_live_smoke_current" / "source.csv"
OUT_DIR = ROOT / "reports" / "mainline_theme_observation_pool_v1"


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig")


def _num(s: Any) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _safe_col(df: pd.DataFrame, names: list[str]) -> str | None:
    for name in names:
        if name in df.columns:
            return name
    return None


def _load_g2() -> pd.DataFrame:
    g2 = _read_csv(G2_SOURCE)
    if g2.empty:
        return pd.DataFrame(columns=["code6", "in_g2_current", "g2_raw_score", "g2_strategy_label"])
    code_col = _safe_col(g2, ["code", "code6", "symbol", "stock_code"])
    if not code_col:
        return pd.DataFrame(columns=["code6", "in_g2_current", "g2_raw_score", "g2_strategy_label"])
    out = pd.DataFrame()
    out["code6"] = g2[code_col].map(_code6).astype(str).str.zfill(6)
    score_col = _safe_col(g2, ["score", "final_score", "rank_score", "g2_score", "total_score"])
    strategy_col = _safe_col(g2, ["strategy", "strategy_name", "signal_type", "source", "label"])
    out["in_g2_current"] = True
    out["g2_raw_score"] = _num(g2[score_col]) if score_col else np.nan
    out["g2_strategy_label"] = g2[strategy_col].astype(str) if strategy_col else ""
    return out.drop_duplicates("code6", keep="first")


def _build_pool(theme: pd.DataFrame, g2: pd.DataFrame) -> pd.DataFrame:
    if theme.empty:
        return pd.DataFrame()
    out = theme.copy()
    out["code6"] = out["code6"].map(_code6).astype(str).str.zfill(6)
    out = out.drop(columns=[c for c in ["in_g2_current", "g2_raw_score", "g2_strategy_label"] if c in out.columns])
    out = out.merge(g2, on="code6", how="left")
    out["in_g2_current"] = out["in_g2_current"].fillna(False).astype(bool)
    for col in ["theme_candidate_score", "theme_score", "candidate_score", "ret20", "ret10", "ret5", "runup_from_60d_low", "drawdown_from_20d_high"]:
        if col in out.columns:
            out[col] = _num(out[col])
    out["observation_level"] = "L3_watch"
    out.loc[out["research_action"].eq("priority_watch"), "observation_level"] = "L2_priority"
    out.loc[out["risk_label"].isin(["high_runup", "short_term_hot"]), "observation_level"] = "L4_wait_pullback"
    out.loc[out["in_g2_current"] & out["research_action"].eq("priority_watch"), "observation_level"] = "L1_g2_theme_overlap"

    out["theme_weight_hint"] = 0.0
    out.loc[out["observation_level"].eq("L2_priority"), "theme_weight_hint"] = 0.08
    out.loc[out["observation_level"].eq("L1_g2_theme_overlap"), "theme_weight_hint"] = 0.12
    out.loc[out["observation_level"].eq("L3_watch"), "theme_weight_hint"] = 0.03
    out.loc[out["observation_level"].eq("L4_wait_pullback"), "theme_weight_hint"] = -0.02

    out["suggested_usage"] = "观察，不作为买点"
    out.loc[out["observation_level"].eq("L2_priority"), "suggested_usage"] = "优先观察，等待G2/V4或盘中确认"
    out.loc[out["observation_level"].eq("L1_g2_theme_overlap"), "suggested_usage"] = "主题与G2重合，可作为排序加权研究"
    out.loc[out["observation_level"].eq("L4_wait_pullback"), "suggested_usage"] = "过热/高位，等待分歧回踩或二次确认"

    out["notes"] = ""
    out.loc[out["risk_label"].eq("high_runup"), "notes"] = "60日低点以来涨幅较高，避免把主线热度等同于追高买点"
    out.loc[out["risk_label"].eq("short_term_hot"), "notes"] = "短期涨幅过热，更适合等分歧"
    out.loc[out["risk_label"].eq("weak_after_high"), "notes"] = "离20日高点回撤较大，需确认是否转弱"

    cols = [
        "rank",
        "code6",
        "display_name",
        "theme_label",
        "observation_level",
        "suggested_usage",
        "theme_weight_hint",
        "theme_candidate_score",
        "theme_score",
        "candidate_score",
        "research_action",
        "risk_label",
        "ret20",
        "ret10",
        "ret5",
        "runup_from_60d_low",
        "drawdown_from_20d_high",
        "in_g2_current",
        "g2_raw_score",
        "g2_strategy_label",
        "notes",
    ]
    keep = [c for c in cols if c in out.columns]
    out = out[keep].sort_values(["observation_level", "theme_candidate_score"], ascending=[True, False]).reset_index(drop=True)
    out["pool_rank"] = np.arange(1, len(out) + 1)
    return out


def _pct(x: Any) -> str:
    if pd.isna(x):
        return ""
    return f"{float(x):.2%}"


def _write_report(pool: pd.DataFrame, target_date: str, args: argparse.Namespace) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pool_path = OUT_DIR / "observation_pool.csv"
    report_path = OUT_DIR / "REPORT.md"
    summary_path = OUT_DIR / "summary.json"
    pool.to_csv(pool_path, index=False, encoding="utf-8-sig")
    payload = {
        "target_date": target_date,
        "pool_count": int(len(pool)),
        "l1_g2_theme_overlap": int((pool["observation_level"] == "L1_g2_theme_overlap").sum()) if not pool.empty else 0,
        "l2_priority": int((pool["observation_level"] == "L2_priority").sum()) if not pool.empty else 0,
        "l4_wait_pullback": int((pool["observation_level"] == "L4_wait_pullback").sum()) if not pool.empty else 0,
        "theme_source": str(THEME_SOURCE),
        "g2_source": str(G2_SOURCE),
        "observation_pool": str(pool_path),
        "research_only": True,
    }
    summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 主线主题观察池 V1 research_only",
        "",
        f"- 目标日期：`{target_date}`",
        f"- 观察池数量：{payload['pool_count']}",
        f"- L1 G2与主题重合：{payload['l1_g2_theme_overlap']}",
        f"- L2 主题优先观察：{payload['l2_priority']}",
        f"- L4 等回踩/确认：{payload['l4_wait_pullback']}",
        "- 用途：把主线主题识别结果接入观察与排序加权研究，不是正式买点，不自动下单。",
        "- 风控边界：主题越热，单票集中度越要低；高位和短期过热只允许等待确认，不允许直接追。",
        "",
        "## 当前观察池",
        "",
        "| 池排名 | 股票 | 层级 | 主题 | 分数 | 建议用途 | 20日 | 10日 | 5日 | 风险 | G2当前 |",
        "|---:|---|---|---|---:|---|---:|---:|---:|---|---|",
    ]
    for row in pool.head(int(args.top_n)).itertuples(index=False):
        lines.append(
            f"| {int(row.pool_rank)} | {row.display_name} `{row.code6}` | {row.observation_level} | "
            f"{row.theme_label} | {float(row.theme_candidate_score):.2f} | {row.suggested_usage} | "
            f"{_pct(row.ret20)} | {_pct(row.ret10)} | {_pct(row.ret5)} | {row.risk_label} | {bool(row.in_g2_current)} |"
        )
    lines.extend(
        [
            "",
            "## 接入建议",
            "",
            "- `L1_g2_theme_overlap`：仅作为 G2 排序加权研究，不能绕过原始买点和市场门禁。",
            "- `L2_priority`：进入主题优先观察池，等待 G2/V4、盘中相对强度或分歧回踩确认。",
            "- `L3_watch`：保留观察，不增加仓位优先级。",
            "- `L4_wait_pullback`：高位或短期过热，只能等回踩或二次确认。",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(str(report_path))
    print(str(pool_path))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not pool.empty:
        show_cols = ["pool_rank", "display_name", "code6", "observation_level", "theme_candidate_score", "suggested_usage", "risk_label", "in_g2_current"]
        print(pool[show_cols].head(int(args.top_n)).to_string(index=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate current mainline theme observation pool.")
    parser.add_argument("--target-date", default=None)
    parser.add_argument("--top-n", type=int, default=30)
    args = parser.parse_args()
    theme = _read_csv(THEME_SOURCE)
    if theme.empty:
        raise RuntimeError("theme candidate source is missing; run generate_behavior_theme_candidate_source_v1.py first")
    target_date = args.target_date
    if not target_date and "trade_date" in theme.columns:
        target_date = str(pd.to_datetime(theme["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d").dropna().max())
    if not target_date or target_date.lower() in {"nan", "nat", "none", "null"}:
        target_date = ""
    g2 = _load_g2()
    pool = _build_pool(theme, g2)
    _write_report(pool, target_date, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
