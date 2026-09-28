from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path


OUT_DIR = report_path("final_strategy_candidate_validation_v1")
MAINWAVE_TRADES = report_path("gen3_score120_formal_institutional_source_v1", "closed_trades.csv")
G2_RUNS = report_path("gen2_v2_complete_strategy", "runs", "official")
BREAKOUT_STOP = report_path("breakout_1030_stop_sensitivity_v1", "stop_sensitivity_summary.csv")
BREAKOUT_ACCEPTANCE = report_path("breakout_entry_30m_acceptance_v1", "summary.json")


def _ratio(value: float | None) -> str:
    return "--" if value is None or pd.isna(value) else f"{value:.2%}"


def _payoff(ret: pd.Series) -> float | None:
    wins = ret[ret > 0]
    losses = ret[ret < 0]
    if wins.empty or losses.empty:
        return None
    return float(wins.mean() / abs(losses.mean()))


def _trade_metrics(frame: pd.DataFrame, ret_col: str) -> dict[str, Any]:
    ret = pd.to_numeric(frame.get(ret_col), errors="coerce").dropna()
    return {
        "trades": int(len(ret)),
        "win_rate": float((ret > 0).mean()) if len(ret) else None,
        "avg_trade_return": float(ret.mean()) if len(ret) else None,
        "payoff_ratio": _payoff(ret),
        "worst_trade": float(ret.min()) if len(ret) else None,
    }


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _mainwave() -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    trades = pd.read_csv(MAINWAVE_TRADES)
    trades["entry_date"] = pd.to_datetime(trades["entry_date"]).dt.normalize()
    trades["policy_exit_date"] = pd.to_datetime(trades["policy_exit_date"]).dt.normalize()
    windows = [
        ("历史早期 2020-2024", trades[trades["entry_date"] < pd.Timestamp("2025-01-01")]),
        ("验证 2025", trades[(trades["entry_date"] >= pd.Timestamp("2025-01-01")) & (trades["entry_date"] < pd.Timestamp("2026-01-01"))]),
        ("盲测 2026H1", trades[trades["entry_date"] >= pd.Timestamp("2026-01-01")]),
        ("全样本", trades),
    ]
    rows = []
    for window, frame in windows:
        row = {"strategy": "Score120机构主浪", "window": window, **_trade_metrics(frame, "net_ret")}
        rows.append(row)
    return trades, rows


def _g2() -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    trades: list[pd.DataFrame] = []
    for name, label in [("valid", "验证 2025"), ("blind_2026ytd", "盲测 2026H1")]:
        summary = _read_json(G2_RUNS / name / "summary.json")
        trade = pd.read_csv(G2_RUNS / name / "trades.csv")
        trade["buy_date"] = pd.to_datetime(trade["buy_date"]).dt.normalize()
        trade["sell_date"] = pd.to_datetime(trade["sell_date"]).dt.normalize()
        trade["validation_window"] = label
        trades.append(trade)
        rows.append(
            {
                "strategy": "G2 V2（stop_cd3_skip）",
                "window": label,
                "trades": int(summary["trade_count"]),
                "portfolio_return": float(summary["total_return"]),
                "max_drawdown": float(summary["max_drawdown"]),
                "excess_return": float(summary["excess_return"]),
                "win_rate": float(summary["win_rate"]),
                "avg_trade_return": float(summary["avg_trade_return"]),
                "payoff_ratio": _payoff(pd.to_numeric(trade["return"], errors="coerce")),
            }
        )
    return pd.concat(trades, ignore_index=True), rows


def _priority_overlap(mainwave: pd.DataFrame, g2: pd.DataFrame) -> dict[str, Any]:
    audit = g2.copy()
    active_counts = []
    duplicate_codes = []
    for row in audit.itertuples(index=False):
        active = mainwave[(mainwave["entry_date"] <= row.buy_date) & (mainwave["policy_exit_date"] > row.buy_date)]
        active_counts.append(int(len(active)))
        duplicate_codes.append(bool((active["code"].astype(str) == str(row.code)).any()))
    audit["active_mainwave_slots_on_buy"] = active_counts
    audit["duplicate_active_mainwave_code"] = duplicate_codes
    audit["priority_router_eligible"] = (audit["active_mainwave_slots_on_buy"] < 2) & ~audit["duplicate_active_mainwave_code"]
    eligible = audit[audit["priority_router_eligible"]].copy()
    metrics = _trade_metrics(eligible, "return")
    summary = {
        "g2_trade_rows": int(len(audit)),
        "eligible_trade_rows": int(len(eligible)),
        "eligible_rate": float(len(eligible) / len(audit)) if len(audit) else 0.0,
        "eligible_trade_metrics": metrics,
        "limitation": "这是主浪优先下的逐笔容量/重复标的审计；G2 原策略的冷却状态未按融合后的成交序列重算，不能视为组合权益回测。",
    }
    audit.to_csv(OUT_DIR / "g2_priority_router_overlap_audit.csv", index=False, encoding="utf-8-sig")
    return summary


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mainwave, mainwave_rows = _mainwave()
    g2, g2_rows = _g2()
    overlap = _priority_overlap(mainwave, g2)
    breakout = _read_json(BREAKOUT_ACCEPTANCE)
    stop = pd.read_csv(BREAKOUT_STOP)
    stop_full = stop[(stop["scope"] == "full") & (stop["stop_pct"] == 0.04)].iloc[0].to_dict()

    candidates = pd.DataFrame(mainwave_rows + g2_rows)
    candidates.to_csv(OUT_DIR / "candidate_window_metrics.csv", index=False, encoding="utf-8-sig")
    verdict = {
        "status": "completed",
        "accepted_now": ["G2 V2（stop_cd3_skip）：通过独立valid/blind组合回测；仅限研究有效，当前运行源不新鲜，不能直接启用自动交易"],
        "conditional_candidate": ["Score120机构主浪：历史成交复盘质量良好，但2026H1盲测仅8笔，未达最少10笔门槛，且当前合同未暴露动态冷却"],
        "rejected_now": ["10:30独立突破：当前仍为次日开盘价结果关联审计，非10:30可成交回测"],
        "mainwave_source": str(MAINWAVE_TRADES),
        "g2_source": str(G2_RUNS),
        "mainwave_windows": mainwave_rows,
        "g2_windows": g2_rows,
        "g2_priority_overlap": overlap,
        "breakout_evidence": {
            "live_eligible": bool(breakout.get("live_eligible")),
            "limitation": breakout.get("limitation"),
            "full_4pct_stop_proxy": stop_full,
        },
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 最终策略候选验证 v1",
        "",
        "## 结论",
        "",
        "- 已验证有效（研究口径）：G2 V2（stop_cd3_skip）。它在 2025 验证和 2026H1 盲测均为正超额，但当前运行源不新鲜，且尚未按主浪优先后的新成交序列重算冷却，不能直接启用自动交易。",
        "- 条件保留：Score120机构主浪。历史成交复盘质量良好，但 2026H1 盲测仅 8 笔，未达到最少 10 笔盲测门槛；当前合同也未暴露动态冷却。",
        "- 暂时淘汰：10:30独立突破。它的收益使用的是次日开盘入场，当前并非可成交的 10:30 回测。",
        "",
        "## 分窗口指标",
        "",
        candidates.to_markdown(index=False, floatfmt=".4f"),
        "",
        "## 主浪优先后的 G2 容量审计",
        "",
        f"- G2 已成交行：{overlap['g2_trade_rows']}；主浪未满两槽且无重复标的的可用行：{overlap['eligible_trade_rows']}（{_ratio(overlap['eligible_rate'])}）。",
        f"- 可用行胜率：{_ratio(overlap['eligible_trade_metrics']['win_rate'])}；单笔均值：{_ratio(overlap['eligible_trade_metrics']['avg_trade_return'])}；盈亏比：{overlap['eligible_trade_metrics']['payoff_ratio'] or '--'}。",
        f"- 限制：{overlap['limitation']}",
        "",
        "## 突破淘汰依据",
        "",
        f"- 可实盘资格：{breakout.get('live_eligible')}。",
        f"- 原因：{breakout.get('limitation')}。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(verdict, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
