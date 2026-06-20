from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_pre_2024_10_state_alpha_tail_risk_v8 import metrics, portfolio_backtest, window_metrics  # noqa: E402
from utils.paths import report_path, reports_root  # noqa: E402


V7_DIR = reports_root() / "gen3_pre_2024_10_state_alpha_migration_audit_v7"
V8_DIR = reports_root() / "gen3_pre_2024_10_state_alpha_tail_risk_v8"
OUT_DIR = report_path("gen3_pre_2024_10_state_alpha_scorecap_observe_v9")
POLICY_KEY = "route_240_loose"
SCORE_CAP = 1.0


def pct(v) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.2%}"


def load_route_policy_candidates() -> pd.DataFrame:
    df = pd.read_csv(V7_DIR / "migration_policy_candidate_ledger.csv", low_memory=False)
    df = df[df["policy_key"].eq(POLICY_KEY)].copy()
    df["entry_date_norm"] = pd.to_datetime(df["entry_date_norm"], errors="coerce").dt.normalize()
    df["policy_exit_date_norm"] = pd.to_datetime(df["policy_exit_date_norm"], errors="coerce").dt.normalize()
    for col in ["ret_norm", "score", "policy_count", "policy_avg_ret", "policy_win_rate", "policy_drawdown", "policy_health_score", "component_priority", "source_priority"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["policy_enabled"] = df["policy_enabled"].fillna(False).astype(bool)
    return df.sort_values(["entry_date_norm", "component_priority", "score"], ascending=[True, False, False])


def build_ledger(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["score_cap"] = SCORE_CAP
    out["score_cap_pass"] = out["score"].fillna(np.inf) <= SCORE_CAP
    out["route_health_enabled"] = out["policy_enabled"].astype(bool)
    out["observe_gate_pass"] = out["route_health_enabled"] & out["score_cap_pass"]
    out["candidate_status"] = np.select(
        [
            ~out["route_health_enabled"],
            out["route_health_enabled"] & ~out["score_cap_pass"],
            out["observe_gate_pass"],
        ],
        [
            "blocked_by_route_health",
            "blocked_by_score_overheat",
            "observe_candidate",
        ],
        default="blocked_unknown",
    )
    out["block_reason"] = np.select(
        [
            ~out["route_health_enabled"],
            out["route_health_enabled"] & ~out["score_cap_pass"],
            out["observe_gate_pass"],
        ],
        [
            "route_health_not_ready",
            "score_overheat_gt_1_0",
            "observe_only_not_orderable",
        ],
        default="unknown",
    )
    out["shadow_action"] = "observe_only"
    out["auto_order_allowed"] = False
    out["formal_buy_signal"] = False
    out["order_path_enabled"] = False
    out["requires_manual_review"] = True
    out["risk_contract_id"] = "pre_state_alpha_scorecap_observe_v1"
    out["signal_chain"] = (
        "pre_state_alpha|route_240_loose|score_cap_1_0|"
        + out["component"].astype(str)
        + "|"
        + out["route"].astype(str)
        + "|observe_only"
    )
    out = out.sort_values(["entry_date_norm", "observe_gate_pass", "component_priority", "score"], ascending=[True, False, False, False])
    out["daily_rank"] = out.groupby("entry_date_norm").cumcount() + 1
    return out


def latest_payload(ledger: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    if ledger.empty:
        return pd.DataFrame(), {}
    latest_date = ledger["entry_date_norm"].max()
    latest = ledger[ledger["entry_date_norm"].eq(latest_date)].copy()
    latest = latest.sort_values(["observe_gate_pass", "component_priority", "score"], ascending=[False, False, False])
    payload = {
        "as_of_date": str(pd.Timestamp(latest_date).date()),
        "strategy_id": "pre_state_alpha_scorecap_observe_v1",
        "shadow_action": "observe_only",
        "auto_order_allowed": False,
        "formal_buy_signal": False,
        "order_path_enabled": False,
        "candidate_count": int(len(latest)),
        "observe_candidate_count": int(latest["observe_gate_pass"].sum()),
        "blocked_candidate_count": int((~latest["observe_gate_pass"]).sum()),
        "candidates": [
            {
                "code": str(row.code),
                "name": str(getattr(row, "name", "")),
                "entry_date": str(pd.Timestamp(row.entry_date_norm).date()),
                "component": str(row.component),
                "route": str(row.route),
                "score": float(row.score) if pd.notna(row.score) else None,
                "route_health_enabled": bool(row.route_health_enabled),
                "score_cap_pass": bool(row.score_cap_pass),
                "candidate_status": str(row.candidate_status),
                "block_reason": str(row.block_reason),
                "auto_order_allowed": False,
                "formal_buy_signal": False,
                "order_path_enabled": False,
            }
            for row in latest.itertuples(index=False)
        ],
    }
    return latest, payload


def status_summary(ledger: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for status, g in ledger.groupby("candidate_status", dropna=False):
        ret = pd.to_numeric(g["ret_norm"], errors="coerce").dropna()
        rows.append(
            {
                "candidate_status": status,
                "trade_count": int(len(ret)),
                "win_rate": float((ret > 0).mean()) if len(ret) else np.nan,
                "avg_ret": float(ret.mean()) if len(ret) else np.nan,
                "median_ret": float(ret.median()) if len(ret) else np.nan,
                "worst_trade": float(ret.min()) if len(ret) else np.nan,
                "bad5_rate": float((ret <= -0.05).mean()) if len(ret) else np.nan,
            }
        )
    return pd.DataFrame(rows).sort_values("trade_count", ascending=False)


def format_table(df: pd.DataFrame, cols: list[str], limit: int | None = None) -> str:
    if df.empty:
        return "_无数据_"
    out = df.copy()
    if limit:
        out = out.head(limit)
    for col in out.columns:
        if col.endswith("rate") or col in {"avg_ret", "median_ret", "worst_trade", "ret_norm", "policy_avg_ret", "policy_win_rate", "policy_drawdown", "capital_return_proxy", "capital_max_drawdown_proxy"}:
            out[col] = out[col].map(pct)
    return out[[c for c in cols if c in out.columns]].to_markdown(index=False)


def write_report(ledger: pd.DataFrame, latest: pd.DataFrame, status: pd.DataFrame, selected: pd.DataFrame, curve: pd.DataFrame, contract: dict) -> None:
    summary = pd.DataFrame([metrics(selected, curve, "scorecap_observe_pass", "route_240 + score<=1.0 observe pass")])
    windows = window_metrics(selected, curve, "scorecap_observe_pass", "route_240 + score<=1.0 observe pass")
    report = f"""# G3 pre_state_alpha score-cap observe-only v9

## 目标

把 v8 的研究结论 `route_240_loose + score<=1.0` 转换为只读候选账本，显式输出阻断原因。

本阶段仍然不做：

- 不写 runtime。
- 不接入当前 G3 shadow。
- 不自动下单。
- 不生成正式买点。

## 核心结论

1. observe-only 账本已生成，订单相关字段全部为 false。
2. 候选状态分为 `observe_candidate`、`blocked_by_route_health`、`blocked_by_score_overheat`。
3. 最新候选日仍未形成可交易开放条件；这里只能作为后续 20 个交易日干跑的候选合同。

## 通过样本回放指标

{format_table(summary, ["key", "trade_count", "win_rate", "avg_ret", "median_ret", "capital_return_proxy", "capital_max_drawdown_proxy", "worst_trade", "diagnosis"])}

## 分窗口指标

{format_table(windows, ["window", "trade_count", "win_rate", "avg_ret", "median_ret", "capital_return_proxy", "capital_max_drawdown_proxy", "worst_trade", "diagnosis"])}

## 阻断状态归因

{format_table(status, ["candidate_status", "trade_count", "win_rate", "avg_ret", "median_ret", "worst_trade", "bad5_rate"])}

## 最新候选日

{format_table(latest, ["entry_date_norm", "code", "name", "component", "route", "score", "route_health_enabled", "score_cap_pass", "candidate_status", "block_reason", "auto_order_allowed", "formal_buy_signal"], limit=50)}

## 机器合同

```json
{json.dumps(contract, ensure_ascii=False, indent=2)}
```
"""
    (OUT_DIR / "REPORT_CN.md").write_text(report, encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw = load_route_policy_candidates()
    ledger = build_ledger(raw)
    selected_pool = ledger[ledger["observe_gate_pass"]].copy()
    selected, curve = portfolio_backtest(selected_pool, "scorecap_observe_pass", "route_240 + score<=1.0 observe pass")
    latest, payload = latest_payload(ledger)
    status = status_summary(ledger)

    contract = {
        "strategy_id": "pre_state_alpha_scorecap_observe_v1",
        "purpose": "research_observe_only_with_explicit_block_reasons",
        "writes_to_runtime": False,
        "feeds_shadow_trading": False,
        "auto_order_allowed": False,
        "formal_buy_signal": False,
        "order_path_enabled": False,
        "rolling_health": POLICY_KEY,
        "score_cap": SCORE_CAP,
        "block_reasons": ["route_health_not_ready", "score_overheat_gt_1_0", "observe_only_not_orderable"],
        "promotion_requirements": [
            "live today candidate generation",
            "20 trading-day observe-only dry run",
            "MTM/UI/block reason parity",
            "explicit user approval before shadow integration",
        ],
    }

    raw.to_csv(OUT_DIR / "scorecap_observe_source_candidates.csv", index=False, encoding="utf-8-sig")
    ledger.to_csv(OUT_DIR / "scorecap_observe_candidate_ledger.csv", index=False, encoding="utf-8-sig")
    selected.to_csv(OUT_DIR / "scorecap_observe_selected_backtest.csv", index=False, encoding="utf-8-sig")
    curve.to_csv(OUT_DIR / "scorecap_observe_curve.csv", index=False, encoding="utf-8-sig")
    latest.to_csv(OUT_DIR / "latest_scorecap_observe_candidates.csv", index=False, encoding="utf-8-sig")
    status.to_csv(OUT_DIR / "scorecap_observe_status_summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "latest_scorecap_observe_candidates.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "scorecap_observe_contract.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(ledger, latest, status, selected, curve, contract)

    safety = {
        "rows": int(len(ledger)),
        "auto_order_allowed_values": sorted(ledger["auto_order_allowed"].astype(str).unique().tolist()),
        "formal_buy_signal_values": sorted(ledger["formal_buy_signal"].astype(str).unique().tolist()),
        "order_path_enabled_values": sorted(ledger["order_path_enabled"].astype(str).unique().tolist()),
        "observe_candidates": int(ledger["observe_gate_pass"].sum()),
        "blocked_candidates": int((~ledger["observe_gate_pass"]).sum()),
        "latest_date": str(pd.Timestamp(ledger["entry_date_norm"].max()).date()) if not ledger.empty else None,
    }
    (OUT_DIR / "scorecap_observe_safety_audit.json").write_text(json.dumps(safety, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT_DIR}")
    print(json.dumps(safety, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
