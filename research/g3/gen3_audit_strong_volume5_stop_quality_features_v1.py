from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import sys
from pathlib import Path

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import SOURCE, _classify_market_style, _md_table


OUT_DIR = _report_path() / "gen3_strong_volume5_stop_quality_features_v1"

FEATURES = [
    "v4_rank",
    "v4_score",
    "mom5",
    "mom10",
    "mom20",
    "vol_ratio",
    "vol10",
    "rt_return_from_d1_close",
    "rt_confirm_vs_ma5",
    "rt_confirm_vs_ma10",
    "rt_30m_amount_ratio",
    "l1_rt_avg_return",
    "l1_rt_strong3_ratio",
    "l1_rt_strong5_ratio",
    "l2_rt_avg_return",
    "l2_rt_strong3_ratio",
    "l2_rt_strong5_ratio",
    "l3_rt_avg_return",
    "l3_rt_strong3_ratio",
    "l3_rt_strong5_ratio",
    "sector_score_bonus",
    "float_market_cap_yi",
    "cap_pressure_amount_share",
    "overhead_pressure_amount_share",
    "prior_max_high_ratio",
    "runup_from_60d_low",
    "recent60_return",
    "close_vs_prior20_high",
    "close_vs_prior60_high",
]


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def _load() -> pd.DataFrame:
    df = pd.read_parquet(SOURCE)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.normalize()
    df = df[df["source_family"].eq("volume5")].copy()
    df["h5"] = pd.to_numeric(df["outcome_fwd_ret_5d"], errors="coerce").where(
        pd.to_numeric(df["outcome_fwd_ret_5d"], errors="coerce").notna(),
        pd.to_numeric(df["fwd_ret_5d"], errors="coerce"),
    )
    df["stop5_touch_30m"] = df.get("stop5_touch_30m", False).fillna(False).astype(bool)
    df["g3_market_style"] = df.apply(_classify_market_style, axis=1)
    df = df[df["g3_market_style"].isin(["main_up", "weak_recovery"])].dropna(subset=["h5"]).copy()
    for col in FEATURES:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _feature_compare(stopped: pd.DataFrame) -> pd.DataFrame:
    rows = []
    stopped = stopped.copy()
    stopped["recovered"] = stopped["h5"] > 0
    for col in FEATURES:
        if col not in stopped.columns:
            continue
        a = stopped.loc[stopped["recovered"], col].dropna()
        b = stopped.loc[~stopped["recovered"], col].dropna()
        if len(a) < 10 or len(b) < 10:
            continue
        rows.append(
            {
                "feature": col,
                "recovered_n": int(len(a)),
                "failed_n": int(len(b)),
                "recovered_mean": float(a.mean()),
                "failed_mean": float(b.mean()),
                "spread_recovered_minus_failed": float(a.mean() - b.mean()),
                "recovered_median": float(a.median()),
                "failed_median": float(b.median()),
                "median_spread": float(a.median() - b.median()),
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["abs_spread"] = out["spread_recovered_minus_failed"].abs()
    return out.sort_values("abs_spread", ascending=False).drop(columns=["abs_spread"])


def _bucket_feature(df: pd.DataFrame, feature: str, q: int = 4) -> pd.DataFrame:
    d = df.dropna(subset=[feature]).copy()
    if d[feature].nunique() < q:
        return pd.DataFrame()
    d["bucket"] = pd.qcut(d[feature], q=q, duplicates="drop")
    rows = []
    for bucket, g in d.groupby("bucket", observed=True):
        rows.append(
            {
                "feature": feature,
                "bucket": str(bucket),
                "rows": int(len(g)),
                "h5_mean": float(g["h5"].mean()),
                "h5_win": float((g["h5"] > 0).mean()),
                "bad10_rate": float((g["h5"] <= -0.10).mean()),
                "stop_rate": float(g["stop5_touch_30m"].mean()),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = _load()
    stopped = df[df["stop5_touch_30m"]].copy()
    compare = _feature_compare(stopped)
    compare.to_csv(OUT_DIR / "stopped_recovered_vs_failed_feature_compare.csv", index=False, encoding="utf-8-sig")

    top_features = compare.head(8)["feature"].tolist() if not compare.empty else []
    buckets = []
    for feature in top_features:
        b = _bucket_feature(stopped, feature)
        if not b.empty:
            buckets.append(b)
    bucket_df = pd.concat(buckets, ignore_index=True) if buckets else pd.DataFrame()
    bucket_df.to_csv(OUT_DIR / "top_feature_buckets_on_stopped.csv", index=False, encoding="utf-8-sig")

    stopped_preview_cols = [c for c in ["entry_date", "code", "name", "g3_market_style", "h5", *top_features] if c in stopped.columns]
    stopped.sort_values("h5", ascending=False).head(30)[stopped_preview_cols].to_csv(OUT_DIR / "stopped_best_recovery_samples.csv", index=False, encoding="utf-8-sig")
    stopped.sort_values("h5", ascending=True).head(30)[stopped_preview_cols].to_csv(OUT_DIR / "stopped_worst_failure_samples.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"h5_mean", "h5_win", "bad10_rate", "stop_rate"}
    lines = [
        "# G3 强势链路 Stop 后质量特征审计 V1",
        "",
        "## 口径",
        "",
        "- 样本：`core_recovery_volume5` 中已触发 `stop5_touch_30m` 的候选。",
        "- 目标：比较触发 stop 后 5 日仍转正的样本，与继续失败样本的入场可见特征。",
        "- 这一步只做诊断，不把单个特征直接变成交易规则，避免过拟合。",
        "",
        "## 修复 vs 失败特征差异",
        "",
        _md_table(compare.head(20)),
        "",
        "## Top 特征分桶",
        "",
        _md_table(bucket_df, pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 如果差异主要来自 `rt_return_from_d1_close`、`rt_confirm_vs_ma5/ma10`、板块强度等盘中可见字段，后续可以把确认失败改成实时 30m 规则。",
        "- 如果差异主要来自不可稳定解释的个股历史收益或极端样本，则不应继续往这个方向收窄参数。",
        "",
    ]
    (OUT_DIR / "stop_quality_features_report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    print(
        {
            "out_dir": str(OUT_DIR),
            "core_rows": len(df),
            "stopped_rows": len(stopped),
            "recovered_rows": int((stopped["h5"] > 0).sum()),
            "top_features": top_features,
        }
    )


if __name__ == "__main__":
    main()
