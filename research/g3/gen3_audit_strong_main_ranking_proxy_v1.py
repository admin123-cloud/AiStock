from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = _PROJECT_ROOT
SOURCE = _report_path() / "gen3_strong_v2_independent_source_v1" / "strong_v2_main_up_only_hold5_closed_trades.csv"
SELECTED = _report_path() / "gen3_guarded_candidate_package_v1" / "g3_guarded_candidate_closed_trades.csv"
OUT_DIR = _report_path() / "gen3_strong_main_ranking_proxy_audit_v1"

FORBIDDEN_SELECTION_PATTERNS = [
    r"^fwd_ret",
    r"^outcome",
    r"^mfe_",
    r"^mae_",
    r"^gross_ret$",
    r"^net_ret$",
    r"^policy_net_ret$",
    r"^realized_pnl$",
    r"^exit_",
    r"^stake$",
]


def _date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.normalize()


def _fmt_pct(v: float) -> str:
    if pd.isna(v):
        return ""
    return f"{v:.2%}"


def _max_dd(returns: pd.Series, stake: float = 0.2) -> float:
    if returns.empty:
        return 0.0
    eq = 1.0 + (pd.to_numeric(returns, errors="coerce").fillna(0.0) * stake).cumsum()
    peak = eq.cummax()
    return float((eq / peak - 1.0).min())


def _metrics(df: pd.DataFrame, label: str) -> dict:
    r = pd.to_numeric(df.get("net_ret", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    return {
        "variant": label,
        "rows": int(len(df)),
        "unique_dates": int(df["entry_date"].nunique()) if not df.empty else 0,
        "win_rate": float((r > 0).mean()) if len(r) else 0.0,
        "avg_net_ret": float(r.mean()) if len(r) else 0.0,
        "sum_net_ret": float(r.sum()) if len(r) else 0.0,
        "worst_trade": float(r.min()) if len(r) else 0.0,
        "rough_slot20_max_dd": _max_dd(r),
        "avg_score_volume5": float(pd.to_numeric(df.get("score_volume5", pd.Series(dtype=float)), errors="coerce").mean()) if len(df) else 0.0,
        "avg_g3_strong_score": float(pd.to_numeric(df.get("g3_strong_score", pd.Series(dtype=float)), errors="coerce").mean()) if len(df) else 0.0,
    }


def _topn(df: pd.DataFrame, sort_cols: list[str], label: str, n: int = 2) -> pd.DataFrame:
    d = df.copy()
    for c in sort_cols:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.sort_values(["entry_date", *sort_cols], ascending=[True, *([False] * len(sort_cols))])
    d = d.groupby("entry_date", group_keys=False).head(n).copy()
    d["proxy_variant"] = label
    return d


def _visible_selection_columns(variant: str) -> list[str]:
    common = [
        "trade_date",
        "factor_date",
        "entry_date",
        "confirm_datetime",
        "score_volume5",
        "rt_return_from_d1_close",
        "rt_confirm_vs_ma5",
        "rt_confirm_vs_ma10",
        "rt_30m_amount_ratio",
        "l3_rt_strong3_ratio",
        "l2_rt_strong3_ratio",
        "runup_from_60d_low",
    ]
    if variant == "original_g3_strong_score_top2":
        return common + ["g3_strong_score"]
    return common


def _forbidden(cols: list[str]) -> list[str]:
    bad = []
    for c in cols:
        if any(re.search(p, c) for p in FORBIDDEN_SELECTION_PATTERNS):
            bad.append(c)
    return bad


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    src = pd.read_csv(SOURCE, low_memory=False)
    src["trade_date"] = _date(src["trade_date"])
    src["factor_date"] = _date(src["factor_date"])
    src["entry_date"] = _date(src["entry_date"])
    src["confirm_datetime"] = pd.to_datetime(src["confirm_datetime"], errors="coerce")
    src["code"] = src["code"].astype(str)
    src["_key"] = src["entry_date"].dt.strftime("%Y-%m-%d") + "|" + src["code"]

    selected = pd.read_csv(SELECTED, low_memory=False)
    selected = selected[selected["route"].eq("strong_main")].copy()
    selected["entry_date"] = _date(selected["entry_date"])
    selected["code"] = selected["code"].astype(str)
    selected["_key"] = selected["entry_date"].dt.strftime("%Y-%m-%d") + "|" + selected["code"]
    selected_keys = set(selected["_key"])
    src["selected_by_guarded_combo"] = src["_key"].isin(selected_keys)

    visible_base = (
        (src["trade_date"] < src["entry_date"])
        & (src["factor_date"] < src["entry_date"])
        & (src["confirm_datetime"].dt.normalize() == src["entry_date"])
    )
    src["live_visible_base"] = visible_base

    base = src[visible_base].copy()
    intraday_filter = (
        (pd.to_numeric(base["rt_return_from_d1_close"], errors="coerce") >= 0.0)
        & (pd.to_numeric(base["rt_confirm_vs_ma5"], errors="coerce") >= 0.0)
        & (pd.to_numeric(base["rt_30m_amount_ratio"], errors="coerce") >= 1.5)
    )
    sector_filter = (
        (pd.to_numeric(base.get("l3_rt_strong3_ratio"), errors="coerce").fillna(0.0) >= 0.05)
        | (pd.to_numeric(base.get("l2_rt_strong3_ratio"), errors="coerce").fillna(0.0) >= 0.05)
    )

    variants = []
    variants.append(_topn(base, ["g3_strong_score"], "original_g3_strong_score_top2"))
    variants.append(_topn(base, ["score_volume5"], "d1_score_volume5_top2"))
    variants.append(_topn(base[intraday_filter].copy(), ["score_volume5"], "d1_score_volume5_plus_30m_top2"))
    variants.append(_topn(base[intraday_filter & sector_filter].copy(), ["score_volume5"], "d1_score_volume5_plus_30m_sector_top2"))
    variants.append(base[base["selected_by_guarded_combo"]].copy().assign(proxy_variant="actual_guarded_combo_strong_selected"))

    combined = pd.concat(variants, ignore_index=True)
    metrics = pd.DataFrame([_metrics(v, label) for label, v in combined.groupby("proxy_variant", dropna=False)])

    years = []
    for label, g in combined.groupby("proxy_variant", dropna=False):
        for year, gy in g.groupby(g["entry_date"].dt.year):
            m = _metrics(gy, label)
            m["year"] = int(year)
            years.append(m)
    annual = pd.DataFrame(years)

    windows = []
    window_defs = {
        "train_2020_2023": ("2020-01-01", "2023-12-31"),
        "valid_2024_2025": ("2024-01-01", "2025-12-31"),
        "blind_2026ytd": ("2026-01-01", "2026-12-31"),
        "weak_gap_2022_2024": ("2022-01-01", "2024-12-31"),
        "full": ("2020-01-01", "2026-12-31"),
    }
    for label, g in combined.groupby("proxy_variant", dropna=False):
        for w, (start, end) in window_defs.items():
            wg = g[(g["entry_date"] >= pd.Timestamp(start)) & (g["entry_date"] <= pd.Timestamp(end))]
            m = _metrics(wg, label)
            m["window"] = w
            windows.append(m)
    window_metrics = pd.DataFrame(windows)

    selection_audit = pd.DataFrame(
        [
            {
                "variant": v,
                "selection_columns": ",".join(_visible_selection_columns(v)),
                "forbidden_selection_fields": ",".join(_forbidden(_visible_selection_columns(v))),
                "verdict": "PASS" if not _forbidden(_visible_selection_columns(v)) else "FAIL",
            }
            for v in metrics["variant"].tolist()
        ]
    )
    visibility = pd.DataFrame(
        [
            {
                "scope": "strong_source",
                "rows": len(src),
                "visible_base_rows": int(visible_base.sum()),
                "visible_base_rate": float(visible_base.mean()),
                "selected_combo_rows": int(src["selected_by_guarded_combo"].sum()),
                "selected_combo_visible_rows": int((src["selected_by_guarded_combo"] & visible_base).sum()),
            }
        ]
    )

    metrics.to_csv(OUT_DIR / "strong_main_ranking_proxy_metrics.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "strong_main_ranking_proxy_annual_metrics.csv", index=False, encoding="utf-8-sig")
    window_metrics.to_csv(OUT_DIR / "strong_main_ranking_proxy_window_metrics.csv", index=False, encoding="utf-8-sig")
    selection_audit.to_csv(OUT_DIR / "strong_main_ranking_proxy_selection_audit.csv", index=False, encoding="utf-8-sig")
    visibility.to_csv(OUT_DIR / "strong_main_ranking_proxy_visibility.csv", index=False, encoding="utf-8-sig")
    combined.to_csv(OUT_DIR / "strong_main_ranking_proxy_variant_trades.csv", index=False, encoding="utf-8-sig")

    best = metrics.sort_values(["sum_net_ret", "rough_slot20_max_dd"], ascending=[False, False]).head(1)
    best_variant = best.iloc[0]["variant"] if not best.empty else ""
    meta = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_rows": int(len(src)),
        "visible_base_rows": int(visible_base.sum()),
        "selected_combo_rows": int(src["selected_by_guarded_combo"].sum()),
        "best_proxy_variant": best_variant,
        "hard_future_function_found": bool(selection_audit["verdict"].eq("FAIL").any()),
        "next_step": "decide_whether_to_apply_strong_main_ranking_proxy_to_live_safe_payload",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    display_metrics = metrics.copy()
    for c in ["win_rate", "avg_net_ret", "sum_net_ret", "worst_trade", "rough_slot20_max_dd", "avg_score_volume5", "avg_g3_strong_score"]:
        display_metrics[c] = display_metrics[c].map(_fmt_pct)
    display_windows = window_metrics.copy()
    for c in ["win_rate", "avg_net_ret", "sum_net_ret", "worst_trade", "rough_slot20_max_dd", "avg_score_volume5", "avg_g3_strong_score"]:
        display_windows[c] = display_windows[c].map(_fmt_pct)

    report = f"""# G3 strong_main 盘中 ranking proxy 审计 V1

生成时间：{meta["generated_at"]}

## 目的

验证强势链路是否必须依赖研究态的 `g3_strong_score`，还是可以用 D-1 可见的 `score_volume5` 加买入日 30m 可见触发条件替代。

这一步只做审计，不改收益曲线，不接自动下单。

## 可见性

| scope | rows | visible_base_rows | visible_base_rate | selected_combo_rows | selected_combo_visible_rows |
|:--|--:|--:|--:|--:|--:|
| strong_source | {len(src)} | {int(visible_base.sum())} | {visible_base.mean():.2%} | {int(src["selected_by_guarded_combo"].sum())} | {int((src["selected_by_guarded_combo"] & visible_base).sum())} |

`visible_base` 要求：`trade_date < entry_date`、`factor_date < entry_date`、`confirm_datetime` 落在买入日。

## 总体指标

{display_metrics.to_markdown(index=False)}

## 窗口指标

{display_windows.to_markdown(index=False)}

## 选择字段审计

{selection_audit.to_markdown(index=False)}

## 阶段判断

- 如果 `d1_score_volume5_plus_30m_sector_top2` 接近或优于原始 `g3_strong_score`，说明强势链路可以用“D-1 volume5 排序 + 盘中确认”推进到 live-safe。
- 如果收益明显塌陷，说明原始强势收益可能依赖不可替代的研究态排序，需要继续构造更真实的盘中相对强度 proxy。

## 下一步目标

根据本报告，决定是否把 `strong_main` 从 `ranking_proxy_required=true` 升级为明确的 `ranking_proxy_mode`，但仍保持 shadow-only、不自动下单。
"""
    (OUT_DIR / "strong_main_ranking_proxy_audit_report_cn.md").write_text(report, encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
