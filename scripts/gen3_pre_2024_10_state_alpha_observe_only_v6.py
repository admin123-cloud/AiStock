from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_pre_2024_10_state_alpha_closure_v5 import (  # noqa: E402
    HealthVariant,
    diagnose,
    load_pre_state_alpha,
    max_drawdown,
    metrics_from_trades,
    rolling_source_history,
)
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("gen3_pre_2024_10_state_alpha_observe_only_v6")
SOURCE_KEY = "pre_state_alpha"

HEALTH_POLICIES = [
    HealthVariant("observe_240_loose", "observe-only 240D loose", 240, 6, 0.000, 0.50, -0.18),
    HealthVariant("observe_240_balanced", "observe-only 240D balanced", 240, 8, 0.003, 0.52, -0.15),
    HealthVariant("observe_480_balanced", "observe-only 480D balanced", 480, 12, 0.003, 0.52, -0.18),
]


def pct(v) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.2%}"


def health_snapshot(candidates: pd.DataFrame) -> pd.DataFrame:
    frames = []
    for policy in HEALTH_POLICIES:
        health = rolling_source_history(candidates, policy)
        rows = []
        for source_key, by_date in health.items():
            for date, stats in by_date.items():
                rows.append(
                    {
                        "health_policy": policy.key,
                        "health_label": policy.label,
                        "date": date,
                        "source_key": source_key,
                        **stats,
                    }
                )
        frames.append(pd.DataFrame(rows))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def build_observe_ledger(candidates: pd.DataFrame, health: pd.DataFrame) -> pd.DataFrame:
    if candidates.empty:
        return pd.DataFrame()

    health_wide = health.pivot_table(
        index=["date", "source_key"],
        columns="health_policy",
        values=["count", "avg_ret", "win_rate", "drawdown", "enabled", "health_score"],
        aggfunc="last",
    )
    health_wide.columns = [f"{metric}_{policy}" for metric, policy in health_wide.columns]
    health_wide = health_wide.reset_index().rename(columns={"date": "entry_date_norm"})

    ledger = candidates.merge(health_wide, on=["entry_date_norm", "source_key"], how="left")
    ledger = ledger.sort_values(["entry_date_norm", "component_priority", "score"], ascending=[True, False, False]).copy()
    ledger["daily_rank"] = ledger.groupby("entry_date_norm").cumcount() + 1

    enabled_cols = [c for c in ledger.columns if c.startswith("enabled_")]
    for col in enabled_cols:
        ledger[col] = ledger[col].fillna(False).astype(bool)
    ledger["health_any_enabled"] = ledger[enabled_cols].any(axis=1) if enabled_cols else False
    ledger["health_balanced_enabled"] = ledger.get("enabled_observe_240_balanced", False)
    ledger["research_ready"] = True
    ledger["live_ready"] = False
    ledger["shadow_action"] = "observe_only"
    ledger["auto_order_allowed"] = False
    ledger["formal_buy_signal"] = False
    ledger["order_path_enabled"] = False
    ledger["requires_manual_review"] = True
    ledger["block_reason"] = np.where(
        ledger["health_any_enabled"],
        "observe_only_not_orderable",
        "health_gate_not_ready",
    )
    ledger["candidate_status"] = np.where(
        ledger["health_any_enabled"],
        "observe_candidate",
        "blocked_by_health",
    )
    ledger["risk_contract_id"] = "pre_state_alpha_observe_only_v1"
    ledger["signal_chain"] = (
        "pre_state_alpha|"
        + ledger["component"].astype(str)
        + "|"
        + ledger["route"].astype(str)
        + "|observe_only"
    )
    return ledger


def latest_outputs(ledger: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    if ledger.empty:
        return ledger, {}
    latest_date = ledger["entry_date_norm"].max()
    latest = ledger[ledger["entry_date_norm"].eq(latest_date)].copy()
    latest = latest.sort_values(["health_any_enabled", "component_priority", "score"], ascending=[False, False, False])
    payload = {
        "as_of_date": str(pd.Timestamp(latest_date).date()),
        "strategy_id": "pre_state_alpha_observe_only_v1",
        "shadow_action": "observe_only",
        "auto_order_allowed": False,
        "formal_buy_signal": False,
        "order_path_enabled": False,
        "candidate_count": int(len(latest)),
        "enabled_candidate_count": int(latest["health_any_enabled"].sum()),
        "blocked_candidate_count": int((~latest["health_any_enabled"]).sum()),
        "candidates": [
            {
                "code": str(row.code),
                "name": str(getattr(row, "name", "")),
                "entry_date": str(pd.Timestamp(row.entry_date_norm).date()),
                "component": str(row.component),
                "route": str(row.route),
                "candidate_status": str(row.candidate_status),
                "block_reason": str(row.block_reason),
                "daily_rank": int(row.daily_rank),
                "auto_order_allowed": False,
                "formal_buy_signal": False,
                "order_path_enabled": False,
            }
            for row in latest.itertuples(index=False)
        ],
    }
    return latest, payload


def daily_summary(ledger: pd.DataFrame) -> pd.DataFrame:
    if ledger.empty:
        return pd.DataFrame()
    return (
        ledger.groupby("entry_date_norm")
        .agg(
            candidate_count=("code", "count"),
            enabled_count=("health_any_enabled", "sum"),
            blocked_count=("health_any_enabled", lambda s: int((~s).sum())),
            component_count=("component", "nunique"),
            top_component=("component", lambda s: s.iloc[0]),
            avg_score=("score", "mean"),
        )
        .reset_index()
        .sort_values("entry_date_norm")
    )


def component_attribution(ledger: pd.DataFrame) -> pd.DataFrame:
    if ledger.empty:
        return pd.DataFrame()
    rows = []
    for component, g in ledger.groupby("component", dropna=False):
        curve = pd.DataFrame({"equity": (1.0 + pd.to_numeric(g["ret_norm"], errors="coerce").fillna(0) * 0.25).cumprod()})
        row = metrics_from_trades(g, curve, str(component), str(component))
        row["component"] = str(component)
        rows.append(row)
    return pd.DataFrame(rows)


def write_contract() -> dict:
    contract = {
        "strategy_id": "pre_state_alpha_observe_only_v1",
        "purpose": "research_observe_only",
        "writes_to_runtime": False,
        "feeds_shadow_trading": False,
        "auto_order_allowed": False,
        "formal_buy_signal": False,
        "order_path_enabled": False,
        "requires_manual_review": True,
        "candidate_components": [
            "range_no_adx_downtrend",
            "down_panic_core",
            "panic_context_gate",
        ],
        "required_output_fields": [
            "entry_date_norm",
            "code",
            "name",
            "component",
            "route",
            "candidate_status",
            "block_reason",
            "shadow_action",
            "auto_order_allowed",
            "formal_buy_signal",
            "order_path_enabled",
            "risk_contract_id",
        ],
        "promotion_requirements": [
            "daily mark-to-market audit",
            "today candidate generation from live data",
            "health gate reproducibility",
            "block reason parity with UI",
            "minimum 20 trading-day observe-only dry run",
            "explicit user approval before touching shadow chain",
        ],
    }
    (OUT_DIR / "observe_only_contract.json").write_text(
        json.dumps(contract, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return contract


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
            "avg_ret_observe_240_loose",
            "win_rate_observe_240_loose",
            "drawdown_observe_240_loose",
        }:
            out[col] = out[col].map(pct)
    return out[[c for c in cols if c in out.columns]].to_markdown(index=False)


def write_report(
    ledger: pd.DataFrame,
    health: pd.DataFrame,
    latest: pd.DataFrame,
    day_summary: pd.DataFrame,
    attribution: pd.DataFrame,
    contract: dict,
) -> None:
    enabled = ledger[ledger["health_any_enabled"]].copy() if not ledger.empty else pd.DataFrame()
    blocked = ledger[~ledger["health_any_enabled"]].copy() if not ledger.empty else pd.DataFrame()
    latest_view = latest[
        [
            "entry_date_norm",
            "code",
            "name",
            "component",
            "route",
            "daily_rank",
            "candidate_status",
            "block_reason",
            "health_any_enabled",
            "auto_order_allowed",
            "formal_buy_signal",
        ]
    ].copy() if not latest.empty else pd.DataFrame()

    report = f"""# G3 pre_state_alpha Observe-Only 第六阶段 v6

## 目标

把第五阶段的 `pre_state_alpha` 研究源转换成影子前 observe-only 产物，覆盖每日候选、健康度、阻断原因、路由归因和风控合同。

本阶段明确不做：

- 不写入 runtime 正式交易目录。
- 不接入当前 G3 shadow。
- 不生成自动下单信号。
- 不改 `institutional_mainwave` 当前链路。

## 闭环状态

- 研究源闭环：完成。
- observe-only 文件闭环：完成。
- 交易闭环：未开放，仍需真实每日 MTM、今日实时候选、UI 阻断原因一致性和至少 20 个交易日干跑。

## 输出合同

| 字段 | 值 |
|:--|:--|
| strategy_id | {contract["strategy_id"]} |
| purpose | {contract["purpose"]} |
| writes_to_runtime | {contract["writes_to_runtime"]} |
| feeds_shadow_trading | {contract["feeds_shadow_trading"]} |
| auto_order_allowed | {contract["auto_order_allowed"]} |
| formal_buy_signal | {contract["formal_buy_signal"]} |
| order_path_enabled | {contract["order_path_enabled"]} |

## 全样本候选概览

| 指标 | 数值 |
|:--|--:|
| 候选总数 | {len(ledger)} |
| 健康度通过候选 | {int(ledger["health_any_enabled"].sum()) if not ledger.empty else 0} |
| 健康度阻断候选 | {int((~ledger["health_any_enabled"]).sum()) if not ledger.empty else 0} |
| 最新候选日 | {str(pd.Timestamp(ledger["entry_date_norm"].max()).date()) if not ledger.empty else ""} |
| 最新日候选数 | {len(latest)} |

## 最新日 Observe-Only 候选

{format_table(latest_view, ["entry_date_norm", "code", "name", "component", "route", "daily_rank", "candidate_status", "block_reason", "health_any_enabled", "auto_order_allowed", "formal_buy_signal"], limit=50)}

## 组件归因

{format_table(attribution.sort_values("capital_return_proxy", ascending=False), ["component", "trade_count", "win_rate", "avg_ret", "median_ret", "capital_return_proxy", "capital_max_drawdown_proxy", "worst_trade", "top5_positive_share", "diagnosis"])}

## 健康度通过样本

{format_table(enabled.sort_values(["entry_date_norm", "daily_rank"]).tail(50), ["entry_date_norm", "code", "name", "component", "route", "daily_rank", "candidate_status", "block_reason", "enabled_observe_240_loose", "enabled_observe_240_balanced", "enabled_observe_480_balanced"], limit=50)}

## 健康度阻断样本

{format_table(blocked.sort_values(["entry_date_norm", "daily_rank"]).tail(50), ["entry_date_norm", "code", "name", "component", "route", "daily_rank", "candidate_status", "block_reason", "enabled_observe_240_loose", "enabled_observe_240_balanced", "enabled_observe_480_balanced"], limit=50)}

## 每日候选摘要

{format_table(day_summary.tail(80), ["entry_date_norm", "candidate_count", "enabled_count", "blocked_count", "component_count", "top_component", "avg_score"], limit=80)}

## 下一步迁移边界

1. 可以迁移为研究页面/工作台的数据源。
2. 可以增加每日定时 observe-only 产物，但目标目录仍应是 report/runtime 的研究子目录，且 `auto_order_allowed=false`。
3. 不应进入当前 G3 shadow 下单链路，除非完成 20 个交易日干跑、每日 MTM 和人工批准。
"""
    (OUT_DIR / "REPORT_CN.md").write_text(report, encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candidates = load_pre_state_alpha()
    health = health_snapshot(candidates)
    ledger = build_observe_ledger(candidates, health)
    latest, latest_payload = latest_outputs(ledger)
    day_summary = daily_summary(ledger)
    attribution = component_attribution(ledger)
    contract = write_contract()

    candidates.to_csv(OUT_DIR / "observe_only_source_candidates.csv", index=False, encoding="utf-8-sig")
    health.to_csv(OUT_DIR / "observe_only_health_ledger.csv", index=False, encoding="utf-8-sig")
    ledger.to_csv(OUT_DIR / "observe_only_candidate_ledger.csv", index=False, encoding="utf-8-sig")
    latest.to_csv(OUT_DIR / "latest_observe_only_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "latest_observe_only_candidates.json").write_text(
        json.dumps(latest_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    day_summary.to_csv(OUT_DIR / "observe_only_daily_summary.csv", index=False, encoding="utf-8-sig")
    attribution.to_csv(OUT_DIR / "observe_only_component_attribution.csv", index=False, encoding="utf-8-sig")
    write_report(ledger, health, latest, day_summary, attribution, contract)

    print(f"wrote {OUT_DIR}")
    print(
        pd.DataFrame(
            [
                {
                    "candidates": len(ledger),
                    "enabled": int(ledger["health_any_enabled"].sum()) if not ledger.empty else 0,
                    "blocked": int((~ledger["health_any_enabled"]).sum()) if not ledger.empty else 0,
                    "latest_date": str(pd.Timestamp(ledger["entry_date_norm"].max()).date()) if not ledger.empty else "",
                    "latest_candidates": len(latest),
                }
            ]
        ).to_string(index=False)
    )


if __name__ == "__main__":
    main()
