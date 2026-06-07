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


OBS_POOL = ROOT / "reports" / "mainline_theme_observation_pool_v1" / "observation_pool.csv"
G2_SOURCE = ROOT / "reports" / "gen2_v2_live_smoke_current" / "source.csv"
OUT_DIR = ROOT / "reports" / "mainline_theme_strategy_overlay_v1"


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig")


def _safe_col(df: pd.DataFrame, names: list[str]) -> str | None:
    for name in names:
        if name in df.columns:
            return name
    return None


def _num(x: Any) -> pd.Series:
    return pd.to_numeric(x, errors="coerce")


def _normalize_strategy_source(df: pd.DataFrame, source_name: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["code6", "strategy_source", "raw_score", "raw_rank", "strategy_label"])
    code_col = _safe_col(df, ["code", "code6", "symbol", "stock_code"])
    if not code_col:
        return pd.DataFrame(columns=["code6", "strategy_source", "raw_score", "raw_rank", "strategy_label"])
    score_col = _safe_col(df, ["score", "final_score", "v4_score", "rank_score", "g2_score", "total_score"])
    rank_col = _safe_col(df, ["rank", "v4_rank", "raw_rank"])
    label_col = _safe_col(df, ["strategy", "strategy_name", "signal_family", "source_family", "source", "label"])
    out = pd.DataFrame()
    out["code6"] = df[code_col].map(_code6).astype(str).str.zfill(6)
    out["strategy_source"] = source_name
    out["raw_score"] = _num(df[score_col]) if score_col else np.nan
    out["raw_rank"] = _num(df[rank_col]) if rank_col else np.nan
    out["strategy_label"] = df[label_col].astype(str) if label_col else ""
    return out.dropna(subset=["code6"]).drop_duplicates(["strategy_source", "code6"], keep="first")


def _theme_addon(obs: pd.DataFrame) -> pd.DataFrame:
    if obs.empty:
        return pd.DataFrame()
    out = obs.copy()
    out["code6"] = out["code6"].map(_code6).astype(str).str.zfill(6)
    for col in ["theme_weight_hint", "theme_candidate_score", "theme_score", "candidate_score"]:
        if col in out.columns:
            out[col] = _num(out[col])
    out["theme_overlay_action"] = "no_action"
    out.loc[out["observation_level"].eq("L1_g2_theme_overlap"), "theme_overlay_action"] = "score_bonus"
    out.loc[out["observation_level"].eq("L2_priority"), "theme_overlay_action"] = "watch_pool"
    out.loc[out["observation_level"].eq("L3_watch"), "theme_overlay_action"] = "weak_watch"
    out.loc[out["observation_level"].eq("L4_wait_pullback"), "theme_overlay_action"] = "risk_cap_or_wait"
    out["can_raise_buy_signal"] = False
    out["can_bypass_market_gate"] = False
    out["can_bypass_original_strategy"] = False
    out["position_cap_hint"] = "normal"
    out.loc[out["observation_level"].eq("L2_priority"), "position_cap_hint"] = "below_normal_until_confirmed"
    out.loc[out["observation_level"].eq("L4_wait_pullback"), "position_cap_hint"] = "low_or_zero_before_pullback_confirm"
    keep = [
        "code6",
        "display_name",
        "theme_label",
        "observation_level",
        "theme_overlay_action",
        "theme_weight_hint",
        "theme_candidate_score",
        "research_action",
        "risk_label",
        "suggested_usage",
        "position_cap_hint",
        "can_raise_buy_signal",
        "can_bypass_market_gate",
        "can_bypass_original_strategy",
    ]
    return out[[c for c in keep if c in out.columns]].drop_duplicates("code6", keep="first")


def _overlay_strategy(strategy: pd.DataFrame, addon: pd.DataFrame) -> pd.DataFrame:
    if strategy.empty:
        return pd.DataFrame(columns=[
            "strategy_source",
            "code6",
            "display_name",
            "theme_label",
            "observation_level",
            "theme_overlay_action",
            "raw_score",
            "theme_weight_hint",
            "overlay_score",
            "risk_label",
            "position_cap_hint",
            "can_raise_buy_signal",
        ])
    out = strategy.merge(addon, on="code6", how="left")
    out["has_mainline_theme"] = out["observation_level"].notna()
    out["theme_weight_hint"] = _num(out["theme_weight_hint"]).fillna(0.0)
    out["raw_score"] = _num(out["raw_score"])
    out["overlay_score"] = out["raw_score"].fillna(0) + out["theme_weight_hint"].fillna(0) * 100.0
    out.loc[~out["has_mainline_theme"], "theme_overlay_action"] = "no_theme_match"
    out.loc[~out["has_mainline_theme"], "position_cap_hint"] = "unchanged"
    out.loc[~out["has_mainline_theme"], "can_raise_buy_signal"] = False
    out = out.sort_values(["strategy_source", "overlay_score", "raw_score"], ascending=[True, False, False]).reset_index(drop=True)
    out["overlay_rank"] = out.groupby("strategy_source").cumcount() + 1
    return out


def _write_outputs(addon: pd.DataFrame, overlays: pd.DataFrame, args: argparse.Namespace) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    addon_path = OUT_DIR / "theme_addon.csv"
    overlay_path = OUT_DIR / "strategy_overlay.csv"
    report_path = OUT_DIR / "REPORT.md"
    summary_path = OUT_DIR / "summary.json"
    addon.to_csv(addon_path, index=False, encoding="utf-8-sig")
    overlays.to_csv(overlay_path, index=False, encoding="utf-8-sig")

    payload = {
        "target_date": args.target_date,
        "theme_addon_count": int(len(addon)),
        "strategy_candidate_count": int(len(overlays)),
        "theme_matched_strategy_count": int(overlays["has_mainline_theme"].sum()) if "has_mainline_theme" in overlays.columns else 0,
        "l1_count": int((addon["observation_level"] == "L1_g2_theme_overlap").sum()) if not addon.empty else 0,
        "l2_count": int((addon["observation_level"] == "L2_priority").sum()) if not addon.empty else 0,
        "l4_count": int((addon["observation_level"] == "L4_wait_pullback").sum()) if not addon.empty else 0,
        "addon_csv": str(addon_path),
        "overlay_csv": str(overlay_path),
        "research_only": True,
    }
    summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 主线主题策略 Overlay V1 research_only",
        "",
        f"- 目标日期：`{args.target_date}`",
        f"- 主题加权条目：{payload['theme_addon_count']}",
        f"- 策略候选条目：{payload['strategy_candidate_count']}",
        f"- 策略与主题重合：{payload['theme_matched_strategy_count']}",
        "- 定位：只提供排序加权与观察层级，不产生买点，不绕过市场门禁，不绕过 G2/V4 原始条件。",
        "",
        "## 主题加权接口规则",
        "",
        "| 层级 | overlay 动作 | 加权含义 | 仓位含义 |",
        "|---|---|---|---|",
        "| L1_g2_theme_overlap | score_bonus | G2/V4 已有候选且命中主线，可研究排序加分 | 原策略仓位上限内处理 |",
        "| L2_priority | watch_pool | 主线优先观察，但没有正式买点 | 等确认前低于正常仓位或不交易 |",
        "| L3_watch | weak_watch | 仅保留观察 | 不提高优先级 |",
        "| L4_wait_pullback | risk_cap_or_wait | 高位/短期过热 | 等回踩，低仓或零仓 |",
        "",
        "## 当前策略重合",
        "",
    ]
    if overlays.empty:
        lines.append("- 当前策略源没有候选行，因此没有 G2/V4 与主题主线重合。")
    else:
        lines.extend([
            "| 排名 | 来源 | 股票 | 原始分 | 主题层级 | overlay分 | 动作 | 风险 |",
            "|---:|---|---|---:|---|---:|---|---|",
        ])
        for row in overlays.head(int(args.top_n)).itertuples(index=False):
            display_name = getattr(row, "display_name", "") if pd.notna(getattr(row, "display_name", "")) else ""
            lines.append(
                f"| {int(row.overlay_rank)} | {row.strategy_source} | {display_name} `{row.code6}` | "
                f"{float(row.raw_score):.2f} | {getattr(row, 'observation_level', '')} | {float(row.overlay_score):.2f} | "
                f"{row.theme_overlay_action} | {getattr(row, 'risk_label', '')} |"
            )
    lines.extend([
        "",
        "## 当前主题 addon",
        "",
        "| 股票 | 层级 | 动作 | 加权 | 风险 | 用途 |",
        "|---|---|---|---:|---|---|",
    ])
    for row in addon.head(int(args.top_n)).itertuples(index=False):
        lines.append(
            f"| {row.display_name} `{row.code6}` | {row.observation_level} | {row.theme_overlay_action} | "
            f"{float(row.theme_weight_hint):.2f} | {row.risk_label} | {row.suggested_usage} |"
        )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(str(report_path))
    print(str(addon_path))
    print(str(overlay_path))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not addon.empty:
        cols = ["code6", "display_name", "observation_level", "theme_overlay_action", "theme_weight_hint", "risk_label"]
        print(addon[cols].head(int(args.top_n)).to_string(index=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate mainline theme overlay for strategy ranking.")
    parser.add_argument("--target-date", default="2026-06-04")
    parser.add_argument("--top-n", type=int, default=30)
    args = parser.parse_args()

    obs = _read_csv(OBS_POOL)
    if obs.empty:
        raise RuntimeError("observation pool is missing; run generate_mainline_theme_observation_pool_v1.py first")
    addon = _theme_addon(obs)
    g2 = _normalize_strategy_source(_read_csv(G2_SOURCE), "g2_v2_complete_current")
    overlays = _overlay_strategy(g2, addon)
    _write_outputs(addon, overlays, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
