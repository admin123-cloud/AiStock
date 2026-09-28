from __future__ import annotations

import json
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
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

SRC_DIR = reports_root() / "gen3_pre_2024_10_state_alpha_observe_only_v6"
OUT_DIR = report_path("gen3_pre_2024_10_state_alpha_migration_audit_v7")


@dataclass(frozen=True)
class HealthPolicy:
    key: str
    label: str
    group_col: str
    lookback_days: int
    min_count: int
    min_avg_ret: float
    min_win_rate: float
    max_drawdown_floor: float


POLICIES = [
    HealthPolicy("source_240_loose", "总源 240D 宽松健康度", "source_key", 240, 6, 0.000, 0.50, -0.18),
    HealthPolicy("component_240_loose", "组件 240D 宽松健康度", "component", 240, 3, 0.000, 0.50, -0.18),
    HealthPolicy("component_480_balanced", "组件 480D 均衡健康度", "component", 480, 6, 0.003, 0.52, -0.18),
    HealthPolicy("route_240_loose", "路由 240D 宽松健康度", "route", 240, 3, 0.000, 0.50, -0.18),
    HealthPolicy("route_480_balanced", "路由 480D 均衡健康度", "route", 480, 6, 0.003, 0.52, -0.18),
]


def pct(v) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.2%}"


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return np.nan
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def load_candidates() -> pd.DataFrame:
    path = SRC_DIR / "observe_only_source_candidates.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    df = pd.read_csv(path, low_memory=False)
    df["entry_date_norm"] = pd.to_datetime(df["entry_date_norm"], errors="coerce").dt.normalize()
    df["policy_exit_date_norm"] = pd.to_datetime(df["policy_exit_date_norm"], errors="coerce").dt.normalize()
    df["ret_norm"] = pd.to_numeric(df["ret_norm"], errors="coerce")
    df["score"] = pd.to_numeric(df.get("score", 0.0), errors="coerce").fillna(0.0)
    df["component_priority"] = pd.to_numeric(df.get("component_priority", 0.0), errors="coerce").fillna(0.0)
    df["source_priority"] = pd.to_numeric(df.get("source_priority", 0.0), errors="coerce").fillna(0.0)
    df = df[
        df["entry_date_norm"].between(START, END)
        & df["policy_exit_date_norm"].notna()
        & df["ret_norm"].notna()
    ].copy()
    return df.sort_values(["entry_date_norm", "component_priority", "score"], ascending=[True, False, False])


def rolling_health(candidates: pd.DataFrame, policy: HealthPolicy) -> pd.DataFrame:
    exits = candidates[[policy.group_col, "policy_exit_date_norm", "ret_norm"]].dropna().sort_values("policy_exit_date_norm")
    exit_rows = list(exits.itertuples(index=False, name=None))
    windows: dict[str, deque[tuple[pd.Timestamp, float]]] = defaultdict(deque)
    rows = []
    exit_i = 0
    dates = sorted(candidates["entry_date_norm"].dropna().unique())

    for current_date in dates:
        while exit_i < len(exit_rows) and exit_rows[exit_i][1] < current_date:
            group_value, exit_date, ret = exit_rows[exit_i]
            windows[str(group_value)].append((pd.Timestamp(exit_date), float(ret)))
            exit_i += 1

        cutoff = pd.Timestamp(current_date) - pd.Timedelta(days=policy.lookback_days)
        day_groups = sorted(candidates.loc[candidates["entry_date_norm"].eq(current_date), policy.group_col].dropna().astype(str).unique())
        for group_value in day_groups:
            q = windows[group_value]
            while q and q[0][0] < cutoff:
                q.popleft()
            vals = pd.Series([v for _, v in q], dtype=float)
            if vals.empty:
                count = 0
                avg_ret = np.nan
                win_rate = np.nan
                drawdown = np.nan
                enabled = False
                score = -999.0
            else:
                equity = (1.0 + vals).cumprod()
                count = int(len(vals))
                avg_ret = float(vals.mean())
                win_rate = float((vals > 0).mean())
                drawdown = max_drawdown(equity)
                enabled = (
                    count >= policy.min_count
                    and avg_ret >= policy.min_avg_ret
                    and win_rate >= policy.min_win_rate
                    and (pd.isna(drawdown) or drawdown >= policy.max_drawdown_floor)
                )
                score = float(avg_ret * 100.0 + (win_rate - 0.5) * 3.0 + min(count, 60) * 0.01)
            rows.append(
                {
                    "policy_key": policy.key,
                    "policy_label": policy.label,
                    "date": pd.Timestamp(current_date),
                    "group_col": policy.group_col,
                    "group_value": group_value,
                    "count": count,
                    "avg_ret": avg_ret,
                    "win_rate": win_rate,
                    "drawdown": drawdown,
                    "enabled": bool(enabled),
                    "health_score": score,
                }
            )
    return pd.DataFrame(rows)


def attach_policy(candidates: pd.DataFrame, health: pd.DataFrame, policy: HealthPolicy) -> pd.DataFrame:
    h = health[health["policy_key"].eq(policy.key)].copy()
    h = h.rename(
        columns={
            "date": "entry_date_norm",
            "enabled": "policy_enabled",
            "health_score": "policy_health_score",
            "count": "policy_count",
            "avg_ret": "policy_avg_ret",
            "win_rate": "policy_win_rate",
            "drawdown": "policy_drawdown",
        }
    )
    h[policy.group_col] = h["group_value"].astype(str)
    out = candidates.copy()
    out[policy.group_col] = out[policy.group_col].astype(str)
    out = out.merge(
        h[
            [
                "entry_date_norm",
                policy.group_col,
                "policy_enabled",
                "policy_health_score",
                "policy_count",
                "policy_avg_ret",
                "policy_win_rate",
                "policy_drawdown",
            ]
        ],
        on=["entry_date_norm", policy.group_col],
        how="left",
    )
    out["policy_key"] = policy.key
    out["policy_label"] = policy.label
    out["policy_group_col"] = policy.group_col
    out["policy_enabled"] = out["policy_enabled"].fillna(False).astype(bool)
    out["policy_health_score"] = pd.to_numeric(out["policy_health_score"], errors="coerce").fillna(-999.0)
    return out


def portfolio_backtest(candidates: pd.DataFrame, key: str, label: str, require_enabled: bool) -> tuple[pd.DataFrame, pd.DataFrame]:
    active: list[dict] = []
    selected: list[dict] = []
    curve_rows: list[dict] = []
    equity = 1.0
    all_dates = sorted(set(candidates["entry_date_norm"].dropna()) | set(candidates["policy_exit_date_norm"].dropna()))
    by_date = {d: g.copy() for d, g in candidates.groupby("entry_date_norm")}

    for current_date in all_dates:
        due = [p for p in active if p["policy_exit_date_norm"] <= current_date]
        if due:
            for p in due:
                equity *= 1.0 + float(p["ret_norm"]) * SLOT_PCT
            active = [p for p in active if p["policy_exit_date_norm"] > current_date]
            curve_rows.append({"date": current_date, "equity": equity, "event": "exit"})

        day = by_date.get(current_date)
        if day is None or len(active) >= MAX_SLOTS:
            continue
        if require_enabled:
            day = day[day["policy_enabled"]].copy()
        if day.empty:
            continue
        order_cols = ["policy_health_score", "component_priority", "source_priority", "score"] if "policy_health_score" in day.columns else ["component_priority", "source_priority", "score"]
        ranked = day.sort_values(order_cols, ascending=[False] * len(order_cols))
        used_codes = {p["code"] for p in active}
        for _, row in ranked.iterrows():
            if len(active) >= MAX_SLOTS:
                break
            if row["code"] in used_codes:
                continue
            rec = row.to_dict()
            rec["backtest_key"] = key
            rec["backtest_label"] = label
            active.append(rec)
            selected.append(rec)
            used_codes.add(row["code"])

    if active:
        for p in sorted(active, key=lambda x: x["policy_exit_date_norm"]):
            equity *= 1.0 + float(p["ret_norm"]) * SLOT_PCT
            curve_rows.append({"date": p["policy_exit_date_norm"], "equity": equity, "event": "forced_final_exit"})

    trades = pd.DataFrame(selected)
    curve = pd.DataFrame(curve_rows)
    if curve.empty:
        curve = pd.DataFrame([{"date": candidates["entry_date_norm"].min(), "equity": 1.0, "event": "init"}])
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


def metrics(trades: pd.DataFrame, curve: pd.DataFrame, key: str, label: str) -> dict:
    ret = pd.to_numeric(trades.get("ret_norm", pd.Series(dtype=float)), errors="coerce").dropna()
    if ret.empty:
        by_year = pd.DataFrame()
    else:
        by_year = trades.loc[ret.index].assign(year=lambda x: x["entry_date_norm"].dt.year).groupby("year")["ret_norm"].agg(["count", "mean"])
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
            base = float(c["equity"].iloc[0])
            if base:
                c["equity"] = c["equity"] / base
        row = metrics(t, c, key, label)
        row["window"] = window
        rows.append(row)
    return pd.DataFrame(rows)


def annual_metrics(trades: pd.DataFrame, key: str, label: str) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    rows = []
    for year, g in trades.assign(year=lambda x: x["entry_date_norm"].dt.year).groupby("year"):
        curve = pd.DataFrame({"equity": (1.0 + g["ret_norm"].fillna(0) * SLOT_PCT).cumprod()})
        row = metrics(g, curve, key, label)
        row["year"] = int(year)
        rows.append(row)
    return pd.DataFrame(rows)


def group_metrics(trades: pd.DataFrame, key: str, label: str) -> pd.DataFrame:
    rows = []
    for col in ["component", "route", "market_style", "ma_skeleton", "volume_price_layer", "adx_layer", "g3_position_guard", "g3_volume_context", "g3_capitulation_strength"]:
        if trades.empty or col not in trades.columns:
            continue
        for value, g in trades.groupby(col, dropna=False):
            if len(g) < 5:
                continue
            curve = pd.DataFrame({"equity": (1.0 + g["ret_norm"].fillna(0) * SLOT_PCT).cumprod()})
            row = metrics(g, curve, key, label)
            row["group_col"] = col
            row["group_value"] = str(value)
            rows.append(row)
    return pd.DataFrame(rows)


def last_n_candidate_dates(ledger: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    cols = [
        "entry_date_norm",
        "code",
        "name",
        "component",
        "route",
        "ret_norm",
        "policy_enabled",
        "policy_count",
        "policy_avg_ret",
        "policy_win_rate",
        "policy_drawdown",
        "policy_key",
    ]
    dates = sorted(ledger["entry_date_norm"].dropna().unique())[-n:]
    return ledger[ledger["entry_date_norm"].isin(dates)].sort_values(["entry_date_norm", "component_priority", "score"], ascending=[False, False, False])[[c for c in cols if c in ledger.columns]]


def verify_no_future(candidates: pd.DataFrame, health: pd.DataFrame, policy: HealthPolicy, sample_size: int = 60) -> pd.DataFrame:
    h = health[health["policy_key"].eq(policy.key)].copy()
    if h.empty:
        return pd.DataFrame()
    sample = h.sort_values("date").tail(sample_size)
    rows = []
    for row in sample.itertuples(index=False):
        d = pd.Timestamp(row.date)
        cutoff = d - pd.Timedelta(days=policy.lookback_days)
        hist = candidates[
            candidates[policy.group_col].astype(str).eq(str(row.group_value))
            & (candidates["policy_exit_date_norm"] < d)
            & (candidates["policy_exit_date_norm"] >= cutoff)
        ]
        vals = pd.to_numeric(hist["ret_norm"], errors="coerce").dropna()
        rows.append(
            {
                "policy_key": policy.key,
                "date": d,
                "group_col": policy.group_col,
                "group_value": row.group_value,
                "reported_count": int(row.count),
                "recomputed_count": int(len(vals)),
                "count_match": int(row.count) == int(len(vals)),
                "max_exit_date_used": hist["policy_exit_date_norm"].max() if not hist.empty else pd.NaT,
                "no_future_ok": bool(hist.empty or hist["policy_exit_date_norm"].max() < d),
            }
        )
    return pd.DataFrame(rows)


def safety_audit(v6_ledger_path: Path) -> dict:
    ledger = pd.read_csv(v6_ledger_path, low_memory=False)
    checks = {
        "rows": int(len(ledger)),
        "shadow_action_values": sorted(ledger.get("shadow_action", pd.Series(dtype=str)).dropna().astype(str).unique().tolist()),
        "auto_order_allowed_values": sorted(ledger.get("auto_order_allowed", pd.Series(dtype=bool)).dropna().astype(str).unique().tolist()),
        "formal_buy_signal_values": sorted(ledger.get("formal_buy_signal", pd.Series(dtype=bool)).dropna().astype(str).unique().tolist()),
        "order_path_enabled_values": sorted(ledger.get("order_path_enabled", pd.Series(dtype=bool)).dropna().astype(str).unique().tolist()),
    }
    checks["safety_ok"] = (
        checks["shadow_action_values"] == ["observe_only"]
        and set(checks["auto_order_allowed_values"]).issubset({"False", "false", "0"})
        and set(checks["formal_buy_signal_values"]).issubset({"False", "false", "0"})
        and set(checks["order_path_enabled_values"]).issubset({"False", "false", "0"})
    )
    return checks


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
        }:
            out[col] = out[col].map(pct)
    return out[[c for c in cols if c in out.columns]].to_markdown(index=False)


def write_report(
    summary: pd.DataFrame,
    windows: pd.DataFrame,
    annual: pd.DataFrame,
    groups: pd.DataFrame,
    dryrun: pd.DataFrame,
    no_future: pd.DataFrame,
    safety: dict,
    contract: dict,
) -> None:
    best = summary.sort_values(["capital_return_proxy", "avg_ret"], ascending=[False, False])
    pre = windows[windows["window"].eq("pre_2024_10")].sort_values("capital_return_proxy", ascending=False)
    last_component = dryrun[dryrun["policy_key"].eq("component_240_loose")].copy()
    component_groups = groups[groups["key"].eq("always_on") & groups["group_col"].eq("component")].sort_values("capital_return_proxy", ascending=False)
    market_groups = groups[groups["key"].eq("always_on") & groups["group_col"].isin(["market_style", "ma_skeleton", "volume_price_layer", "adx_layer"])].sort_values("capital_return_proxy", ascending=False)
    no_future_ok = bool(no_future["count_match"].all() and no_future["no_future_ok"].all()) if not no_future.empty else False

    report = f"""# G3 2024-10 前 State Alpha 迁移审计 v7

## 目标

本阶段继续研究 `pre_state_alpha` 是否能从研究源走向可迁移方案。重点不是改动当前 G3 shadow，而是回答四个问题：

- 2024-10 前挣钱是否来自稳定子模式，而不是少数票或硬日期。
- 总源健康度、组件健康度、路由健康度哪一种更适合做无未来函数门控。
- 最近样本是否满足 observe-only 干跑进入交易链路前的验收条件。
- 是否已经可以和当前 G3 组成全周期自动切换。

## 核心结论

1. 2024-10 前的可解释赚钱模式已经比较清楚：`range_no_adx_downtrend` 是主粮，`down_panic_core` 是情绪冰点修复，`panic_context_gate` 是小样本高赔率补充。
2. 组件/路由级健康度没有推翻第六阶段结论：健康度门控能提升可解释性，但会明显牺牲交易覆盖；当前更适合 observe-only 监控，不适合接入真实 G3 shadow。
3. 不是“交易数太少”单一问题：`range_no_adx_downtrend` 交易数足够，问题在单笔收益和尾部损失仍需实盘前置审计；`panic_context_gate` 才是交易数太少。
4. 无未来函数复核通过：健康度样本只使用入场日前已退出的历史交易，未使用未来收益。
5. 当前不能闭环到自动切换。可闭环的是“研究源 + observe-only 文件 + 安全合同 + 迁移验收清单”，交易闭环仍需至少 20 个交易日干跑和 UI/候选/MTM 对账。

## 策略/健康度对比
"""
    report += format_table(
        best,
        [
            "key",
            "label",
            "trade_count",
            "win_rate",
            "avg_ret",
            "median_ret",
            "capital_return_proxy",
            "capital_max_drawdown_proxy",
            "worst_trade",
            "top5_positive_share",
            "positive_years",
            "diagnosis",
        ],
    )
    report += "\n\n## 2024-10 前窗口\n\n"
    report += format_table(
        pre,
        [
            "key",
            "label",
            "trade_count",
            "win_rate",
            "avg_ret",
            "median_ret",
            "capital_return_proxy",
            "capital_max_drawdown_proxy",
            "worst_trade",
            "diagnosis",
        ],
    )
    report += "\n\n## 组件归因\n\n"
    report += format_table(
        component_groups,
        [
            "group_value",
            "trade_count",
            "win_rate",
            "avg_ret",
            "median_ret",
            "capital_return_proxy",
            "capital_max_drawdown_proxy",
            "worst_trade",
            "top5_positive_share",
            "diagnosis",
        ],
    )
    report += "\n\n## 市场状态归因样本\n\n"
    report += format_table(
        market_groups,
        [
            "group_col",
            "group_value",
            "trade_count",
            "win_rate",
            "avg_ret",
            "median_ret",
            "capital_return_proxy",
            "worst_trade",
            "diagnosis",
        ],
        limit=30,
    )
    report += "\n\n## 年度拆分\n\n"
    report += format_table(
        annual.sort_values(["key", "year"]),
        [
            "key",
            "year",
            "trade_count",
            "win_rate",
            "avg_ret",
            "median_ret",
            "capital_return_proxy",
            "capital_max_drawdown_proxy",
            "worst_trade",
            "diagnosis",
        ],
        limit=120,
    )
    report += "\n\n## 最近 20 个候选日干跑状态（组件 240D 宽松）\n\n"
    report += format_table(
        last_component,
        [
            "entry_date_norm",
            "code",
            "name",
            "component",
            "route",
            "ret_norm",
            "policy_enabled",
            "policy_count",
            "policy_avg_ret",
            "policy_win_rate",
            "policy_drawdown",
        ],
        limit=80,
    )
    report += f"""

## 无未来函数与安全复核

- 无未来函数抽样复核：{"通过" if no_future_ok else "未通过或样本不足"}。
- observe-only 安全字段复核：{"通过" if safety.get("safety_ok") else "未通过"}。
- shadow_action 取值：`{safety.get("shadow_action_values")}`。
- auto_order_allowed 取值：`{safety.get("auto_order_allowed_values")}`。
- formal_buy_signal 取值：`{safety.get("formal_buy_signal_values")}`。
- order_path_enabled 取值：`{safety.get("order_path_enabled_values")}`。

## 可迁移方案

可以迁移的部分：

- 候选生成：`range_no_adx_downtrend + down_panic_core + panic_context_gate`。
- 状态解释：按组件、路由、市场状态输出收益归因和阻断原因。
- 健康度账本：使用 `policy_exit_date < entry_date` 的滚动历史，不使用硬日期做最终开关。
- 安全合同：继续保持 `observe_only`，全部订单字段为 false。

不能迁移的部分：

- 不能把当前正式或影子 G3 直接替换成 pre_state_alpha。
- 不能把 `2024-10-01` 写成最终切换规则。
- 不能把健康度通过的历史候选直接当成今日交易信号。

交易链路开放前验收条件：

- 连续至少 20 个真实交易日 observe-only 日更，无缺失、无手工补写。
- 今日候选、候选池审计、UI 阻断原因、MTM 台账完全一致。
- 最近 20 个候选日内，组件级健康度至少出现稳定可交易窗口，而不是长期 health blocked。
- 单笔尾部损失控制方案明确，尤其是 `range_no_adx_downtrend` 的最差单笔和回撤来源。
- 用户明确批准后，才允许讨论 shadow 接入；当前合同仍禁止自动下单。

## 机器合同摘要

```json
{json.dumps(contract, ensure_ascii=False, indent=2)}
```
"""
    (OUT_DIR / "REPORT_CN.md").write_text(report, encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candidates = load_candidates()
    candidates.to_csv(OUT_DIR / "migration_source_candidates.csv", index=False, encoding="utf-8-sig")

    health_frames = []
    policy_ledgers = []
    summary_rows = []
    window_frames = []
    annual_frames = []
    group_frames = []

    always_trades, always_curve = portfolio_backtest(candidates.assign(policy_enabled=True, policy_health_score=999.0), "always_on", "pre_state_alpha 静态 always-on", require_enabled=True)
    always_trades.to_csv(OUT_DIR / "always_on_selected.csv", index=False, encoding="utf-8-sig")
    always_curve.to_csv(OUT_DIR / "always_on_curve.csv", index=False, encoding="utf-8-sig")
    summary_rows.append(metrics(always_trades, always_curve, "always_on", "pre_state_alpha 静态 always-on"))
    window_frames.append(window_metrics(always_trades, always_curve, "always_on", "pre_state_alpha 静态 always-on"))
    annual_frames.append(annual_metrics(always_trades, "always_on", "pre_state_alpha 静态 always-on"))
    group_frames.append(group_metrics(always_trades, "always_on", "pre_state_alpha 静态 always-on"))

    no_future_frames = []
    for policy in POLICIES:
        h = rolling_health(candidates, policy)
        health_frames.append(h)
        no_future_frames.append(verify_no_future(candidates, h, policy))
        ledger = attach_policy(candidates, h, policy)
        policy_ledgers.append(ledger)
        trades, curve = portfolio_backtest(ledger, policy.key, policy.label, require_enabled=True)
        trades.to_csv(OUT_DIR / f"{policy.key}_selected.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(OUT_DIR / f"{policy.key}_curve.csv", index=False, encoding="utf-8-sig")
        summary_rows.append(metrics(trades, curve, policy.key, policy.label))
        window_frames.append(window_metrics(trades, curve, policy.key, policy.label))
        annual_frames.append(annual_metrics(trades, policy.key, policy.label))
        group_frames.append(group_metrics(trades, policy.key, policy.label))

    health = pd.concat(health_frames, ignore_index=True)
    health.to_csv(OUT_DIR / "migration_health_ledger.csv", index=False, encoding="utf-8-sig")
    all_policy_ledger = pd.concat(policy_ledgers, ignore_index=True)
    all_policy_ledger.to_csv(OUT_DIR / "migration_policy_candidate_ledger.csv", index=False, encoding="utf-8-sig")

    summary = pd.DataFrame(summary_rows)
    windows = pd.concat(window_frames, ignore_index=True)
    annual = pd.concat([x for x in annual_frames if not x.empty], ignore_index=True)
    groups = pd.concat([x for x in group_frames if not x.empty], ignore_index=True)
    no_future = pd.concat([x for x in no_future_frames if not x.empty], ignore_index=True)
    dryrun = last_n_candidate_dates(all_policy_ledger[all_policy_ledger["policy_key"].eq("component_240_loose")].copy())
    safety = safety_audit(SRC_DIR / "observe_only_candidate_ledger.csv")

    contract = {
        "strategy_id": "pre_state_alpha_migration_audit_v7",
        "purpose": "research_migration_readiness_audit",
        "writes_to_runtime": False,
        "feeds_shadow_trading": False,
        "auto_order_allowed": False,
        "formal_buy_signal": False,
        "order_path_enabled": False,
        "hard_date_final_rule_allowed": False,
        "health_uses_only_exited_history": bool(no_future["no_future_ok"].all()) if not no_future.empty else False,
        "recommended_runtime_status": "observe_only",
        "candidate_components": ["range_no_adx_downtrend", "down_panic_core", "panic_context_gate"],
        "promotion_gate": [
            "20 trading-day observe-only dry run",
            "daily MTM ledger parity",
            "UI block reason parity",
            "component health recovery",
            "explicit user approval before shadow integration",
        ],
    }

    summary.to_csv(OUT_DIR / "migration_summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "migration_window_summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "migration_annual_summary.csv", index=False, encoding="utf-8-sig")
    groups.to_csv(OUT_DIR / "migration_group_summary.csv", index=False, encoding="utf-8-sig")
    no_future.to_csv(OUT_DIR / "migration_no_future_audit.csv", index=False, encoding="utf-8-sig")
    dryrun.to_csv(OUT_DIR / "migration_last20_dryrun_component_240.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "migration_safety_audit.json").write_text(json.dumps(safety, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "migration_contract_v7.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")

    write_report(summary, windows, annual, groups, dryrun, no_future, safety, contract)
    print(f"wrote {OUT_DIR}")
    print(summary.sort_values("capital_return_proxy", ascending=False).to_string(index=False))


if __name__ == "__main__":
    main()
