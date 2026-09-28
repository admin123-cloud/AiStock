from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api.gen3_state_alpha as g3  # noqa: E402
from scripts.backtest_g3_five_strategies_from_scratch_v1 import _md_table, _money, _pct  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("g3_formal_five_strategy_overfit_audit_v1")
FORMAL_DIR = report_path("g2_g3_market_style_router_v1")
DISPATCH_DIR = report_path("g3_formal_five_strategy_dispatch_contract_v1")
PRACTICAL_DIR = report_path("g3_practical_fusion_contract_v1")
MODEL = "g3_final_with_g2_gap_supplement"
INITIAL_EQUITY = 1_000_000.0


STRATEGY_LABELS = {
    "institutional_score120_mainwave": "机构主升Score120",
    "range_weak_repair": "震荡弱势修复",
    "old_g3_strong_breakout": "强势突破",
    "panic_capitulation_repair": "恐慌出清修复",
    "volume_runup_supplement": "量能续强补位",
}


WINDOWS = [
    ("2020_2021", "2020-01-01", "2021-12-31", "2020-2021"),
    ("2022_bear", "2022-01-01", "2022-12-31", "2022熊市"),
    ("2023", "2023-01-01", "2023-12-31", "2023"),
    ("2024", "2024-01-01", "2024-12-31", "2024"),
    ("2025", "2025-01-01", "2025-12-31", "2025"),
    ("2026ytd", "2026-01-01", "2026-12-31", "2026年内"),
]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_trades() -> pd.DataFrame:
    df = pd.read_csv(FORMAL_DIR / f"{MODEL}_closed_trades.csv", low_memory=False)
    df = g3._normalize_latest_g3_closed_trades(df)
    df = g3._with_route_strategy_fields(df)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce")
    df["exit_date"] = pd.to_datetime(df.get("exit_date", df.get("policy_exit_date")), errors="coerce")
    for col in ["net_ret", "stake", "realized_pnl", "account_ret", "score", "rank_key"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _overall_metrics(trades: pd.DataFrame) -> dict[str, Any]:
    summary_rows = pd.read_csv(FORMAL_DIR / "summary.csv", low_memory=False)
    row = summary_rows[summary_rows["model"].eq(MODEL)].iloc[0].to_dict()
    dispatch = _read_json(DISPATCH_DIR / "summary.json")
    practical = _read_json(PRACTICAL_DIR / "summary.json")
    return {
        "model": MODEL,
        "contract": "g3_formal_five_strategy_dispatch_contract_v1",
        "total_return": float(row["return"]),
        "max_drawdown": float(row["max_drawdown"]),
        "trade_count": int(row["trades"]),
        "win_rate": float(row["win_rate"]),
        "avg_trade_return": float(row["avg_trade_return"]),
        "worst_trade": float(row["worst_trade"]),
        "best_trade": float(row["best_trade"]),
        "sum_pnl": float(row["sum_pnl"]),
        "dispatch_ok": bool(dispatch.get("ok")),
        "practical_retain_rate": practical.get("recommended_retain_rate"),
        "practical_total_return": practical.get("recommended_total_return"),
    }


def _strategy_metrics(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    total_pnl = float(trades["realized_pnl"].fillna(0).sum())
    for strategy, label in STRATEGY_LABELS.items():
        part = trades[trades["trade_strategy"].eq(strategy)].copy()
        ret = part["net_ret"]
        pnl = part["realized_pnl"].fillna(0)
        top1 = float(pnl.max()) if len(part) else 0.0
        rows.append(
            {
                "trade_strategy": strategy,
                "trade_strategy_label": label,
                "trade_count": int(len(part)),
                "win_rate": float((ret > 0).mean()) if len(part) else None,
                "avg_trade_return": float(ret.mean()) if len(part) else None,
                "worst_trade": float(ret.min()) if len(part) else None,
                "best_trade": float(ret.max()) if len(part) else None,
                "sum_pnl": float(pnl.sum()),
                "pnl_share": float(pnl.sum() / total_pnl) if total_pnl else None,
                "top1_pnl_share_within_strategy": float(top1 / pnl.sum()) if pnl.sum() else None,
                "risk_flag": _strategy_risk_flag(strategy, len(part), float(pnl.sum() / total_pnl) if total_pnl else 0.0),
            }
        )
    return pd.DataFrame(rows)


def _strategy_risk_flag(strategy: str, count: int, pnl_share: float) -> str:
    flags = []
    if count < 20:
        flags.append("sample_lt_20")
    elif count < 30:
        flags.append("sample_lt_30")
    if pnl_share > 0.45:
        flags.append("high_pnl_concentration")
    if strategy in {"old_g3_strong_breakout", "panic_capitulation_repair"}:
        flags.append("conditional_strategy")
    return ",".join(flags) or "ok"


def _window_strategy_metrics(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for key, start, end, label in WINDOWS:
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        win = trades[trades["entry_date"].between(start_ts, end_ts)].copy()
        for strategy, strategy_label in STRATEGY_LABELS.items():
            part = win[win["trade_strategy"].eq(strategy)].copy()
            pnl = part["realized_pnl"].fillna(0)
            ret = part["net_ret"]
            rows.append(
                {
                    "window": key,
                    "window_label": label,
                    "trade_strategy": strategy,
                    "trade_strategy_label": strategy_label,
                    "trade_count": int(len(part)),
                    "sum_pnl": float(pnl.sum()),
                    "return_on_initial_equity": float(pnl.sum() / INITIAL_EQUITY),
                    "win_rate": float((ret > 0).mean()) if len(part) else None,
                    "avg_trade_return": float(ret.mean()) if len(part) else None,
                    "worst_trade": float(ret.min()) if len(part) else None,
                }
            )
    return pd.DataFrame(rows)


def _overall_window_metrics() -> pd.DataFrame:
    df = pd.read_csv(FORMAL_DIR / "window_summary.csv", low_memory=False)
    return df[df["model"].eq(MODEL)].copy()


def _concentration_metrics(trades: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame]:
    ordered = trades.sort_values("realized_pnl", ascending=False).copy()
    total_pnl = float(ordered["realized_pnl"].fillna(0).sum())
    top = ordered.head(20)[
        [
            "code",
            "name",
            "entry_date",
            "trade_strategy",
            "trade_strategy_label",
            "route_strategy_label",
            "net_ret",
            "realized_pnl",
            "exit_reason",
        ]
    ].copy()
    for col in ["entry_date"]:
        top[col] = pd.to_datetime(top[col], errors="coerce").dt.strftime("%Y-%m-%d")
    meta = {
        "top1_pnl_share": float(ordered.head(1)["realized_pnl"].sum() / total_pnl) if total_pnl else None,
        "top3_pnl_share": float(ordered.head(3)["realized_pnl"].sum() / total_pnl) if total_pnl else None,
        "top5_pnl_share": float(ordered.head(5)["realized_pnl"].sum() / total_pnl) if total_pnl else None,
        "top10_pnl_share": float(ordered.head(10)["realized_pnl"].sum() / total_pnl) if total_pnl else None,
        "positive_trade_count": int((ordered["net_ret"] > 0).sum()),
        "negative_trade_count": int((ordered["net_ret"] <= 0).sum()),
    }
    return meta, top


def _overfit_checks(overall: dict[str, Any], strategy: pd.DataFrame, concentration: dict[str, Any], windows: pd.DataFrame) -> pd.DataFrame:
    checks = []
    top5 = float(concentration.get("top5_pnl_share") or 0)
    top10 = float(concentration.get("top10_pnl_share") or 0)
    score_share = float(strategy.loc[strategy["trade_strategy"].eq("institutional_score120_mainwave"), "pnl_share"].iloc[0])
    strong_count = int(strategy.loc[strategy["trade_strategy"].eq("old_g3_strong_breakout"), "trade_count"].iloc[0])
    panic_count = int(strategy.loc[strategy["trade_strategy"].eq("panic_capitulation_repair"), "trade_count"].iloc[0])
    negative_windows = int((pd.to_numeric(windows["return"], errors="coerce") < 0).sum()) if not windows.empty else 0
    checks.append(
        {
            "check": "total_sample_size",
            "status": "pass" if overall["trade_count"] >= 150 else "warn",
            "evidence": f"closed_trades={overall['trade_count']}",
            "interpretation": "总样本数够做策略级判断，但不是足够做逐股/细参数优化。",
        }
    )
    checks.append(
        {
            "check": "small_conditional_strategy_samples",
            "status": "warn" if strong_count < 20 or panic_count < 30 else "pass",
            "evidence": f"strong={strong_count}, panic={panic_count}",
            "interpretation": "强势突破和恐慌出清修复可保留为条件启用，但不应在此样本量下继续细调参数。",
        }
    )
    checks.append(
        {
            "check": "pnl_concentration",
            "status": "warn" if top5 > 0.35 or top10 > 0.50 else "pass",
            "evidence": f"top5={top5:.1%}, top10={top10:.1%}",
            "interpretation": "收益若过度集中于少数交易，未来实盘的路径依赖会更强。",
        }
    )
    checks.append(
        {
            "check": "strategy_concentration",
            "status": "warn" if score_share > 0.45 else "pass",
            "evidence": f"Score120 pnl_share={score_share:.1%}",
            "interpretation": "收益主引擎集中在 Score120，这是可接受的进攻核心，但不能假装五个策略贡献均衡。",
        }
    )
    checks.append(
        {
            "check": "window_stability",
            "status": "pass" if negative_windows == 0 else "warn",
            "evidence": f"negative_formal_windows={negative_windows}",
            "interpretation": "正式窗口没有负收益窗口更好；若有负窗口，需保留压力期降仓或暂停规则。",
        }
    )
    checks.append(
        {
            "check": "live_reproducibility",
            "status": "warn",
            "evidence": "formal backtest depends on native source signals and native 30m exit semantics",
            "interpretation": "实盘最大风险不是历史收益，而是当前候选、30m确认、卖出合同是否完全复现历史语义。",
        }
    )
    return pd.DataFrame(checks)


def _verdict(checks: pd.DataFrame) -> dict[str, Any]:
    warn_count = int((checks["status"] == "warn").sum())
    fail_count = int((checks["status"] == "fail").sum())
    if fail_count:
        status = "fail"
        tradable = False
    elif warn_count >= 4:
        status = "conditional"
        tradable = False
    else:
        status = "conditional_pass"
        tradable = True
    return {
        "status": status,
        "tradable_with_controls": tradable,
        "warn_count": warn_count,
        "fail_count": fail_count,
        "conclusion": "不是明显逐股过拟合，但存在收益集中、小样本条件策略、实盘复现三类风险；可作为真实交易策略的前提是严格执行源新鲜度、30m确认、账户风控和纸面跟踪验收。",
    }


def _write_report(
    summary: dict[str, Any],
    strategy: pd.DataFrame,
    windows: pd.DataFrame,
    window_strategy: pd.DataFrame,
    concentration_meta: dict[str, Any],
    top_trades: pd.DataFrame,
    checks: pd.DataFrame,
) -> None:
    lines = [
        "# G3 五策略正式调度合同过拟合审计",
        "",
        "## 结论",
        "",
        summary["conclusion"],
        "",
        f"历史正式收益 {_pct(summary['overall']['total_return'])}，最大回撤 {_pct(summary['overall']['max_drawdown'])}，成交 {summary['overall']['trade_count']} 笔。审计结论为 `{summary['status']}`，真实交易可用性为 `{summary['tradable_with_controls']}`。",
        "",
        "这套合同不是明显的逐股历史挑选过拟合，因为策略取舍落在未来可见的市场状态、原生信号和二槽规则上；但它也不是可以直接无保护实盘的“无风险高收益模型”。必须承认三类风险：收益集中在核心引擎、强势突破/恐慌修复样本偏少、历史原生 30m 语义必须被当前实盘链路完整复现。",
        "",
        "## 五策略历史表现",
        "",
        _md_table(
            strategy,
            {"win_rate", "avg_trade_return", "worst_trade", "best_trade", "pnl_share", "top1_pnl_share_within_strategy"},
            {"sum_pnl"},
            max_rows=10,
        ),
        "",
        "## 过拟合检查",
        "",
        _md_table(checks, set(), set(), max_rows=20),
        "",
        "## 分窗口正式表现",
        "",
        _md_table(windows, {"return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade"}, set(), max_rows=20),
        "",
        "## 策略分窗口贡献",
        "",
        _md_table(
            window_strategy,
            {"return_on_initial_equity", "win_rate", "avg_trade_return", "worst_trade"},
            {"sum_pnl"},
            max_rows=40,
        ),
        "",
        "## 收益集中度",
        "",
        f"- Top1 贡献占比：{_pct(concentration_meta.get('top1_pnl_share'))}",
        f"- Top3 贡献占比：{_pct(concentration_meta.get('top3_pnl_share'))}",
        f"- Top5 贡献占比：{_pct(concentration_meta.get('top5_pnl_share'))}",
        f"- Top10 贡献占比：{_pct(concentration_meta.get('top10_pnl_share'))}",
        "",
        _md_table(top_trades, {"net_ret"}, {"realized_pnl"}, max_rows=20),
        "",
        "## 真实交易前必须满足",
        "",
        "1. 当前候选生成器必须能复现历史原生信号，不允许用日线代理替代。",
        "2. 30m确认、分批止盈、前低保护和硬止损必须在实盘/纸面盘一致执行。",
        "3. 强势突破和恐慌出清修复保留为条件启用策略，不继续细调到逐股或过细参数。",
        "4. Score120 是收益主引擎，允许收益集中，但必须有高热度降仓、主线退潮观察和真实账户仓位约束。",
        "5. 实盘前至少做 30 个交易日影子/纸面跟踪，比较候选、入场、卖出、滑点和资金约束差异。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = _read_trades()
    overall = _overall_metrics(trades)
    strategy = _strategy_metrics(trades)
    windows = _overall_window_metrics()
    window_strategy = _window_strategy_metrics(trades)
    concentration_meta, top_trades = _concentration_metrics(trades)
    checks = _overfit_checks(overall, strategy, concentration_meta, windows)
    verdict = _verdict(checks)
    summary = {
        "version": "g3_formal_five_strategy_overfit_audit_v1",
        **verdict,
        "overall": overall,
        "concentration": concentration_meta,
    }
    strategy.to_csv(OUT_DIR / "strategy_overfit_metrics.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_summary.csv", index=False, encoding="utf-8-sig")
    window_strategy.to_csv(OUT_DIR / "window_strategy_metrics.csv", index=False, encoding="utf-8-sig")
    top_trades.to_csv(OUT_DIR / "top_contribution_trades.csv", index=False, encoding="utf-8-sig")
    checks.to_csv(OUT_DIR / "overfit_checks.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _write_report(summary, strategy, windows, window_strategy, concentration_meta, top_trades, checks)
    print(json.dumps({"output_dir": str(OUT_DIR), **summary}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
