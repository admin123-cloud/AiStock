from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "gen3_range_filtered_haircut2_failure_v1"

STRESS_TRADES = (
    ROOT
    / "reports"
    / "gen3_combo_range_filter_execution_stress_v1"
    / "nextopen_haircut2_30bps_closed_trades.csv"
)
PANIC_SOURCE = (
    ROOT
    / "reports"
    / "gen3_panic_v2_research"
    / "final_candidate_v1"
    / "m30_close5_full_nextopen_cost30_closed_trades.csv"
)
RANGE_SOURCE = (
    ROOT
    / "reports"
    / "gen3_range_v3_mtm_pressure_v1"
    / "range_v3_weak_low_not_chasing_h5_cost30_closed_trades.csv"
)

NUMERIC_FEATURES = [
    "score",
    "candidate_score",
    "up_rate",
    "big_down_rate",
    "limit_down_proxy_rate",
    "market_amount_ratio20",
    "adx20",
    "plus_di20",
    "minus_di20",
    "index_mom20",
    "mom20",
    "drawdown10",
    "drawdown20",
    "runup_from_60d_low",
    "range_pos20",
    "range_pos60",
    "amount_ratio5",
    "amount_ratio20",
    "turnover_rate",
    "lower_shadow_ratio",
    "close_position",
    "gap_open",
    "box_width60",
    "bar_ret",
    "bar_close_pos",
    "amount_ratio3",
]

CATEGORICAL_FEATURES = [
    "route",
    "market_style",
    "ma_skeleton",
    "volume_price_layer",
    "adx_layer",
    "up_rate_bucket",
    "big_down_bucket",
    "range_pos60_bucket",
    "drawdown20_bucket",
    "liquidity_amount20_bucket",
    "g3_position_guard",
    "g3_capitulation_strength",
    "g3_volume_context",
    "g3_repair_env_label",
    "range_v3_family",
]


def _date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.strftime("%Y-%m-%d")


def _key(df: pd.DataFrame) -> pd.Series:
    return _date(df["entry_date"]) + "|" + df["code"].astype(str)


def _prefixed_source(path: Path, route: str) -> pd.DataFrame:
    d = pd.read_csv(path, low_memory=False)
    d["code"] = d["code"].astype(str)
    d["entry_date"] = _date(d["entry_date"])
    d["_key"] = d["entry_date"] + "|" + d["code"]
    d["route"] = route
    keep = ["_key", "route", "entry_date", "code", "name"]
    for col in NUMERIC_FEATURES + CATEGORICAL_FEATURES:
        if col in d.columns and col not in keep:
            keep.append(col)
    return d[keep].drop_duplicates("_key", keep="first")


def _category_stats(d: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for col in CATEGORICAL_FEATURES:
        if col not in d.columns:
            continue
        g = d.groupby(col, dropna=False)
        for value, part in g:
            if len(part) < 5:
                continue
            rows.append(
                {
                    "feature": col,
                    "value": value,
                    "trade_count": int(len(part)),
                    "avg_stress_ret": float(part["stress_net_ret"].mean()),
                    "win_rate": float((part["stress_net_ret"] > 0).mean()),
                    "bad5_rate": float((part["stress_net_ret"] <= -0.05).mean()),
                    "worst": float(part["stress_net_ret"].min()),
                }
            )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values(["bad5_rate", "avg_stress_ret", "trade_count"], ascending=[False, True, False])


def _numeric_contrast(d: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    d = d.copy()
    d["is_bad5"] = d["stress_net_ret"] <= -0.05
    d["is_win"] = d["stress_net_ret"] > 0
    for col in NUMERIC_FEATURES:
        if col not in d.columns:
            continue
        values = pd.to_numeric(d[col], errors="coerce")
        if values.notna().sum() < 10:
            continue
        bad = values[d["is_bad5"]]
        good = values[d["is_win"]]
        allv = values.dropna()
        rows.append(
            {
                "feature": col,
                "all_median": float(allv.median()) if len(allv) else None,
                "bad5_median": float(bad.median()) if len(bad) else None,
                "win_median": float(good.median()) if len(good) else None,
                "bad_minus_win": float(bad.median() - good.median()) if len(bad) and len(good) else None,
                "coverage": int(values.notna().sum()),
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values("bad_minus_win", key=lambda s: s.abs(), ascending=False)


def _route_summary(d: pd.DataFrame) -> pd.DataFrame:
    return (
        d.groupby("route", dropna=False)
        .agg(
            trade_count=("code", "size"),
            avg_stress_ret=("stress_net_ret", "mean"),
            win_rate=("stress_net_ret", lambda s: float((s > 0).mean())),
            bad5_rate=("stress_net_ret", lambda s: float((s <= -0.05).mean())),
            worst=("stress_net_ret", "min"),
        )
        .reset_index()
        .sort_values("avg_stress_ret")
    )


def _top_losers(d: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "route",
        "entry_date",
        "stress_exit_date",
        "code",
        "name",
        "stress_net_ret",
        "market_style",
        "adx_layer",
        "gap_open",
        "range_pos60",
        "drawdown20",
        "runup_from_60d_low",
        "amount_ratio20",
        "close_position",
        "g3_capitulation_strength",
        "g3_volume_context",
    ]
    cols = [c for c in cols if c in d.columns]
    return d.sort_values("stress_net_ret").head(40)[cols]


def _pct(v: object) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.2%}"


def _md(df: pd.DataFrame, pct_cols: set[str]) -> str:
    if df.empty:
        return "_无数据_"
    out = df.copy()
    for col in pct_cols:
        if col in out.columns:
            out[col] = out[col].map(_pct)
    for col in out.columns:
        if col not in pct_cols and pd.api.types.is_float_dtype(out[col]):
            out[col] = out[col].map(lambda v: "" if pd.isna(v) else f"{v:.4f}")
    return out.to_markdown(index=False)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    stress = pd.read_csv(STRESS_TRADES, low_memory=False)
    stress = stress[stress["route"].isin(["down_panic", "range_gap"])].copy()
    stress["code"] = stress["code"].astype(str)
    stress["entry_date"] = _date(stress["entry_date"])
    stress["_key"] = _key(stress)
    stress["stress_net_ret"] = pd.to_numeric(stress["stress_net_ret"], errors="coerce")

    sources = pd.concat(
        [
            _prefixed_source(PANIC_SOURCE, "down_panic"),
            _prefixed_source(RANGE_SOURCE, "range_gap"),
        ],
        ignore_index=True,
    )
    joined = stress.merge(sources.drop(columns=["entry_date", "code", "name", "route"], errors="ignore"), on="_key", how="left")

    route_summary = _route_summary(joined)
    category_stats = _category_stats(joined)
    numeric_contrast = _numeric_contrast(joined)
    top_losers = _top_losers(joined)

    joined.to_csv(OUT_DIR / "haircut2_failure_joined_samples.csv", index=False, encoding="utf-8-sig")
    route_summary.to_csv(OUT_DIR / "haircut2_failure_route_summary.csv", index=False, encoding="utf-8-sig")
    category_stats.to_csv(OUT_DIR / "haircut2_failure_category_stats.csv", index=False, encoding="utf-8-sig")
    numeric_contrast.to_csv(OUT_DIR / "haircut2_failure_numeric_contrast.csv", index=False, encoding="utf-8-sig")
    top_losers.to_csv(OUT_DIR / "haircut2_failure_top_losers.csv", index=False, encoding="utf-8-sig")

    route_pain = route_summary.iloc[0].to_dict() if not route_summary.empty else {}
    worst_cats = category_stats.head(12)
    strongest_numeric = numeric_contrast.head(12)

    summary = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sample_count": int(len(joined)),
        "bad5_count": int((joined["stress_net_ret"] <= -0.05).sum()),
        "worst_route": route_pain.get("route"),
        "worst_route_avg_stress_ret": route_pain.get("avg_stress_ret"),
        "next_step": "test_structural_veto_for_down_panic_and_range_gap_haircut2",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    report = f"""# G3 range-filtered 极端冲击失败归因 V1

生成时间：{summary["generated_at"]}

## 英文名解释

- `down_panic`：弱势市场恐慌买法，目标是买在非理性出清后的修复点。
- `range_gap`：横盘/弱反弹买法，来自箱体底部或弱反弹缺口修复机会。
- `nextopen_haircut2_30bps`：最严苛执行压力，按次日开盘退出并额外扣 2%，模拟低开、滑点和冲击成本。
- `bad5`：单笔在该压力口径下亏损超过 5%，用于识别真正拖累组合的失败样本。

## 总体结论

这次只审计 `down_panic` 和 `range_gap`，共 {len(joined)} 笔；其中 `bad5` 失败样本 {(joined["stress_net_ret"] <= -0.05).sum()} 笔。

初步结论：极端冲击下的问题不是 score/rank 排名，而是结构性安全垫不足。下一步应该验证结构 veto，而不是继续调排序。

## 路由表现

{_md(route_summary, {"avg_stress_ret", "win_rate", "bad5_rate", "worst"})}

## 最差类别特征

{_md(worst_cats, {"avg_stress_ret", "win_rate", "bad5_rate", "worst"})}

## 数值特征对比

{_md(strongest_numeric, set())}

## 最大亏损样本

{_md(top_losers, {"stress_net_ret", "gap_open", "range_pos60", "drawdown20", "runup_from_60d_low", "amount_ratio20", "close_position"})}

## 下一步目标

做一个小型结构 veto 复验：

1. 对 `range_gap` 优先测试：箱体位置过高、非底部修复、放量异常、趋势仍下跌这些条件能否减少 `bad5`。
2. 对 `down_panic` 优先测试：极端恐慌但没有修复承接、低安全边际不足、成交量语义偏出货的样本能否被剔除。
3. 所有 veto 只能用入场前或入场当时可见字段，不能使用未来收益字段。
"""
    (OUT_DIR / "haircut2_failure_report_cn.md").write_text(report, encoding="utf-8", newline="\n")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
