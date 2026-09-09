from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = _PROJECT_ROOT
AUDIT_DIR = _report_path() / "gen3_range_gap_cost_execution_noise_audit_v1"
OUT_DIR = _report_path() / "gen3_range_gap_broad_noise_filter_test_v1"


def _pct(v: float | int | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.2%}"


def _date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.normalize()


def _metrics(df: pd.DataFrame, label: str, profile: str, ret_col: str, window: str) -> dict:
    r = pd.to_numeric(df.get(ret_col, pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    return {
        "variant": label,
        "profile": profile,
        "window": window,
        "rows": int(len(df)),
        "unique_dates": int(df["entry_date"].nunique()) if "entry_date" in df and not df.empty else 0,
        "win_rate": float((r > 0).mean()) if len(r) else 0.0,
        "avg_ret": float(r.mean()) if len(r) else 0.0,
        "sum_ret": float(r.sum()) if len(r) else 0.0,
        "worst_trade": float(r.min()) if len(r) else 0.0,
    }


def _window_df(df: pd.DataFrame, window: str) -> pd.DataFrame:
    defs = {
        "train_2020_2023": ("2020-01-01", "2023-12-31"),
        "valid_2024_2025": ("2024-01-01", "2025-12-31"),
        "blind_2026ytd": ("2026-01-01", "2026-12-31"),
        "weak_gap_2022_2024": ("2022-01-01", "2024-12-31"),
        "full": ("2020-01-01", "2026-12-31"),
    }
    start, end = defs[window]
    return df[(df["entry_date"] >= pd.Timestamp(start)) & (df["entry_date"] <= pd.Timestamp(end))].copy()


def _variant_mask(df: pd.DataFrame, variant: str) -> pd.Series:
    idx = pd.Series(True, index=df.index)
    adx = df.get("adx_layer", pd.Series("", index=df.index)).astype(str)
    range_pos = pd.to_numeric(df.get("range_pos60"), errors="coerce")
    gap_open = pd.to_numeric(df.get("gap_open"), errors="coerce")
    market_amt = pd.to_numeric(df.get("market_amount_ratio20"), errors="coerce")
    lower_shadow = pd.to_numeric(df.get("lower_shadow_ratio"), errors="coerce")
    score = pd.to_numeric(df.get("range_v3_score"), errors="coerce")

    if variant == "base":
        return idx
    if variant == "no_adx_downtrend":
        return idx & ~adx.eq("downtrend_strength")
    if variant == "range_pos60_gt30":
        return idx & (range_pos > 0.30)
    if variant == "no_small_positive_gap":
        return idx & ~((gap_open > 0.0) & (gap_open <= 0.02))
    if variant == "market_amount_outside_08_12":
        return idx & ((market_amt <= 0.8) | (market_amt > 1.2))
    if variant == "no_mid_lower_shadow":
        return idx & ~((lower_shadow > 0.15) & (lower_shadow <= 0.30))
    if variant == "score_not_extreme_high":
        return idx & (score <= 0.90)
    if variant == "conservative_combo_a":
        return idx & ~adx.eq("downtrend_strength") & (range_pos > 0.30)
    if variant == "conservative_combo_b":
        return idx & ~adx.eq("downtrend_strength") & ~((gap_open > 0.0) & (gap_open <= 0.02))
    raise ValueError(f"unknown variant: {variant}")


def _display(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in ["win_rate", "avg_ret", "sum_ret", "worst_trade"]:
        if c in out:
            out[c] = out[c].map(_pct)
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pure = pd.read_csv(AUDIT_DIR / "range_gap_pure_joined_profiles.csv", low_memory=False)
    combo = pd.read_csv(AUDIT_DIR / "range_gap_combo_stress_joined_profiles.csv", low_memory=False)
    pure["entry_date"] = _date(pure["entry_date"])
    combo["entry_date"] = _date(combo["entry_date"])

    variants = [
        "base",
        "no_adx_downtrend",
        "range_pos60_gt30",
        "no_small_positive_gap",
        "market_amount_outside_08_12",
        "no_mid_lower_shadow",
        "score_not_extreme_high",
        "conservative_combo_a",
        "conservative_combo_b",
    ]
    windows = ["train_2020_2023", "valid_2024_2025", "blind_2026ytd", "weak_gap_2022_2024", "full"]

    rows = []
    for data_name, df, ret_col in [
        ("pure", pure, "policy_net_ret"),
        ("combo", combo, "audit_ret"),
    ]:
        for profile, g in df.groupby("profile", dropna=False):
            for variant in variants:
                vg = g[_variant_mask(g, variant)].copy()
                for window in windows:
                    wg = _window_df(vg, window)
                    m = _metrics(wg, variant, str(profile), ret_col, window)
                    m["dataset"] = data_name
                    m["retention_vs_profile"] = len(vg) / len(g) if len(g) else 0.0
                    rows.append(m)
    metrics = pd.DataFrame(rows)

    # A variant is only useful if it improves the weak-cost pain points without deleting most samples.
    pivot_rows = []
    for variant in variants:
        full_close100 = metrics[
            metrics["dataset"].eq("combo")
            & metrics["profile"].eq("combo_close_100bps")
            & metrics["window"].eq("full")
            & metrics["variant"].eq(variant)
        ]
        full_hair = metrics[
            metrics["dataset"].eq("combo")
            & metrics["profile"].eq("combo_nextopen_haircut2_30bps")
            & metrics["window"].eq("full")
            & metrics["variant"].eq(variant)
        ]
        valid_close100 = metrics[
            metrics["dataset"].eq("combo")
            & metrics["profile"].eq("combo_close_100bps")
            & metrics["window"].eq("valid_2024_2025")
            & metrics["variant"].eq(variant)
        ]
        blind_close100 = metrics[
            metrics["dataset"].eq("combo")
            & metrics["profile"].eq("combo_close_100bps")
            & metrics["window"].eq("blind_2026ytd")
            & metrics["variant"].eq(variant)
        ]
        if full_close100.empty:
            continue
        pivot_rows.append(
            {
                "variant": variant,
                "close100_rows": int(full_close100.iloc[0]["rows"]),
                "close100_avg_ret": float(full_close100.iloc[0]["avg_ret"]),
                "close100_sum_ret": float(full_close100.iloc[0]["sum_ret"]),
                "close100_valid_sum_ret": float(valid_close100.iloc[0]["sum_ret"]) if not valid_close100.empty else None,
                "close100_blind_sum_ret": float(blind_close100.iloc[0]["sum_ret"]) if not blind_close100.empty else None,
                "haircut2_avg_ret": float(full_hair.iloc[0]["avg_ret"]) if not full_hair.empty else None,
                "haircut2_sum_ret": float(full_hair.iloc[0]["sum_ret"]) if not full_hair.empty else None,
                "retention": float(full_close100.iloc[0]["retention_vs_profile"]),
            }
        )
    comparison = pd.DataFrame(pivot_rows).sort_values(["close100_avg_ret", "haircut2_avg_ret"], ascending=[False, False])

    metrics.to_csv(OUT_DIR / "range_gap_broad_filter_window_metrics.csv", index=False, encoding="utf-8-sig")
    comparison.to_csv(OUT_DIR / "range_gap_broad_filter_comparison.csv", index=False, encoding="utf-8-sig")

    display_cmp = comparison.copy()
    for c in ["close100_avg_ret", "close100_sum_ret", "close100_valid_sum_ret", "close100_blind_sum_ret", "haircut2_avg_ret", "haircut2_sum_ret", "retention"]:
        if c in display_cmp:
            display_cmp[c] = display_cmp[c].map(_pct)

    # Prefer broad filters with at least 40% retention and non-negative close100 avg.
    passable = comparison[(comparison["retention"] >= 0.40) & (comparison["close100_avg_ret"] >= 0)]
    best = passable.head(1).iloc[0].to_dict() if not passable.empty else {}
    meta = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tested_variants": len(variants),
        "passable_variants": int(len(passable)),
        "best_variant": best.get("variant", ""),
        "best_close100_avg_ret": best.get("close100_avg_ret"),
        "best_haircut2_avg_ret": best.get("haircut2_avg_ret"),
        "next_step": "if_passable_variant_exists_rebuild_combo_with_range_filter_else_research_new_range_trigger",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = f"""# G3 range_gap 宽口径降噪过滤复验 V1

生成时间：{meta["generated_at"]}

## 目的

只测试少数从坏桶归纳出来的宽口径过滤，避免大规模参数搜索导致过拟合。

通过条件暂定为：组合 `range_gap` 在 `close_100bps` 下平均收益转正，同时保留至少 40% 样本。

## 总体比较

{display_cmp.to_markdown(index=False)}

## 阶段判断

- 可候选过滤器数量：`{meta["passable_variants"]}`
- 当前最佳：`{meta["best_variant"] or "无"}`

如果有可候选过滤器，下一步要把它放回完整三链组合重算 MTM，而不是只看单链路表格。
如果没有，则说明 `range_gap` 当前触发源本身太薄，需要回到箱体底部/冰点定义，而不是继续小修小补。
"""
    (OUT_DIR / "range_gap_broad_noise_filter_test_report_cn.md").write_text(report, encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
