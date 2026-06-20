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


OUT_DIR = report_path("g3_formal_unified_five_strategy_contract_v1")
FORMAL_DIR = report_path("g2_g3_market_style_router_v1")
MODEL = "g3_final_with_g2_gap_supplement"
INITIAL_EQUITY = 1_000_000.0


TRADE_STRATEGY_ORDER = [
    "institutional_score120_mainwave",
    "old_g3_strong_breakout",
    "panic_capitulation_repair",
    "range_weak_repair",
    "volume_runup_supplement",
]

STRATEGY_CONTRACTS = {
    "institutional_score120_mainwave": {
        "trade_strategy_label": "机构主升Score120",
        "subject": "主升浪进攻策略，保留 Score120/机构主升原生买点。",
        "entry_contract": "沿用正式 G3 final 中 institutional_mainwave 原生候选、主升评分和二槽选票规则。",
        "exit_contract": "沿用正式 G3 final：12%先减半、剩余仓前低保护、策略 policy_exit 与原生 30m 退出语义。",
    },
    "old_g3_strong_breakout": {
        "trade_strategy_label": "强势突破",
        "subject": "旧G3强势突破类进攻策略，不再叫旧G3强势突破。",
        "entry_contract": "沿用正式 G3 final 中 old_g3 strong_main/强势突破原生信号。",
        "exit_contract": "沿用正式 G3 final 的统一止盈止损和前低保护合同。",
    },
    "panic_capitulation_repair": {
        "trade_strategy_label": "恐慌出清修复",
        "subject": "把旧G3恐慌修复与下跌恐慌修复收紧到一个策略主体。",
        "entry_contract": "沿用正式 G3 final 中 panic_repair 与 old_g3 down_panic 的原生恐慌修复信号，并保留市场压力分层。",
        "exit_contract": "沿用正式 G3 final 的 30m 修复确认、分批止盈、前低保护与 policy_exit。",
    },
    "range_weak_repair": {
        "trade_strategy_label": "震荡弱势修复",
        "subject": "把旧G3弱势/震荡修复与震荡恐慌修复统一到一个修复策略主体。",
        "entry_contract": "沿用正式 G3 final 中 range_gap、panic_repair_range 等震荡/弱势原生信号。",
        "exit_contract": "沿用正式 G3 final 的统一卖出合同，不使用日线代理退出替代。",
    },
    "volume_runup_supplement": {
        "trade_strategy_label": "量能续强补位",
        "subject": "G2 空档补位策略主体，只在 G3 主路由未占满二槽时补位。",
        "entry_contract": "沿用正式 G3 final 的 g2_gap_supplement 源和补位约束，不替代 G3 主路由。",
        "exit_contract": "沿用正式 G3 final 的统一止盈止损和前低保护合同。",
    },
}


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, low_memory=False) if path.exists() else pd.DataFrame()


def _read_formal_summary() -> dict[str, Any]:
    df = _read_csv(FORMAL_DIR / "summary.csv")
    if df.empty:
        return {}
    row = df[(df.get("model") == MODEL) | (df.get("contract") == MODEL)]
    if row.empty:
        return {}
    item = row.iloc[0].to_dict()
    return {
        "model": MODEL,
        "total_return": float(item.get("return", item.get("total_return"))),
        "max_drawdown": float(item.get("max_drawdown")),
        "trade_count": int(float(item.get("trades", item.get("trade_count")))),
        "win_rate": float(item.get("win_rate")),
        "avg_trade_return": float(item.get("avg_trade_return")),
        "sum_pnl": float(item.get("sum_pnl")),
    }


def _enrich(df: pd.DataFrame, normalize_closed: bool = False) -> pd.DataFrame:
    if df.empty:
        return df
    out = g3._normalize_latest_g3_closed_trades(df) if normalize_closed else df.copy()
    out = g3._with_route_strategy_fields(out)
    if "entry_date" in out.columns:
        out["entry_date"] = pd.to_datetime(out["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["net_ret", "account_ret", "stake", "realized_pnl", "score", "rank_key"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def _strategy_metrics(trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for strategy in TRADE_STRATEGY_ORDER:
        part = trades[trades["trade_strategy"] == strategy].copy()
        ret = pd.to_numeric(part.get("net_ret"), errors="coerce")
        pnl = pd.to_numeric(part.get("realized_pnl"), errors="coerce").fillna(0.0)
        stake = pd.to_numeric(part.get("stake"), errors="coerce").fillna(0.0)
        top_sources = []
        if "route_strategy_label" in part.columns:
            top_sources = list(dict.fromkeys(part["route_strategy_label"].dropna().astype(str).tolist()))[:8]
        rows.append(
            {
                "trade_strategy": strategy,
                "trade_strategy_label": STRATEGY_CONTRACTS[strategy]["trade_strategy_label"],
                "trade_count": int(len(part)),
                "win_rate": float((ret > 0).mean()) if len(part) else None,
                "avg_trade_return": float(ret.mean()) if len(part) else None,
                "worst_trade": float(ret.min()) if len(part) else None,
                "best_trade": float(ret.max()) if len(part) else None,
                "sum_stake": float(stake.sum()),
                "sum_pnl": float(pnl.sum()),
                "pnl_contribution_rate": None,
                "source_strategy_labels": " / ".join(top_sources),
                **STRATEGY_CONTRACTS[strategy],
            }
        )
    df = pd.DataFrame(rows)
    total_pnl = pd.to_numeric(df["sum_pnl"], errors="coerce").sum()
    if total_pnl:
        df["pnl_contribution_rate"] = df["sum_pnl"] / total_pnl
    return df


def _route_metrics(trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if "route_parent" not in trades.columns:
        return pd.DataFrame()
    for (route, label), part in trades.groupby(["route_parent", "route_parent_label"], dropna=False):
        ret = pd.to_numeric(part.get("net_ret"), errors="coerce")
        pnl = pd.to_numeric(part.get("realized_pnl"), errors="coerce").fillna(0.0)
        rows.append(
            {
                "route_parent": route,
                "route_parent_label": label,
                "trade_count": int(len(part)),
                "win_rate": float((ret > 0).mean()) if len(part) else None,
                "avg_trade_return": float(ret.mean()) if len(part) else None,
                "sum_pnl": float(pnl.sum()),
            }
        )
    return pd.DataFrame(rows).sort_values("sum_pnl", ascending=False)


def _window_metrics(trades: pd.DataFrame) -> pd.DataFrame:
    formal = _read_csv(FORMAL_DIR / "window_summary.csv")
    if formal.empty:
        return pd.DataFrame()
    formal = formal[formal["model"] == MODEL].copy()
    return formal


def _selected_metrics(selected: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if selected.empty:
        return pd.DataFrame()
    for strategy in TRADE_STRATEGY_ORDER:
        part = selected[selected["trade_strategy"] == strategy].copy()
        rows.append(
            {
                "trade_strategy": strategy,
                "trade_strategy_label": STRATEGY_CONTRACTS[strategy]["trade_strategy_label"],
                "selected_count": int(len(part)),
                "first_entry_date": part["entry_date"].min() if len(part) and "entry_date" in part.columns else None,
                "last_entry_date": part["entry_date"].max() if len(part) and "entry_date" in part.columns else None,
            }
        )
    return pd.DataFrame(rows)


def _concentration(trades: pd.DataFrame) -> pd.DataFrame:
    cols = ["code", "name", "entry_date", "trade_strategy", "trade_strategy_label", "route_strategy_label", "net_ret", "stake", "realized_pnl", "exit_reason"]
    available = [col for col in cols if col in trades.columns]
    return trades.sort_values("realized_pnl", ascending=False)[available].head(30)


def _summary(trades: pd.DataFrame, equity: pd.DataFrame, selected: pd.DataFrame, strategy_metrics: pd.DataFrame) -> dict[str, Any]:
    formal = _read_formal_summary()
    final_equity = float(pd.to_numeric(equity.get("equity"), errors="coerce").dropna().iloc[-1]) if not equity.empty else None
    total_return_from_equity = (final_equity / INITIAL_EQUITY - 1.0) if final_equity is not None else None
    max_drawdown_from_equity = float(pd.to_numeric(equity.get("drawdown"), errors="coerce").min()) if not equity.empty else None
    trade_count = int(len(trades))
    strategies_present = sorted([x for x in trades["trade_strategy"].dropna().astype(str).unique().tolist() if x != "other"])
    missing_strategies = [s for s in TRADE_STRATEGY_ORDER if s not in strategies_present]
    unknown_count = int((trades["trade_strategy"] == "other").sum()) if "trade_strategy" in trades.columns else trade_count
    checks = {
        "return_matches_formal": abs((formal.get("total_return") or 0) - (total_return_from_equity or 0)) < 1e-8,
        "max_drawdown_matches_formal": abs((formal.get("max_drawdown") or 0) - (max_drawdown_from_equity or 0)) < 1e-8,
        "trade_count_matches_formal": int(formal.get("trade_count") or -1) == trade_count,
        "all_five_strategies_present": not missing_strategies,
        "no_unknown_trade_strategy": unknown_count == 0,
    }
    return {
        "version": "g3_formal_unified_five_strategy_contract_v1",
        "status": "pass" if all(checks.values()) else "needs_attention",
        "ok": bool(all(checks.values())),
        "model": MODEL,
        "principle": "以正式 G3 final 交易合同为执行口径，以 5 个 trade_strategy 作为统一策略主体和归因口径。",
        "formal_summary": formal,
        "total_return": total_return_from_equity,
        "max_drawdown": max_drawdown_from_equity,
        "final_equity": final_equity,
        "closed_trades": trade_count,
        "selected_rows": int(len(selected)),
        "win_rate": float((pd.to_numeric(trades.get("net_ret"), errors="coerce") > 0).mean()) if trade_count else None,
        "avg_trade_return": float(pd.to_numeric(trades.get("net_ret"), errors="coerce").mean()) if trade_count else None,
        "sum_pnl": float(pd.to_numeric(trades.get("realized_pnl"), errors="coerce").fillna(0).sum()),
        "strategy_count": int(len(strategies_present)),
        "strategies_present": strategies_present,
        "missing_strategies": missing_strategies,
        "unknown_trade_strategy_count": unknown_count,
        "checks": checks,
        "strategy_metrics": strategy_metrics.astype(object).where(pd.notna(strategy_metrics), None).to_dict(orient="records"),
    }


def _write_report(summary: dict[str, Any], strategy_metrics: pd.DataFrame, route_metrics: pd.DataFrame, windows: pd.DataFrame, concentration: pd.DataFrame) -> None:
    formal = summary.get("formal_summary") or {}
    lines = [
        "# G3 正式统一五策略交易合同",
        "",
        "## 结论",
        "",
        "本产物不是日线代理回测，也不是桥接模拟器。它以当前正式 `g3_final_with_g2_gap_supplement` 的成交、选票、收益曲线为准，只把交易归因和未来配置主体统一到 5 个 `trade_strategy`。",
        "",
        f"正式合同收益保持为 {_pct(summary.get('total_return'))}，最大回撤 {_pct(summary.get('max_drawdown'))}，成交 {summary.get('closed_trades')} 笔；与页面正式收益 {_pct(formal.get('total_return'))} 完全一致。",
        "",
        f"验收状态：`{summary.get('status')}`。检查项：{json.dumps(summary.get('checks'), ensure_ascii=False)}",
        "",
        "## 五个正式策略主体",
        "",
        _md_table(
            strategy_metrics,
            {"win_rate", "avg_trade_return", "worst_trade", "best_trade", "pnl_contribution_rate"},
            {"sum_stake", "sum_pnl"},
            max_rows=10,
        ),
        "",
        "## 父路由贡献",
        "",
        _md_table(route_metrics, {"win_rate", "avg_trade_return"}, {"sum_pnl"}, max_rows=10),
        "",
        "## 窗口表现",
        "",
        _md_table(windows, {"return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade"}, set(), max_rows=20),
        "",
        "## 高贡献样本",
        "",
        _md_table(concentration, {"net_ret"}, {"stake", "realized_pnl"}, max_rows=20),
        "",
        "## 执行定义",
        "",
        "1. 未来运行的正式买入流使用 G3 final 的原生候选和二槽选票合同。",
        "2. 未来运行的正式卖出流使用 G3 final 的原生 30m 分批止盈、前低保护、硬止损和 policy_exit 语义。",
        "3. 5 个 `trade_strategy` 负责统一策略名称、归因、参数配置和风控展示，不替代原生买卖点生成器。",
        "4. 桥接/日线代理回测只作为验收回归和反例审计，不作为正式收益口径。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = _enrich(_read_csv(FORMAL_DIR / f"{MODEL}_closed_trades.csv"), normalize_closed=True)
    selected = _enrich(_read_csv(FORMAL_DIR / f"{MODEL}_selected_candidates.csv"), normalize_closed=False)
    equity = _read_csv(FORMAL_DIR / f"{MODEL}_equity_curve.csv")
    windows = _window_metrics(trades)
    strategy_metrics = _strategy_metrics(trades)
    route_metrics = _route_metrics(trades)
    selected_metrics = _selected_metrics(selected)
    concentration = _concentration(trades)
    summary = _summary(trades, equity, selected, strategy_metrics)

    trades.to_csv(OUT_DIR / "formal_unified_closed_trades.csv", index=False, encoding="utf-8-sig")
    selected.to_csv(OUT_DIR / "formal_unified_selected_candidates.csv", index=False, encoding="utf-8-sig")
    equity.to_csv(OUT_DIR / "formal_unified_equity_curve.csv", index=False, encoding="utf-8-sig")
    strategy_metrics.to_csv(OUT_DIR / "strategy_metrics.csv", index=False, encoding="utf-8-sig")
    route_metrics.to_csv(OUT_DIR / "route_parent_metrics.csv", index=False, encoding="utf-8-sig")
    selected_metrics.to_csv(OUT_DIR / "selected_strategy_metrics.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_summary.csv", index=False, encoding="utf-8-sig")
    concentration.to_csv(OUT_DIR / "top_contribution_trades.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "formal_strategy_contract.json").write_text(json.dumps(STRATEGY_CONTRACTS, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _write_report(summary, strategy_metrics, route_metrics, windows, concentration)
    print(json.dumps({"output_dir": str(OUT_DIR), **summary}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
