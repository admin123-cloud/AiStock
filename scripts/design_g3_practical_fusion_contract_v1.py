from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api.gen3_state_alpha as g3  # noqa: E402
from scripts.backtest_g3_five_strategies_from_scratch_v1 import _md_table, _pct  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("g3_practical_fusion_contract_v1")
FORMAL_DIR = report_path("g2_g3_market_style_router_v1")
MODEL = "g3_final_with_g2_gap_supplement"
INITIAL_EQUITY = 1_000_000.0
MIN_ACCEPT_RETAIN_RATE = 0.85
CAUTION_RETAIN_RATE = 0.80


STRATEGY_LABELS = {
    "institutional_score120_mainwave": "机构主升Score120",
    "old_g3_strong_breakout": "强势突破",
    "panic_capitulation_repair": "恐慌出清修复",
    "range_weak_repair": "震荡弱势修复",
    "volume_runup_supplement": "量能续强补位",
}

CORE_STRATEGIES = {"institutional_score120_mainwave", "range_weak_repair"}

NAMED_SCENARIOS = {
    "formal_five_full": {
        "label": "五策略全量正式合同",
        "strategies": list(STRATEGY_LABELS),
        "policy": "完整保留 5 个策略主体，收益与页面正式版一致；适合作为收益上限和完整执行口径。",
    },
    "core_four_without_panic": {
        "label": "核心四策略-暂缓恐慌",
        "strategies": [
            "institutional_score120_mainwave",
            "old_g3_strong_breakout",
            "range_weak_repair",
            "volume_runup_supplement",
        ],
        "policy": "保留主升、突破、震荡修复和 G2 补位；恐慌策略只在极端市场作为观察/低仓位开关。",
    },
    "core_four_without_strong": {
        "label": "核心四策略-暂缓强突",
        "strategies": [
            "institutional_score120_mainwave",
            "panic_capitulation_repair",
            "range_weak_repair",
            "volume_runup_supplement",
        ],
        "policy": "保留主升、修复和补位；强势突破因交易少、阶段性强，作为二级进攻开关。",
    },
    "core_four_without_g2": {
        "label": "核心四策略-无G2补位",
        "strategies": [
            "institutional_score120_mainwave",
            "old_g3_strong_breakout",
            "panic_capitulation_repair",
            "range_weak_repair",
        ],
        "policy": "完全不依赖 G2 补位，检验 G3 自身闭环收益；G2 可作为未来空档补位而非收益硬依赖。",
    },
    "score_range_g2": {
        "label": "三策略-主升修复补位",
        "strategies": ["institutional_score120_mainwave", "range_weak_repair", "volume_runup_supplement"],
        "policy": "最顺滑的未来执行骨架：主升负责进攻，震荡弱势负责修复，G2 只补空档。",
    },
    "score_range_strong": {
        "label": "三策略-主升修复强突",
        "strategies": ["institutional_score120_mainwave", "old_g3_strong_breakout", "range_weak_repair"],
        "policy": "偏进攻版本：用强势突破替代 G2 补位。",
    },
    "score_range_panic": {
        "label": "三策略-主升双修复",
        "strategies": ["institutional_score120_mainwave", "panic_capitulation_repair", "range_weak_repair"],
        "policy": "偏防守版本：保留主升和两类修复，收益保留率接近警戒线。",
    },
    "score_range_only": {
        "label": "双核心-主升+震荡修复",
        "strategies": ["institutional_score120_mainwave", "range_weak_repair"],
        "policy": "最小核心骨架；收益保留刚过 80%，适合作为下限，不建议直接作为正式全量口径。",
    },
}


def _read_formal_trades() -> pd.DataFrame:
    path = FORMAL_DIR / f"{MODEL}_closed_trades.csv"
    df = pd.read_csv(path, low_memory=False)
    df = g3._normalize_latest_g3_closed_trades(df)
    df = g3._with_route_strategy_fields(df)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["exit_date"] = pd.to_datetime(df["exit_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["net_ret", "stake", "realized_pnl", "account_ret"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _realized_equity_stats(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {"realized_curve_max_drawdown": None, "final_equity": INITIAL_EQUITY}
    daily = trades.groupby("exit_date", dropna=False)["realized_pnl"].sum().reset_index()
    daily = daily.sort_values("exit_date")
    daily["equity"] = INITIAL_EQUITY + daily["realized_pnl"].cumsum()
    daily["peak"] = daily["equity"].cummax()
    daily["drawdown"] = daily["equity"] / daily["peak"] - 1.0
    return {
        "realized_curve_max_drawdown": float(daily["drawdown"].min()),
        "final_equity": float(daily["equity"].iloc[-1]),
    }


def _scenario_metrics(trades: pd.DataFrame, strategies: list[str], scenario: str, label: str, policy: str, formal_return: float) -> dict[str, Any]:
    part = trades[trades["trade_strategy"].isin(strategies)].copy()
    ret = pd.to_numeric(part["net_ret"], errors="coerce")
    pnl = pd.to_numeric(part["realized_pnl"], errors="coerce").fillna(0)
    total_return = float(pnl.sum() / INITIAL_EQUITY)
    top5 = part.sort_values("realized_pnl", ascending=False).head(5)
    top5_share = float(pd.to_numeric(top5["realized_pnl"], errors="coerce").sum() / pnl.sum()) if pnl.sum() else None
    stats = _realized_equity_stats(part)
    has_core = CORE_STRATEGIES.issubset(set(strategies))
    retain_rate = total_return / formal_return if formal_return else None
    if retain_rate is not None and retain_rate >= MIN_ACCEPT_RETAIN_RATE and has_core:
        tier = "pass"
    elif retain_rate is not None and retain_rate >= CAUTION_RETAIN_RATE and has_core:
        tier = "caution"
    else:
        tier = "reject"
    return {
        "scenario": scenario,
        "label": label,
        "tier": tier,
        "policy": policy,
        "strategy_count": len(strategies),
        "strategies": ",".join(strategies),
        "strategy_labels": " / ".join(STRATEGY_LABELS[s] for s in strategies),
        "trade_count": int(len(part)),
        "win_rate": float((ret > 0).mean()) if len(part) else None,
        "avg_trade_return": float(ret.mean()) if len(part) else None,
        "worst_trade": float(ret.min()) if len(part) else None,
        "total_return": total_return,
        "retain_rate_vs_formal": retain_rate,
        "sum_pnl": float(pnl.sum()),
        "top5_pnl_share": top5_share,
        "has_core_score_and_range": has_core,
        **stats,
    }


def _enumerate_strategy_subsets(trades: pd.DataFrame, formal_return: float) -> pd.DataFrame:
    rows = []
    strategies = list(STRATEGY_LABELS)
    for n in range(1, len(strategies) + 1):
        for combo in itertools.combinations(strategies, n):
            rows.append(
                _scenario_metrics(
                    trades,
                    list(combo),
                    "subset_" + "_".join(combo),
                    f"{n}策略组合",
                    "机械枚举，仅用于判断收益保留率，不作为正式逐股优化。",
                    formal_return,
                )
            )
    return pd.DataFrame(rows).sort_values(["tier", "total_return"], ascending=[True, False])


def _strategy_admission(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    formal_pnl = float(pd.to_numeric(trades["realized_pnl"], errors="coerce").fillna(0).sum())
    for strategy, label in STRATEGY_LABELS.items():
        part = trades[trades["trade_strategy"] == strategy].copy()
        ret = pd.to_numeric(part["net_ret"], errors="coerce")
        pnl = pd.to_numeric(part["realized_pnl"], errors="coerce").fillna(0)
        if strategy in CORE_STRATEGIES:
            role = "core"
            admission = "正式默认保留"
        elif strategy == "volume_runup_supplement":
            role = "supplement"
            admission = "只在二槽未满且 G2 源新鲜时补位"
        elif strategy == "old_g3_strong_breakout":
            role = "offense_switch"
            admission = "保留，但需要强势突破质量 gate 与仓位约束"
        else:
            role = "defense_switch"
            admission = "保留，但只在恐慌/下跌状态满足时启用，可低仓位"
        rows.append(
            {
                "trade_strategy": strategy,
                "trade_strategy_label": label,
                "role": role,
                "admission": admission,
                "trade_count": int(len(part)),
                "win_rate": float((ret > 0).mean()) if len(part) else None,
                "avg_trade_return": float(ret.mean()) if len(part) else None,
                "worst_trade": float(ret.min()) if len(part) else None,
                "sum_pnl": float(pnl.sum()),
                "pnl_share": float(pnl.sum() / formal_pnl) if formal_pnl else None,
            }
        )
    return pd.DataFrame(rows)


def _write_report(summary: dict[str, Any], named: pd.DataFrame, admission: pd.DataFrame, subsets: pd.DataFrame) -> None:
    lines = [
        "# G3 实用融合交易合同设计",
        "",
        "## 结论",
        "",
        "融合后的收益允许低于页面正式版，但不能把收益大幅牺牲给一个未来难执行的简化模型。验收口径改为：不逐股挑历史赢家，按未来可执行的策略主体/子来源规则取舍；默认收益保留率应不低于 85%，80%-85% 只能作为警戒下限。",
        "",
        f"页面正式版收益为 {_pct(summary['formal_total_return'])}。当前建议的实用默认方案是 `score_range_g2`：机构主升Score120 + 震荡弱势修复 + G2量能续强补位，收益保留 {_pct(summary['recommended_retain_rate'])}，全周期收益 {_pct(summary['recommended_total_return'])}。强势突破和恐慌出清修复不删除，作为进攻/防守开关保留，但不强行追求历史满收益。",
        "",
        "## 命名场景矩阵",
        "",
        _md_table(
            named,
            {"win_rate", "avg_trade_return", "worst_trade", "total_return", "retain_rate_vs_formal", "top5_pnl_share", "realized_curve_max_drawdown"},
            {"sum_pnl", "final_equity"},
            max_rows=20,
        ),
        "",
        "## 策略准入角色",
        "",
        _md_table(
            admission,
            {"win_rate", "avg_trade_return", "worst_trade", "pnl_share"},
            {"sum_pnl"},
            max_rows=10,
        ),
        "",
        "## 收益保留率最高的组合",
        "",
        _md_table(
            subsets.head(12),
            {"win_rate", "avg_trade_return", "worst_trade", "total_return", "retain_rate_vs_formal", "top5_pnl_share"},
            {"sum_pnl"},
            max_rows=12,
        ),
        "",
        "## 正式执行建议",
        "",
        "1. `机构主升Score120` 和 `震荡弱势修复` 是不可删除核心；删掉任意一个都会让收益结构明显失真。",
        "2. `量能续强补位` 适合作为未来丝滑运行的补位策略：只在 G3 主路由未占满二槽且 G2 源新鲜时启用。",
        "3. `强势突破` 保留为进攻开关，不因交易少而删除，但需要质量 gate 和仓位约束。",
        "4. `恐慌出清修复` 保留为防守/极端状态开关，不为了追求历史收益而在非恐慌状态硬开。",
        "5. 禁止逐股历史挑选；所有取舍必须落到策略主体、市场状态、源新鲜度、30m确认、仓位分层这些未来可见条件上。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = _read_formal_trades()
    formal_total_return = float(pd.to_numeric(trades["realized_pnl"], errors="coerce").fillna(0).sum() / INITIAL_EQUITY)
    named_rows = []
    for scenario, config in NAMED_SCENARIOS.items():
        named_rows.append(
            _scenario_metrics(
                trades,
                config["strategies"],
                scenario,
                config["label"],
                config["policy"],
                formal_total_return,
            )
        )
    named = pd.DataFrame(named_rows).sort_values("total_return", ascending=False)
    admission = _strategy_admission(trades)
    subsets = _enumerate_strategy_subsets(trades, formal_total_return)
    recommended = named[named["scenario"] == "score_range_g2"].iloc[0].to_dict()
    summary = {
        "version": "g3_practical_fusion_contract_v1",
        "status": "pass" if recommended["retain_rate_vs_formal"] >= MIN_ACCEPT_RETAIN_RATE else "caution",
        "ok": bool(recommended["retain_rate_vs_formal"] >= MIN_ACCEPT_RETAIN_RATE),
        "formal_total_return": formal_total_return,
        "min_accept_retain_rate": MIN_ACCEPT_RETAIN_RATE,
        "caution_retain_rate": CAUTION_RETAIN_RATE,
        "recommended_scenario": "score_range_g2",
        "recommended_total_return": recommended["total_return"],
        "recommended_retain_rate": recommended["retain_rate_vs_formal"],
        "recommended_trade_count": int(recommended["trade_count"]),
        "principle": "允许收益略低于页面正式版，但默认保留率不低于85%；禁止逐股历史挑选，取舍只能来自未来可执行的策略和状态规则。",
    }
    named.to_csv(OUT_DIR / "named_scenario_matrix.csv", index=False, encoding="utf-8-sig")
    admission.to_csv(OUT_DIR / "strategy_admission_policy.csv", index=False, encoding="utf-8-sig")
    subsets.to_csv(OUT_DIR / "strategy_subset_matrix.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_DIR / "formal_trades_with_practical_strategy.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _write_report(summary, named, admission, subsets)
    print(json.dumps({"output_dir": str(OUT_DIR), **summary}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
