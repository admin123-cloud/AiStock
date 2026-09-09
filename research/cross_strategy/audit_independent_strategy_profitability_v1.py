from __future__ import annotations

"""Audit independent mainwave and breakout research without trade-count overfitting."""

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path


OUT_DIR = report_path("independent_strategy_profitability_audit_v1")
MAINWAVE = report_path("score120_mom60_gate_revalidation_v1", "policy_summary.csv")
BREAKOUT_ASSOC = report_path("breakout_entry_30m_acceptance_v1", "payoff_by_30m_state.csv")
BREAKOUT_EXEC = report_path("breakout_1030_execution_proxy_v1", "payoff_summary.csv")
BREAKOUT_WINDOWS = ["train_2020_2023", "validation_2024_2025", "blind_2026", "full"]
MAINWAVE_WINDOWS = ["train_2020_2023", "valid_2024_2025", "blind_2026ytd", "full"]
MIN_RETENTION = 0.65
MIN_BLIND_TRADES = 10


def _read(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"缺少研究输入：{path}")
    return pd.read_csv(path, encoding="utf-8-sig")


def _mainwave() -> tuple[pd.DataFrame, dict]:
    raw = _read(MAINWAVE)
    baseline = raw[raw["policy"].eq("base_no_mom60_gate")].set_index("window")
    rows = []
    for policy, group in raw.groupby("policy"):
        for _, item in group[group["window"].isin(MAINWAVE_WINDOWS)].iterrows():
            window = str(item["window"])
            base = baseline.loc[window]
            closed = int(item["closed"])
            base_closed = int(base["closed"])
            retention = closed / base_closed if base_closed else None
            rows.append({
                "track": "institutional_mainwave",
                "variant": policy,
                "window": window,
                "trades": closed,
                "baseline_trades": base_closed,
                "trade_retention": retention,
                "mean_trade_ret": float(item["mean_trade_ret"]),
                "baseline_mean_trade_ret": float(base["mean_trade_ret"]),
                "excess_ret": float(item["excess_ret"]),
                "baseline_excess_ret": float(base["excess_ret"]),
                "max_drawdown": float(item["max_drawdown"]),
                "baseline_max_drawdown": float(base["max_drawdown"]),
            })
    result = pd.DataFrame(rows)
    assessments = []
    for policy, group in result.groupby("variant"):
        pivot = group.set_index("window")
        coverage_ok = all(
            pd.notna(pivot.loc[w, "trade_retention"]) and pivot.loc[w, "trade_retention"] >= MIN_RETENTION
            for w in ["full", "valid_2024_2025", "blind_2026ytd"]
        )
        blind_trades = int(pivot.loc["blind_2026ytd", "trades"])
        positive_increment = all(
            pivot.loc[w, "mean_trade_ret"] > pivot.loc[w, "baseline_mean_trade_ret"]
            and pivot.loc[w, "excess_ret"] > pivot.loc[w, "baseline_excess_ret"]
            for w in ["valid_2024_2025", "blind_2026ytd"]
        )
        status = "eligible_for_next_research_stage"
        if blind_trades < MIN_BLIND_TRADES:
            status = "reject_insufficient_blind_sample"
        elif not coverage_ok:
            status = "reject_trade_count_overfit"
        elif not positive_increment:
            status = "reject_no_out_of_sample_increment"
        assessments.append({"track": "institutional_mainwave", "variant": policy, "status": status, "blind_trades": blind_trades, "coverage_ok": coverage_ok, "positive_increment": positive_increment})
    return result, {"assessments": assessments, "source": str(MAINWAVE)}


def _breakout() -> tuple[pd.DataFrame, dict]:
    assoc = _read(BREAKOUT_ASSOC)
    execution = _read(BREAKOUT_EXEC)
    rows = []
    for window in BREAKOUT_WINDOWS:
        base = assoc[(assoc["window"].eq(window)) & (assoc["state"].eq("all"))].iloc[0]
        accepted = assoc[(assoc["window"].eq(window)) & (assoc["state"].eq("m30_native_accept"))].iloc[0]
        executable = execution[execution["window"].eq(window)].iloc[0]
        rows.append({
            "track": "breakout_initiation",
            "variant": "native_30m_acceptance",
            "window": window,
            "trades": int(executable["trades"]),
            "baseline_trades": int(base["trades"]),
            "trade_retention": int(executable["trades"]) / int(base["trades"]),
            "association_expectation": float(accepted["expectation"]),
            "baseline_association_expectation": float(base["expectation"]),
            "executable_expectation": float(executable["expectation"]),
            "executable_payoff_ratio": float(executable["payoff_ratio"]),
            "executable_worst_trade": float(executable["worst_trade"]),
        })
    result = pd.DataFrame(rows)
    pivot = result.set_index("window")
    coverage_ok = all(pivot.loc[w, "trade_retention"] >= MIN_RETENTION for w in ["full", "validation_2024_2025", "blind_2026"])
    blind_trades = int(pivot.loc["blind_2026", "trades"])
    positive_increment = all(
        pivot.loc[w, "association_expectation"] > pivot.loc[w, "baseline_association_expectation"]
        and pivot.loc[w, "executable_expectation"] > 0
        for w in ["validation_2024_2025", "blind_2026"]
    )
    status = "eligible_for_next_research_stage"
    if blind_trades < MIN_BLIND_TRADES:
        status = "reject_insufficient_blind_sample"
    elif not coverage_ok:
        status = "reject_trade_count_overfit"
    elif not positive_increment:
        status = "reject_no_out_of_sample_increment"
    return result, {"assessments": [{"track": "breakout_initiation", "variant": "native_30m_acceptance", "status": status, "blind_trades": blind_trades, "coverage_ok": coverage_ok, "positive_increment": positive_increment}], "sources": [str(BREAKOUT_ASSOC), str(BREAKOUT_EXEC)]}


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mainwave, mainwave_meta = _mainwave()
    breakout, breakout_meta = _breakout()
    combined = pd.concat([mainwave, breakout], ignore_index=True, sort=False)
    combined.to_csv(OUT_DIR / "independent_track_window_metrics.csv", index=False, encoding="utf-8-sig")
    assessments = mainwave_meta["assessments"] + breakout_meta["assessments"]
    result = {
        "status": "completed",
        "research_only": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "anti_overfit_contract": {"min_retention": MIN_RETENTION, "min_blind_trades": MIN_BLIND_TRADES},
        "mainwave_source": mainwave_meta["source"],
        "breakout_sources": breakout_meta["sources"],
        "assessments": assessments,
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    report = [
        "# 两条策略独立盈利能力与反过拟合审计 v1", "",
        "- 仅研究；不改变 G3 路由、影子账本或下单链路。",
        f"- 反过拟合门槛：全周期、验证、盲测的交易保留率均不得低于 {MIN_RETENTION:.0%}；盲测少于 {MIN_BLIND_TRADES} 笔不能升级。", "",
        "## 窗口指标", "", combined.to_markdown(index=False), "",
        "## 判定", "",
    ]
    for item in assessments:
        report.append(f"- {item['track']} / {item['variant']}：`{item['status']}`；盲测交易 {item['blind_trades']} 笔；保留率合格={item['coverage_ok']}；样本外增量合格={item['positive_increment']}。")
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(report) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
