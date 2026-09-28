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
RANGE_DIR = _report_path() / "gen3_range_v3_mtm_pressure_v1"
STRESS_DIR = _report_path() / "gen3_guarded_execution_stress_v1"
OUT_DIR = _report_path() / "gen3_range_gap_cost_execution_noise_audit_v1"

PURE_RANGE_FILES = {
    "range_h5_cost30": RANGE_DIR / "range_v3_weak_low_not_chasing_h5_cost30_closed_trades.csv",
    "range_h5_cost50": RANGE_DIR / "range_v3_weak_low_not_chasing_h5_cost50_closed_trades.csv",
    "range_h5_cost100": RANGE_DIR / "range_v3_weak_low_not_chasing_h5_cost100_closed_trades.csv",
}
COMBO_STRESS_FILES = {
    "combo_close_100bps": STRESS_DIR / "close_100bps_closed_trades.csv",
    "combo_nextopen_haircut2_30bps": STRESS_DIR / "nextopen_haircut2_30bps_closed_trades.csv",
    "combo_nextopen_limitdown_30bps": STRESS_DIR / "nextopen_limitdown_30bps_closed_trades.csv",
}


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, low_memory=False) if path.exists() else pd.DataFrame()


def _date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.normalize()


def _pct(v: float | int | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.2%}"


def _metrics(df: pd.DataFrame, profile: str, ret_col: str = "policy_net_ret") -> dict:
    r = pd.to_numeric(df.get(ret_col, pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    return {
        "profile": profile,
        "rows": int(len(df)),
        "unique_dates": int(df["entry_date"].nunique()) if "entry_date" in df and not df.empty else 0,
        "win_rate": float((r > 0).mean()) if len(r) else 0.0,
        "avg_ret": float(r.mean()) if len(r) else 0.0,
        "sum_ret": float(r.sum()) if len(r) else 0.0,
        "worst_trade": float(r.min()) if len(r) else 0.0,
    }


def _add_buckets(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    bucket_specs = {
        "gap_open": [-1, -0.05, -0.02, 0, 0.02, 0.05, 1],
        "amount_ratio20": [-1, 0.6, 0.9, 1.2, 1.8, 3, 100],
        "range_pos60": [-1, 0.15, 0.3, 0.5, 0.7, 1.01],
        "runup_from_60d_low": [-1, 0.15, 0.3, 0.6, 1.0, 100],
        "drawdown20": [-1, -0.3, -0.2, -0.1, 0, 1],
        "close_position": [-1, 0.25, 0.5, 0.75, 1.01],
        "lower_shadow_ratio": [-1, 0.05, 0.15, 0.3, 0.5, 10],
        "range_v3_score": [-1, 0.4, 0.6, 0.75, 0.9, 10],
        "box_width60": [-1, 0.3, 0.5, 0.7, 0.9, 10],
        "market_amount_ratio20": [-1, 0.8, 1.0, 1.2, 1.5, 10],
    }
    for col, bins in bucket_specs.items():
        if col in d.columns:
            d[f"{col}_bucket"] = pd.cut(pd.to_numeric(d[col], errors="coerce"), bins=bins, include_lowest=True)
    return d


def _bucket_summary(df: pd.DataFrame, profile: str, ret_col: str, cols: list[str]) -> pd.DataFrame:
    rows = []
    for col in cols:
        if col not in df.columns:
            continue
        for val, g in df.groupby(col, dropna=False, observed=False):
            if len(g) < 8:
                continue
            m = _metrics(g, profile, ret_col)
            m["feature"] = col
            m["bucket"] = str(val)
            rows.append(m)
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pure_parts = []
    for profile, path in PURE_RANGE_FILES.items():
        d = _read_csv(path)
        if d.empty:
            continue
        d["profile"] = profile
        d["entry_date"] = _date(d["entry_date"])
        d["trade_date"] = _date(d["trade_date"])
        d["code"] = d["code"].astype(str)
        d["_key"] = d["entry_date"].dt.strftime("%Y-%m-%d") + "|" + d["code"]
        pure_parts.append(_add_buckets(d))
    pure = pd.concat(pure_parts, ignore_index=True) if pure_parts else pd.DataFrame()

    feature_base = pure[pure["profile"].eq("range_h5_cost30")].drop_duplicates("_key", keep="first").copy() if not pure.empty else pd.DataFrame()

    combo_parts = []
    for profile, path in COMBO_STRESS_FILES.items():
        d = _read_csv(path)
        if d.empty:
            continue
        d = d[d["route"].eq("range_gap")].copy()
        d["profile"] = profile
        d["entry_date"] = _date(d["entry_date"])
        d["code"] = d["code"].astype(str)
        d["_key"] = d["entry_date"].dt.strftime("%Y-%m-%d") + "|" + d["code"]
        ret_col = "stress_net_ret" if "stress_net_ret" in d.columns else "policy_net_ret"
        d["audit_ret"] = pd.to_numeric(d[ret_col], errors="coerce")
        if not feature_base.empty:
            keep_cols = [
                "_key",
                "trade_date",
                "market_style",
                "g3_chain",
                "volume_price_layer",
                "adx_layer",
                "up_rate",
                "big_down_rate",
                "limit_down_proxy_rate",
                "market_amount_ratio20",
                "gap_open",
                "amount_ratio20",
                "range_pos60",
                "runup_from_60d_low",
                "drawdown20",
                "close_position",
                "lower_shadow_ratio",
                "range_v3_score",
                "box_width60",
                "reclaim_flag",
                "rank_in_day",
                "hold_days",
            ]
            d = d.merge(feature_base[[c for c in keep_cols if c in feature_base.columns]], on="_key", how="left", suffixes=("", "_feature"))
        combo_parts.append(_add_buckets(d))
    combo = pd.concat(combo_parts, ignore_index=True) if combo_parts else pd.DataFrame()

    pure_summary = pd.DataFrame([_metrics(g, p) for p, g in pure.groupby("profile", dropna=False)]) if not pure.empty else pd.DataFrame()
    combo_summary = pd.DataFrame([_metrics(g, p, "audit_ret") for p, g in combo.groupby("profile", dropna=False)]) if not combo.empty else pd.DataFrame()

    bucket_cols = [
        "market_style",
        "g3_chain",
        "volume_price_layer",
        "adx_layer",
        "reclaim_flag",
        "gap_open_bucket",
        "amount_ratio20_bucket",
        "range_pos60_bucket",
        "runup_from_60d_low_bucket",
        "drawdown20_bucket",
        "close_position_bucket",
        "lower_shadow_ratio_bucket",
        "range_v3_score_bucket",
        "box_width60_bucket",
        "market_amount_ratio20_bucket",
    ]
    pure_bucket = pd.concat(
        [
            _bucket_summary(g, profile, "policy_net_ret", bucket_cols)
            for profile, g in pure.groupby("profile", dropna=False)
        ],
        ignore_index=True,
    ) if not pure.empty else pd.DataFrame()
    combo_bucket = pd.concat(
        [
            _bucket_summary(g, profile, "audit_ret", bucket_cols)
            for profile, g in combo.groupby("profile", dropna=False)
        ],
        ignore_index=True,
    ) if not combo.empty else pd.DataFrame()

    bad_buckets = combo_bucket[
        (combo_bucket["profile"].isin(["combo_close_100bps", "combo_nextopen_haircut2_30bps"]))
        & (combo_bucket["rows"] >= 12)
        & ((combo_bucket["avg_ret"] < 0) | (combo_bucket["sum_ret"] < 0))
    ].sort_values(["profile", "sum_ret", "avg_ret"]).copy() if not combo_bucket.empty else pd.DataFrame()

    # Robustness candidates are broad, interpretable exclusions only; this is audit, not final selection.
    candidates = []
    for _, row in bad_buckets.head(12).iterrows():
        candidates.append(
            {
                "source_profile": row["profile"],
                "feature": row["feature"],
                "bucket": row["bucket"],
                "rows": row["rows"],
                "avg_ret": row["avg_ret"],
                "sum_ret": row["sum_ret"],
                "candidate_action": "仅列为降噪候选，需做 train/valid/blind 复验后才能进入规则",
            }
        )
    candidate_df = pd.DataFrame(candidates)

    pure.to_csv(OUT_DIR / "range_gap_pure_joined_profiles.csv", index=False, encoding="utf-8-sig")
    combo.to_csv(OUT_DIR / "range_gap_combo_stress_joined_profiles.csv", index=False, encoding="utf-8-sig")
    pure_summary.to_csv(OUT_DIR / "range_gap_pure_cost_summary.csv", index=False, encoding="utf-8-sig")
    combo_summary.to_csv(OUT_DIR / "range_gap_combo_stress_summary.csv", index=False, encoding="utf-8-sig")
    pure_bucket.to_csv(OUT_DIR / "range_gap_pure_bucket_summary.csv", index=False, encoding="utf-8-sig")
    combo_bucket.to_csv(OUT_DIR / "range_gap_combo_bucket_summary.csv", index=False, encoding="utf-8-sig")
    bad_buckets.to_csv(OUT_DIR / "range_gap_bad_buckets.csv", index=False, encoding="utf-8-sig")
    candidate_df.to_csv(OUT_DIR / "range_gap_noise_reduction_candidates.csv", index=False, encoding="utf-8-sig")

    display_combo = combo_summary.copy()
    for c in ["win_rate", "avg_ret", "sum_ret", "worst_trade"]:
        if c in display_combo:
            display_combo[c] = display_combo[c].map(_pct)
    display_pure = pure_summary.copy()
    for c in ["win_rate", "avg_ret", "sum_ret", "worst_trade"]:
        if c in display_pure:
            display_pure[c] = display_pure[c].map(_pct)
    display_bad = bad_buckets.head(20).copy()
    for c in ["win_rate", "avg_ret", "sum_ret", "worst_trade"]:
        if c in display_bad:
            display_bad[c] = display_bad[c].map(_pct)
    display_candidates = candidate_df.copy()
    for c in ["avg_ret", "sum_ret"]:
        if c in display_candidates:
            display_candidates[c] = display_candidates[c].map(_pct)

    close100 = combo_summary[combo_summary["profile"].eq("combo_close_100bps")].iloc[0].to_dict() if not combo_summary.empty else {}
    haircut = combo_summary[combo_summary["profile"].eq("combo_nextopen_haircut2_30bps")].iloc[0].to_dict() if not combo_summary.empty else {}
    meta = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "combo_close_100bps_range_rows": int(close100.get("rows", 0)),
        "combo_close_100bps_range_avg_ret": close100.get("avg_ret"),
        "combo_close_100bps_range_sum_ret": close100.get("sum_ret"),
        "combo_haircut2_range_avg_ret": haircut.get("avg_ret"),
        "combo_haircut2_range_sum_ret": haircut.get("sum_ret"),
        "bad_bucket_count": int(len(bad_buckets)),
        "next_step": "test_broad_range_gap_noise_filters_without_overfitting",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = f"""# G3 range_gap 成本敏感与执行噪声审计 V1

生成时间：{meta["generated_at"]}

## 目的

专门拆解 `range_gap` 为什么在组合 100bps 与极端次日低开压力下拖后腿。

这一步只做归因，不直接改正式规则，避免为了修一个窗口而过拟合。

## 纯 range 链路成本表现

{display_pure.to_markdown(index=False) if not display_pure.empty else "_无数据_"}

## 组合压力中的 range_gap 表现

{display_combo.to_markdown(index=False) if not display_combo.empty else "_无数据_"}

## 主要坏桶

以下坏桶满足：样本数不少于 12，且在 `combo_close_100bps` 或 `combo_nextopen_haircut2_30bps` 下平均收益/总收益为负。

{display_bad.to_markdown(index=False) if not display_bad.empty else "_无明显坏桶_"}

## 降噪候选

这些不是正式规则，只是下一步要做 train/valid/blind 复验的方向。

{display_candidates.to_markdown(index=False) if not display_candidates.empty else "_暂无候选_"}

## 判断

`range_gap` 的问题不是“完全没收益”，而是收益太薄，遇到 100bps 与隔夜开盘冲击后安全垫不够。下一步应该只测试少数宽口径、可解释的降噪过滤，不能做大量参数搜索。
"""
    (OUT_DIR / "range_gap_cost_execution_noise_audit_report_cn.md").write_text(report, encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
