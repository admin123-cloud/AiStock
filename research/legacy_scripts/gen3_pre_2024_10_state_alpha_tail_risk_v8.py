from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path, reports_root  # noqa: E402


START = pd.Timestamp("2020-01-01")
PRE_END = pd.Timestamp("2024-09-30")
POST_START = pd.Timestamp("2024-10-01")
END = pd.Timestamp("2026-06-18")
SLOT_PCT = 0.25
MAX_SLOTS = 4

V6_DIR = reports_root() / "gen3_pre_2024_10_state_alpha_observe_only_v6"
V7_DIR = reports_root() / "gen3_pre_2024_10_state_alpha_migration_audit_v7"
OUT_DIR = report_path("gen3_pre_2024_10_state_alpha_tail_risk_v8")


def pct(v) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.2%}"


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return np.nan
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def normalize_dates(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["entry_date_norm"] = pd.to_datetime(out["entry_date_norm"], errors="coerce").dt.normalize()
    out["policy_exit_date_norm"] = pd.to_datetime(out["policy_exit_date_norm"], errors="coerce").dt.normalize()
    for col in ["ret_norm", "score", "component_priority", "source_priority", "policy_health_score", "policy_count", "policy_avg_ret", "policy_win_rate", "policy_drawdown"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    if "policy_enabled" in out.columns:
        out["policy_enabled"] = out["policy_enabled"].fillna(False).astype(bool)
    return out


def load_source_candidates() -> pd.DataFrame:
    df = pd.read_csv(V6_DIR / "observe_only_source_candidates.csv", low_memory=False)
    df = normalize_dates(df)
    df["policy_key"] = "always_on"
    df["policy_label"] = "pre_state_alpha 静态源"
    df["policy_enabled"] = True
    df["policy_health_score"] = 999.0
    return df[df["entry_date_norm"].between(START, END) & df["ret_norm"].notna()].copy()


def load_policy_candidates(policy_key: str) -> pd.DataFrame:
    df = pd.read_csv(V7_DIR / "migration_policy_candidate_ledger.csv", low_memory=False)
    df = normalize_dates(df)
    df = df[df["policy_key"].eq(policy_key)].copy()
    return df[df["entry_date_norm"].between(START, END) & df["ret_norm"].notna()].copy()


def portfolio_backtest(candidates: pd.DataFrame, key: str, label: str, stress_additional_cost: float = 0.0) -> tuple[pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame([{"date": START, "equity": 1.0, "event": "init"}])
    d = candidates.copy()
    d["effective_ret"] = pd.to_numeric(d["ret_norm"], errors="coerce").fillna(0.0) - stress_additional_cost
    active: list[dict] = []
    selected: list[dict] = []
    curve_rows: list[dict] = []
    equity = 1.0
    all_dates = sorted(set(d["entry_date_norm"].dropna()) | set(d["policy_exit_date_norm"].dropna()))
    by_date = {date: g.copy() for date, g in d.groupby("entry_date_norm")}

    for current_date in all_dates:
        due = [p for p in active if p["policy_exit_date_norm"] <= current_date]
        if due:
            for p in due:
                equity *= 1.0 + float(p["effective_ret"]) * SLOT_PCT
            active = [p for p in active if p["policy_exit_date_norm"] > current_date]
            curve_rows.append({"date": current_date, "equity": equity, "event": "exit"})

        day = by_date.get(current_date)
        if day is None or len(active) >= MAX_SLOTS:
            continue
        day = day[day["policy_enabled"].astype(bool)].copy()
        if day.empty:
            continue
        order_cols = ["policy_health_score", "component_priority", "source_priority", "score"]
        order_cols = [c for c in order_cols if c in day.columns]
        ranked = day.sort_values(order_cols, ascending=[False] * len(order_cols))
        used_codes = {p["code"] for p in active}
        for _, row in ranked.iterrows():
            if len(active) >= MAX_SLOTS:
                break
            if row["code"] in used_codes:
                continue
            rec = row.to_dict()
            rec["variant_key"] = key
            rec["variant_label"] = label
            active.append(rec)
            selected.append(rec)
            used_codes.add(row["code"])

    if active:
        for p in sorted(active, key=lambda x: x["policy_exit_date_norm"]):
            equity *= 1.0 + float(p["effective_ret"]) * SLOT_PCT
            curve_rows.append({"date": p["policy_exit_date_norm"], "equity": equity, "event": "forced_final_exit"})

    trades = pd.DataFrame(selected)
    curve = pd.DataFrame(curve_rows)
    if curve.empty:
        curve = pd.DataFrame([{"date": d["entry_date_norm"].min(), "equity": 1.0, "event": "init"}])
    return trades, curve


def diagnose(ret: pd.Series, by_year: pd.DataFrame, pos: pd.Series, pos_sum: float) -> str:
    if len(ret) < 20:
        return "交易数太少"
    if float(ret.mean()) < 0.005 or float((ret > 0).mean()) < 0.5:
        return "策略单笔收益太低"
    if not by_year.empty and int((by_year["mean"] > 0).sum()) < 3:
        return "年度稳定性不足"
    if pos_sum > 0 and float(pos.head(5).sum() / pos_sum) > 0.5:
        return "收益集中度偏高"
    return "可继续建模"


def metrics(trades: pd.DataFrame, curve: pd.DataFrame, key: str, label: str, ret_col: str = "effective_ret") -> dict:
    ret = pd.to_numeric(trades.get(ret_col, pd.Series(dtype=float)), errors="coerce").dropna()
    by_year = (
        trades.loc[ret.index].assign(year=lambda x: x["entry_date_norm"].dt.year).groupby("year")[ret_col].agg(["count", "mean"])
        if len(ret)
        else pd.DataFrame()
    )
    pos = ret[ret > 0].sort_values(ascending=False)
    pos_sum = float(pos.sum()) if len(pos) else 0.0
    return {
        "key": key,
        "label": label,
        "trade_count": int(len(ret)),
        "win_rate": float((ret > 0).mean()) if len(ret) else np.nan,
        "avg_ret": float(ret.mean()) if len(ret) else np.nan,
        "median_ret": float(ret.median()) if len(ret) else np.nan,
        "capital_return_proxy": float(curve["equity"].iloc[-1] - 1.0) if not curve.empty else np.nan,
        "capital_max_drawdown_proxy": max_drawdown(curve["equity"]) if not curve.empty else np.nan,
        "worst_trade": float(ret.min()) if len(ret) else np.nan,
        "best_trade": float(ret.max()) if len(ret) else np.nan,
        "top5_positive_share": float(pos.head(5).sum() / pos_sum) if pos_sum > 0 else np.nan,
        "positive_years": int((by_year["mean"] > 0).sum()) if not by_year.empty else 0,
        "min_year_count": int(by_year["count"].min()) if not by_year.empty else 0,
        "diagnosis": diagnose(ret, by_year, pos, pos_sum),
    }


def window_metrics(trades: pd.DataFrame, curve: pd.DataFrame, key: str, label: str) -> pd.DataFrame:
    rows = []
    for window, start, end in [
        ("pre_2024_10", START, PRE_END),
        ("post_2024_10", POST_START, END),
        ("full", START, END),
    ]:
        t = trades[trades["entry_date_norm"].between(start, end)].copy() if not trades.empty else trades
        c = curve[curve["date"].between(start, end)].copy() if not curve.empty else curve
        if not c.empty:
            first = float(c["equity"].iloc[0])
            if first:
                c["equity"] = c["equity"] / first
        row = metrics(t, c, key, label)
        row["window"] = window
        rows.append(row)
    return pd.DataFrame(rows)


def annual_metrics(trades: pd.DataFrame, key: str, label: str) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    rows = []
    for year, g in trades.assign(year=lambda x: x["entry_date_norm"].dt.year).groupby("year"):
        curve = pd.DataFrame({"equity": (1.0 + g["effective_ret"].fillna(0) * SLOT_PCT).cumprod()})
        row = metrics(g, curve, key, label)
        row["year"] = int(year)
        rows.append(row)
    return pd.DataFrame(rows)


def group_metrics(trades: pd.DataFrame, key: str, label: str) -> pd.DataFrame:
    rows = []
    for col in ["component", "route", "route_source"]:
        if trades.empty or col not in trades.columns:
            continue
        for value, g in trades.groupby(col, dropna=False):
            curve = pd.DataFrame({"equity": (1.0 + g["effective_ret"].fillna(0) * SLOT_PCT).cumprod()})
            row = metrics(g, curve, key, label)
            row["group_col"] = col
            row["group_value"] = str(value)
            rows.append(row)
    return pd.DataFrame(rows)


def variant_frames() -> list[tuple[str, str, pd.DataFrame]]:
    source = load_source_candidates()
    route = load_policy_candidates("route_240_loose")
    component = load_policy_candidates("component_240_loose")

    variants: list[tuple[str, str, pd.DataFrame]] = []
    variants.append(("always_on", "pre_state_alpha 静态源", source))
    variants.append(("always_score_cap_1_0", "静态源 + score<=1.0 过热过滤", source[source["score"].fillna(-999) <= 1.0].copy()))

    route_enabled = route[route["policy_enabled"]].copy()
    variants.append(("route_240_loose", "路由 240D 宽松健康度", route_enabled))
    variants.append(("route_240_score_cap_1_0", "路由 240D + score<=1.0", route_enabled[route_enabled["score"].fillna(-999) <= 1.0].copy()))
    variants.append(("route_240_score_cap_0_9", "路由 240D + score<=0.9", route_enabled[route_enabled["score"].fillna(-999) <= 0.9].copy()))
    variants.append(("route_240_dd_floor_12", "路由 240D + 健康回撤>=-12%", route_enabled[route_enabled["policy_drawdown"].fillna(-999) >= -0.12].copy()))
    variants.append(
        (
            "route_240_score_cap_1_0_dd_floor_12",
            "路由 240D + score<=1.0 + 健康回撤>=-12%",
            route_enabled[(route_enabled["score"].fillna(-999) <= 1.0) & (route_enabled["policy_drawdown"].fillna(-999) >= -0.12)].copy(),
        )
    )
    variants.append(
        (
            "route_240_score_cap_1_0_count12",
            "路由 240D + score<=1.0 + 样本数>=12",
            route_enabled[(route_enabled["score"].fillna(-999) <= 1.0) & (route_enabled["policy_count"].fillna(-1) >= 12)].copy(),
        )
    )

    comp_enabled = component[component["policy_enabled"]].copy()
    variants.append(("component_240_loose", "组件 240D 宽松健康度", comp_enabled))
    variants.append(("component_240_score_cap_1_0", "组件 240D + score<=1.0", comp_enabled[comp_enabled["score"].fillna(-999) <= 1.0].copy()))
    return variants


def score_bucket_summary(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    bins = [-np.inf, 0.7, 0.8, 0.9, 1.0, 1.1, np.inf]
    labels = ["<=0.7", "0.7-0.8", "0.8-0.9", "0.9-1.0", "1.0-1.1", ">1.1"]
    out["score_bucket"] = pd.cut(pd.to_numeric(out["score"], errors="coerce"), bins=bins, labels=labels)
    rows = []
    for bucket, g in out.groupby("score_bucket", dropna=False, observed=False):
        ret = pd.to_numeric(g["ret_norm"], errors="coerce").dropna()
        rows.append(
            {
                "score_bucket": str(bucket),
                "trade_count": int(len(ret)),
                "win_rate": float((ret > 0).mean()) if len(ret) else np.nan,
                "avg_ret": float(ret.mean()) if len(ret) else np.nan,
                "median_ret": float(ret.median()) if len(ret) else np.nan,
                "worst_trade": float(ret.min()) if len(ret) else np.nan,
                "bad5_rate": float((ret <= -0.05).mean()) if len(ret) else np.nan,
            }
        )
    return pd.DataFrame(rows)


def worst_trades(trades: pd.DataFrame, limit: int = 30) -> pd.DataFrame:
    cols = [
        "entry_date_norm",
        "policy_exit_date_norm",
        "code",
        "name",
        "component",
        "route",
        "route_source",
        "score",
        "policy_count",
        "policy_avg_ret",
        "policy_win_rate",
        "policy_drawdown",
        "ret_norm",
        "effective_ret",
    ]
    return trades.sort_values("effective_ret").head(limit)[[c for c in cols if c in trades.columns]]


def format_table(df: pd.DataFrame, cols: list[str], limit: int | None = None) -> str:
    if df.empty:
        return "_无数据_"
    out = df.copy()
    if limit:
        out = out.head(limit)
    for col in out.columns:
        if col.endswith("rate") or col in {
            "avg_ret",
            "median_ret",
            "capital_return_proxy",
            "capital_max_drawdown_proxy",
            "worst_trade",
            "best_trade",
            "top5_positive_share",
            "policy_avg_ret",
            "policy_win_rate",
            "policy_drawdown",
            "ret_norm",
            "effective_ret",
        }:
            out[col] = out[col].map(pct)
    return out[[c for c in cols if c in out.columns]].to_markdown(index=False)


def write_report(
    summary: pd.DataFrame,
    windows: pd.DataFrame,
    annual: pd.DataFrame,
    groups: pd.DataFrame,
    stress: pd.DataFrame,
    score_buckets: pd.DataFrame,
    worst: pd.DataFrame,
    contract: dict,
) -> None:
    best = summary.sort_values(["capital_return_proxy", "avg_ret"], ascending=[False, False])
    pre = windows[windows["window"].eq("pre_2024_10")].sort_values("capital_return_proxy", ascending=False)
    stress_view = stress.sort_values(["stress_profile", "capital_return_proxy"], ascending=[True, False])
    report = f"""# G3 2024-10 前 State Alpha 尾部风险审计 v8

## 目标

本阶段继续 v7 的迁移审计，专门回答：`pre_state_alpha` 如果未来要迁移，最先应该挡掉什么风险。

约束保持不变：

- 不写 runtime。
- 不接入当前 G3 shadow。
- 不自动下单。
- 不使用 `2024-10-01` 作为最终切换规则。

## 核心结论

1. 新增发现：老市场的 `score` 过高并不一定更好，`score > 1.0` 在路由健康度通过后反而表现为过热/拥挤尾部。`route_240_score_cap_1_0` 比单纯 `route_240_loose` 更优。
2. `route_240_score_cap_1_0` 全周期 110 笔，胜率 60.91%，平均收益 2.13%，资金曲线代理 +76.79%，最大回撤 -5.64%，最差单笔 -9.29%。
3. 2024-10 前同一规则 78 笔，胜率 60.26%，平均收益 2.04%，资金曲线代理 +47.40%，最大回撤 -5.64%，最差单笔 -8.44%。
4. 这说明 2024-10 前不是缺交易数，而是需要“路由健康度 + 过热过滤”的状态交易合同；泛化到实盘前仍需 observe-only 干跑。
5. 不能把 score cap 立刻写入正式 G3。它是研究级迁移候选，还需要今日候选生成、UI 阻断原因、MTM 对账和 20 个交易日观测。

## 主口径对比

{format_table(best, ["key", "label", "trade_count", "win_rate", "avg_ret", "median_ret", "capital_return_proxy", "capital_max_drawdown_proxy", "worst_trade", "top5_positive_share", "positive_years", "diagnosis"])}

## 2024-10 前窗口

{format_table(pre, ["key", "label", "trade_count", "win_rate", "avg_ret", "median_ret", "capital_return_proxy", "capital_max_drawdown_proxy", "worst_trade", "diagnosis"])}

## 成本压力

这里的压力不是重新撮合，只是在已选交易上叠加额外成本：

- `cost30`：原始 30bps 成本口径。
- `cost100_proxy`：在原始收益上额外扣 70bps。
- `shock2_proxy`：在原始收益上额外扣 2%。

{format_table(stress_view, ["stress_profile", "key", "trade_count", "win_rate", "avg_ret", "capital_return_proxy", "capital_max_drawdown_proxy", "worst_trade", "diagnosis"], limit=80)}

## Score 分层

对象是 `route_240_loose` 健康度通过后的候选池，用来解释为什么要测试 `score<=1.0`。

{format_table(score_buckets, ["score_bucket", "trade_count", "win_rate", "avg_ret", "median_ret", "worst_trade", "bad5_rate"])}

## 路由/组件归因

{format_table(groups.sort_values(["key", "group_col", "capital_return_proxy"], ascending=[True, True, False]), ["key", "group_col", "group_value", "trade_count", "win_rate", "avg_ret", "capital_return_proxy", "worst_trade", "diagnosis"], limit=120)}

## 年度拆分

{format_table(annual.sort_values(["key", "year"]), ["key", "year", "trade_count", "win_rate", "avg_ret", "median_ret", "capital_return_proxy", "capital_max_drawdown_proxy", "worst_trade", "diagnosis"], limit=120)}

## `route_240_score_cap_1_0` 最差交易

{format_table(worst, ["entry_date_norm", "policy_exit_date_norm", "code", "name", "component", "route", "score", "policy_count", "policy_avg_ret", "policy_win_rate", "policy_drawdown", "ret_norm"], limit=30)}

## 迁移判断

可以进入下一步 observe-only 合同升级研究：

- `route_240_score_cap_1_0`：作为 pre_state_alpha 的首选研究门控。
- 阻断理由应显式输出：`route_health_not_ready`、`score_overheat_gt_1_0`、`observe_only_not_orderable`。
- 资金合同仍建议不超过单票 25%，且交易开放前必须复核真实日更候选。

仍然不能进入交易链路：

- v8 仍基于历史归档，不是今日实时候选生成。
- score cap 是研究发现，需要干跑验证是否会错杀当前市场的有效强势。
- 当前正式/影子 G3 仍以现有状态路由为准，不自动替换。

## 机器合同

```json
{json.dumps(contract, ensure_ascii=False, indent=2)}
```
"""
    (OUT_DIR / "REPORT_CN.md").write_text(report, encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    variants = variant_frames()
    summary_rows = []
    window_frames = []
    annual_frames = []
    group_frames = []
    stress_rows = []
    selected_by_key = {}

    for key, label, frame in variants:
        trades, curve = portfolio_backtest(frame, key, label, stress_additional_cost=0.0)
        selected_by_key[key] = trades
        trades.to_csv(OUT_DIR / f"{key}_selected.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(OUT_DIR / f"{key}_curve.csv", index=False, encoding="utf-8-sig")
        summary_rows.append(metrics(trades, curve, key, label))
        window_frames.append(window_metrics(trades, curve, key, label))
        annual_frames.append(annual_metrics(trades, key, label))
        group_frames.append(group_metrics(trades, key, label))

        for stress_key, extra_cost in [("cost30", 0.0), ("cost100_proxy", 0.007), ("shock2_proxy", 0.02)]:
            st, sc = portfolio_backtest(frame, key, label, stress_additional_cost=extra_cost)
            row = metrics(st, sc, key, label)
            row["stress_profile"] = stress_key
            stress_rows.append(row)

    summary = pd.DataFrame(summary_rows)
    windows = pd.concat(window_frames, ignore_index=True)
    annual = pd.concat([x for x in annual_frames if not x.empty], ignore_index=True)
    groups = pd.concat([x for x in group_frames if not x.empty], ignore_index=True)
    stress = pd.DataFrame(stress_rows)

    route_enabled = load_policy_candidates("route_240_loose")
    route_enabled = route_enabled[route_enabled["policy_enabled"]].copy()
    score_buckets = score_bucket_summary(route_enabled)
    worst = worst_trades(selected_by_key.get("route_240_score_cap_1_0", pd.DataFrame()))

    contract = {
        "strategy_id": "pre_state_alpha_tail_risk_audit_v8",
        "purpose": "research_tail_risk_and_promotion_gate",
        "writes_to_runtime": False,
        "feeds_shadow_trading": False,
        "auto_order_allowed": False,
        "formal_buy_signal": False,
        "order_path_enabled": False,
        "recommended_observe_only_candidate": "route_240_score_cap_1_0",
        "candidate_gate": {
            "rolling_health": "route_240_loose",
            "score_max": 1.0,
            "uses_future_return_for_selection": False,
            "hard_date_final_rule_allowed": False,
        },
        "block_reasons": ["route_health_not_ready", "score_overheat_gt_1_0", "observe_only_not_orderable"],
        "promotion_requirements": [
            "rebuild today candidate generator with score cap visible",
            "20 trading-day observe-only dry run",
            "MTM ledger parity",
            "UI block reason parity",
            "explicit user approval before shadow integration",
        ],
    }

    summary.to_csv(OUT_DIR / "tail_risk_summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "tail_risk_window_summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "tail_risk_annual_summary.csv", index=False, encoding="utf-8-sig")
    groups.to_csv(OUT_DIR / "tail_risk_group_summary.csv", index=False, encoding="utf-8-sig")
    stress.to_csv(OUT_DIR / "tail_risk_stress_summary.csv", index=False, encoding="utf-8-sig")
    score_buckets.to_csv(OUT_DIR / "tail_risk_score_bucket_summary.csv", index=False, encoding="utf-8-sig")
    worst.to_csv(OUT_DIR / "tail_risk_route240_scorecap_worst_trades.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "tail_risk_contract_v8.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(summary, windows, annual, groups, stress, score_buckets, worst, contract)

    print(f"wrote {OUT_DIR}")
    print(summary.sort_values(["capital_return_proxy", "avg_ret"], ascending=[False, False]).to_string(index=False))


if __name__ == "__main__":
    main()
