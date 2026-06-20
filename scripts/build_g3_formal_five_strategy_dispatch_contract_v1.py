from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api.gen3_state_alpha as g3  # noqa: E402
from scripts.backtest_g3_five_strategies_from_scratch_v1 import _md_table, _money, _pct  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("g3_formal_five_strategy_dispatch_contract_v1")
FORMAL_DIR = report_path("g2_g3_market_style_router_v1")
PRACTICAL_DIR = report_path("g3_practical_fusion_contract_v1")
MODEL = "g3_final_with_g2_gap_supplement"
INITIAL_EQUITY = 1_000_000.0


DISPATCH_STATES = [
    {
        "state": "panic_capitulation",
        "label": "恐慌出清",
        "definition": "前一交易日恐慌扩散或下跌压力释放：big_down_rate>=15%，或 limit_down_proxy_rate>=3%，或 standard_downtrend 且 up_rate<=35%。",
        "enabled_strategies": ["panic_capitulation_repair", "institutional_score120_mainwave"],
        "primary_strategy": "panic_capitulation_repair",
        "reason": "恐慌修复只在真正的恐慌/下跌出清状态启用；若同时出现高质量主升候选，可作为二槽共存但不放大仓位。",
    },
    {
        "state": "mainwave_or_strong_trend",
        "label": "主升/强趋势",
        "definition": "存在机构主升候选，或市场扩散不弱且出现强势突破候选；不处于 severe panic。",
        "enabled_strategies": ["institutional_score120_mainwave", "old_g3_strong_breakout", "volume_runup_supplement"],
        "primary_strategy": "institutional_score120_mainwave",
        "reason": "主升负责主要进攻；强势突破作为同方向进攻开关；G2 只在二槽未满时补位。",
    },
    {
        "state": "range_or_weak_repair",
        "label": "震荡/弱势修复",
        "definition": "market_style 为 standard_range 或 weak_rebound，或均线骨架 mixed/weak_repair，且未触发恐慌出清。",
        "enabled_strategies": ["range_weak_repair", "volume_runup_supplement", "old_g3_strong_breakout"],
        "primary_strategy": "range_weak_repair",
        "reason": "震荡弱势修复是常规修复核心；强势突破仅在出现质量合格突破候选时参与；G2 继续补空档。",
    },
    {
        "state": "no_g3_primary",
        "label": "G3主路由空档",
        "definition": "当天没有合格 G3 主路由候选，且 G2 补位源新鲜。",
        "enabled_strategies": ["volume_runup_supplement"],
        "primary_strategy": "volume_runup_supplement",
        "reason": "G2 只承担空档补位，不替代 G3 主路由。",
    },
]


STRATEGY_CONTRACT = [
    {
        "trade_strategy": "institutional_score120_mainwave",
        "label": "机构主升Score120",
        "role": "core_offense",
        "default_enabled": True,
        "activation": "存在 institutional_mainwave 原生候选，score>=120，主线扩散/30m确认满足；index_mom60<=5%正常，5%-10%降仓，>10%不开新仓。",
        "priority": 10,
        "default_slot_pct": 0.50,
        "max_slot_pct": 0.50,
        "backtest_role": "贡献最大，不能删除。",
    },
    {
        "trade_strategy": "range_weak_repair",
        "label": "震荡弱势修复",
        "role": "core_repair",
        "default_enabled": True,
        "activation": "非恐慌状态下，old_g3 range_gap 或 panic_repair_range 等原生修复候选满足结构修复/30m确认。",
        "priority": 30,
        "default_slot_pct": 0.50,
        "max_slot_pct": 0.50,
        "backtest_role": "第二大收益来源，是常规修复核心。",
    },
    {
        "trade_strategy": "old_g3_strong_breakout",
        "label": "强势突破",
        "role": "conditional_offense_switch",
        "default_enabled": True,
        "activation": "出现 old_g3 strong_main/强势突破原生候选，且市场不是下跌风险状态；作为强趋势/突破周期的进攻开关。",
        "priority": 20,
        "default_slot_pct": 0.50,
        "max_slot_pct": 0.50,
        "backtest_role": "交易少但质量高；不常态硬开，也不删除。",
    },
    {
        "trade_strategy": "panic_capitulation_repair",
        "label": "恐慌出清修复",
        "role": "conditional_defense_switch",
        "default_enabled": True,
        "activation": "触发 panic_capitulation 状态，且 panic_repair_downtrend 或 old_g3 down_panic 原生候选满足 30m 修复确认。",
        "priority": 5,
        "default_slot_pct": 0.25,
        "max_slot_pct": 0.50,
        "backtest_role": "极端状态修复策略；非恐慌状态不硬开。",
    },
    {
        "trade_strategy": "volume_runup_supplement",
        "label": "量能续强补位",
        "role": "gap_supplement",
        "default_enabled": True,
        "activation": "G3 主路由未占满二槽，且 g2_v2_complete 补位源覆盖 entry_date 并有 buy_allowed 候选。",
        "priority": 90,
        "default_slot_pct": 0.50,
        "max_slot_pct": 0.50,
        "backtest_role": "补足空档，提高资金利用率；不能替代主路由。",
    },
]


DISPATCH_RULES = {
    "version": "g3_formal_five_strategy_dispatch_contract_v1",
    "model": MODEL,
    "principle": "五个策略全部是正式策略；差别在于市场状态、候选质量、仓位和二槽调度，不再把强势突破/恐慌修复视为关闭策略。",
    "daily_open_limit": 2,
    "slot_pct_default": 0.50,
    "same_sector_policy": "只有两张都是机构主升Score120时允许同板块/同主线；否则重复板块跳过第二张。",
    "route_health_policy": "route_health 只观察，不进入默认硬 gate。",
    "hard_safety_gates": [
        "数据新鲜度覆盖 entry_date",
        "候选无未来函数",
        "30m确认语义满足对应原生策略",
        "真实账户风控与资金约束通过",
        "正式下单开关显式开启前只生成影子/纸面票据",
    ],
    "dispatch_sequence": [
        "读取前一交易日市场状态和当日各策略原生候选",
        "应用硬安全 gate 和策略自身原生准入条件",
        "根据市场状态选择启用策略集合",
        "按策略优先级、策略内主分数、候选排名排序",
        "执行二槽、重复代码和板块暴露约束",
        "G3 主路由不足二槽时使用 G2 量能续强补位",
        "生成买入票据、退出合同、仓位和审计字段",
    ],
    "states": DISPATCH_STATES,
    "strategies": STRATEGY_CONTRACT,
    "acceptance": {
        "formal_reference_return": 20.88493439960298,
        "practical_min_retain_rate": 0.85,
        "caution_retain_rate": 0.80,
        "forbidden": "禁止逐股历史挑选；所有取舍必须落到未来可见的状态、原生信号、源新鲜度、30m确认和仓位分层。",
    },
}


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, low_memory=False) if path.exists() else pd.DataFrame()


def _formal_trades() -> pd.DataFrame:
    df = _read_csv(FORMAL_DIR / f"{MODEL}_closed_trades.csv")
    df = g3._normalize_latest_g3_closed_trades(df)
    df = g3._with_route_strategy_fields(df)
    for col in ["net_ret", "realized_pnl", "stake"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _strategy_metrics(trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    formal_pnl = float(pd.to_numeric(trades["realized_pnl"], errors="coerce").fillna(0).sum())
    for rule in STRATEGY_CONTRACT:
        key = rule["trade_strategy"]
        part = trades[trades["trade_strategy"].eq(key)].copy()
        ret = pd.to_numeric(part["net_ret"], errors="coerce")
        pnl = pd.to_numeric(part["realized_pnl"], errors="coerce").fillna(0)
        rows.append(
            {
                **rule,
                "trade_count": int(len(part)),
                "win_rate": float((ret > 0).mean()) if len(part) else None,
                "avg_trade_return": float(ret.mean()) if len(part) else None,
                "worst_trade": float(ret.min()) if len(part) else None,
                "sum_pnl": float(pnl.sum()),
                "pnl_share": float(pnl.sum() / formal_pnl) if formal_pnl else None,
            }
        )
    return pd.DataFrame(rows)


def _state_metrics(trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for state in DISPATCH_STATES:
        strategies = set(state["enabled_strategies"])
        part = trades[trades["trade_strategy"].isin(strategies)].copy()
        ret = pd.to_numeric(part["net_ret"], errors="coerce")
        pnl = pd.to_numeric(part["realized_pnl"], errors="coerce").fillna(0)
        rows.append(
            {
                "state": state["state"],
                "label": state["label"],
                "primary_strategy": state["primary_strategy"],
                "enabled_strategies": ",".join(state["enabled_strategies"]),
                "definition": state["definition"],
                "trade_count_if_enabled_set": int(len(part)),
                "win_rate_if_enabled_set": float((ret > 0).mean()) if len(part) else None,
                "avg_trade_return_if_enabled_set": float(ret.mean()) if len(part) else None,
                "sum_pnl_if_enabled_set": float(pnl.sum()),
            }
        )
    return pd.DataFrame(rows)


def _read_practical_summary() -> dict[str, Any]:
    path = PRACTICAL_DIR / "summary.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _summary(trades: pd.DataFrame, strategy_metrics: pd.DataFrame) -> dict[str, Any]:
    practical = _read_practical_summary()
    total_return = float(pd.to_numeric(trades["realized_pnl"], errors="coerce").fillna(0).sum() / INITIAL_EQUITY)
    checks = {
        "five_strategies_present": set(strategy_metrics["trade_strategy"]) == {x["trade_strategy"] for x in STRATEGY_CONTRACT},
        "no_unknown_strategy": int((trades["trade_strategy"] == "other").sum()) == 0,
        "formal_reference_return_loaded": total_return > 20.0,
        "practical_retain_rate_ok": float(practical.get("recommended_retain_rate") or 0) >= 0.85,
    }
    return {
        "version": DISPATCH_RULES["version"],
        "ok": bool(all(checks.values())),
        "status": "pass" if all(checks.values()) else "needs_attention",
        "model": MODEL,
        "formal_total_return": total_return,
        "formal_closed_trades": int(len(trades)),
        "practical_recommended_scenario": practical.get("recommended_scenario"),
        "practical_recommended_total_return": practical.get("recommended_total_return"),
        "practical_recommended_retain_rate": practical.get("recommended_retain_rate"),
        "checks": checks,
    }


def _write_report(summary: dict[str, Any], strategy_metrics: pd.DataFrame, state_metrics: pd.DataFrame) -> None:
    lines = [
        "# G3 五策略正式调度合同",
        "",
        "## 合同结论",
        "",
        "这份合同把 G3 从代码里的路由逻辑升级为稳定、可解释、可回测的五策略调度规则。五个策略全部是正式策略，不再把强势突破或恐慌出清修复理解成默认关闭；它们是条件启用策略，满足市场状态和原生信号时进入统一二槽调度。",
        "",
        f"正式参考收益：{_pct(summary['formal_total_return'])}；实用融合推荐收益：{_pct(summary.get('practical_recommended_total_return'))}，收益保留率：{_pct(summary.get('practical_recommended_retain_rate'))}。验收状态：`{summary['status']}`。",
        "",
        "## 每日调度顺序",
        "",
        *[f"{i + 1}. {item}" for i, item in enumerate(DISPATCH_RULES["dispatch_sequence"])],
        "",
        "## 市场状态与启用策略",
        "",
        _md_table(
            state_metrics,
            {"win_rate_if_enabled_set", "avg_trade_return_if_enabled_set"},
            {"sum_pnl_if_enabled_set"},
            max_rows=10,
        ),
        "",
        "## 五策略准入合同",
        "",
        _md_table(
            strategy_metrics,
            {"win_rate", "avg_trade_return", "worst_trade", "pnl_share"},
            {"sum_pnl"},
            max_rows=10,
        ),
        "",
        "## 稳定性原则",
        "",
        "- `机构主升Score120` 与 `震荡弱势修复` 是核心策略，不能删除。",
        "- `强势突破` 是进攻条件策略，出现强趋势/突破原生候选时自动启用，不为追历史收益硬开。",
        "- `恐慌出清修复` 是防守条件策略，出现恐慌扩散或下跌出清时自动启用，非恐慌状态不硬开。",
        "- `量能续强补位` 只在二槽未满且 G2 源新鲜时补位，不替代 G3 主路由。",
        "- 所有策略共用二槽、板块暴露、30m确认、止盈止损和账户安全约束。",
        "",
        "## 回测验收",
        "",
        "- 正式完整合同用于对齐页面高收益基准。",
        "- 实用融合合同允许收益略低，但默认收益保留率不低于 85%。",
        "- 禁止逐股历史挑选；策略启停必须来自未来可见的市场状态、原生信号、源新鲜度、30m确认和仓位分层。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = _formal_trades()
    strategy_metrics = _strategy_metrics(trades)
    state_metrics = _state_metrics(trades)
    summary = _summary(trades, strategy_metrics)
    (OUT_DIR / "dispatch_contract.json").write_text(json.dumps(DISPATCH_RULES, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    strategy_metrics.to_csv(OUT_DIR / "strategy_dispatch_contract.csv", index=False, encoding="utf-8-sig")
    state_metrics.to_csv(OUT_DIR / "state_dispatch_contract.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_DIR / "formal_trades_with_dispatch_strategy.csv", index=False, encoding="utf-8-sig")
    _write_report(summary, strategy_metrics, state_metrics)
    print(json.dumps({"output_dir": str(OUT_DIR), **summary}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
