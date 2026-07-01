from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_g3_five_strategies_from_scratch_v1 import _md_table, _money, _pct  # noqa: E402
from utils.paths import report_path, runtime_path  # noqa: E402


OUT_DIR = report_path("g3_strategy_fusion_guardrails_v1")
NATIVE_BRIDGE_DIR = report_path("g3_five_strategies_native_bridge_v1")
DAILY_PROXY_DIR = report_path("g3_five_strategies_from_scratch_v1")
BRIDGE_RECALL_DIR = report_path("g3_native_source_bridge_recall_v1")
DAILY_RECALL_DIR = report_path("g3_profitable_signal_recall_v1")
NATIVE_M30_DIR = report_path("g3_native_bridge_native_m30_semantics_v1")
STATE_ALPHA_SUMMARY_PATH = runtime_path("gen3_state_alpha", "latest_summary.json")
STATE_ROUTER_SUMMARY_PATH = runtime_path("gen3_state_router_shadow", "latest_summary.json")
FORMAL_GATE_FILES = [
    ROOT / "scripts" / "gen3_institutional_mainwave_current_v1.py",
    ROOT / "scripts" / "gen3_state_router_shadow_daily_v1.py",
    ROOT / "scripts" / "gen3_hard_gate_daily_closure_audit_v1.py",
    ROOT / "api" / "gen3_state_alpha.py",
]

REQUIRED_STRATEGIES = {
    "institutional_score120_mainwave": "机构主升Score120",
    "old_g3_strong_breakout": "强势突破",
    "panic_capitulation_repair": "恐慌出清修复",
    "range_weak_repair": "震荡弱势修复",
    "volume_runup_supplement": "量能续强补位",
}

KEY_NATIVE_SOURCE_MIN_RECALL = {
    "Score120主升+主线扩散+30m原生源": 0.99,
    "G2量能续强/Alpha191原生源": 0.99,
    "旧G3强势突破原生源": 0.99,
    "旧G3恐慌修复原生源": 0.99,
    "旧G3弱势/震荡修复原生源": 0.90,
}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, low_memory=False)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _num(value: Any, default: float = 0.0) -> float:
    out = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return float(out) if pd.notna(out) else default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "ok", "pass"}
    return False


def _latest_current_summary() -> dict[str, Any]:
    candidates = [_read_json(STATE_ALPHA_SUMMARY_PATH), _read_json(STATE_ROUTER_SUMMARY_PATH)]
    for item in candidates:
        source = _state_source(item, "institutional_mainwave_current_builder_v1")
        if source.get("mainwave_dynamic_cooldown"):
            return item
    for item in candidates:
        if _state_source(item, "institutional_mainwave_current_builder_v1"):
            return item
    return candidates[0] or candidates[1]


def _state_source(summary: dict[str, Any], source_name: str) -> dict[str, Any]:
    for item in summary.get("sources") or []:
        if isinstance(item, dict) and item.get("source") == source_name:
            return item
    return {}


def _check(
    rows: list[dict[str, Any]],
    key: str,
    title: str,
    ok: bool,
    severity: str,
    evidence: str,
    action: str = "",
    metric: Any = None,
    threshold: Any = None,
) -> None:
    rows.append(
        {
            "key": key,
            "title": title,
            "ok": bool(ok),
            "status": "pass" if ok else severity,
            "severity": severity,
            "metric": metric,
            "threshold": threshold,
            "evidence": evidence,
            "action": action,
        }
    )


def _build_guardrails() -> tuple[pd.DataFrame, dict[str, Any], pd.DataFrame]:
    checks: list[dict[str, Any]] = []
    native_summary = _read_json(NATIVE_BRIDGE_DIR / "summary.json")
    daily_summary = _read_json(DAILY_PROXY_DIR / "summary.json")
    native_metrics = _read_csv(NATIVE_BRIDGE_DIR / "strategy_metrics.csv")
    bridge_by_strategy = _read_csv(BRIDGE_RECALL_DIR / "native_bridge_recall_by_strategy.csv")
    bridge_by_source = _read_csv(BRIDGE_RECALL_DIR / "native_bridge_recall_by_source.csv")
    daily_by_strategy = _read_csv(DAILY_RECALL_DIR / "profitable_recall_by_strategy.csv")
    native_m30 = _read_json(NATIVE_M30_DIR / "summary.json")
    current_summary = _latest_current_summary()

    native_total_return = _num(native_summary.get("total_return"))
    native_max_drawdown = _num(native_summary.get("max_drawdown"))
    native_closed = int(_num(native_summary.get("closed_trades")))
    include_range_v3 = _bool(native_summary.get("include_range_v3"))
    daily_total_return = _num(daily_summary.get("total_return"))

    _check(
        checks,
        "native_bridge_total_return_positive",
        "原生桥接五策略总体收益为正",
        native_total_return > 0,
        "fail",
        f"native_bridge total_return={_pct(native_total_return)}, closed_trades={native_closed}",
        "若失败，不能宣布策略融合已还原收益；先恢复原生买点源再谈减少策略。",
        native_total_return,
        "> 0",
    )
    _check(
        checks,
        "native_bridge_has_enough_trades",
        "原生桥接样本数足够",
        native_closed >= 100,
        "fail",
        f"native_bridge closed_trades={native_closed}",
        "样本过少时收益不具备策略级验收意义。",
        native_closed,
        ">= 100",
    )
    _check(
        checks,
        "daily_proxy_not_accepted_as_replacement",
        "日线代理不能作为最终替代",
        daily_total_return < 0 and native_total_return > 0,
        "warn",
        f"daily_proxy total_return={_pct(daily_total_return)}; native_bridge total_return={_pct(native_total_return)}",
        "保留该差异作为防误用证据：统一策略名可以，不能用日线代理替代原生生成器。",
        daily_total_return,
        "daily_proxy < 0 and native_bridge > 0",
    )
    _check(
        checks,
        "range_v3_excluded_from_default",
        "Range V3 暂不进入默认正式合同",
        not include_range_v3,
        "warn",
        f"include_range_v3={include_range_v3}",
        "Range V3弱势低吸继续作为研究源，不能混入默认五策略收益还原口径。",
        include_range_v3,
        "False",
    )

    present = set(native_metrics.get("trade_strategy", pd.Series(dtype=str)).dropna().astype(str))
    for strategy, label in REQUIRED_STRATEGIES.items():
        row = native_metrics[native_metrics.get("trade_strategy", pd.Series(dtype=str)).astype(str).eq(strategy)] if not native_metrics.empty else pd.DataFrame()
        trade_count = int(_num(row["trade_count"].iloc[0])) if not row.empty else 0
        _check(
            checks,
            f"strategy_present_{strategy}",
            f"{label} 策略主体存在且有成交",
            strategy in present and trade_count > 0,
            "fail",
            f"trade_count={trade_count}",
            "五策略减少后的正式主体不能缺项；缺一项就可能跑丢历史盈利来源。",
            trade_count,
            "> 0",
        )

    if not bridge_by_strategy.empty:
        for _, row in bridge_by_strategy.iterrows():
            strategy = str(row.get("trade_strategy") or "")
            label = str(row.get("trade_strategy_label") or strategy)
            rate = _num(row.get("candidate_within_3_trade_days_rate"))
            threshold = 0.70 if strategy == "range_weak_repair" else 0.99
            _check(
                checks,
                f"strategy_profitable_recall_{strategy}",
                f"{label} 盈利样本原生桥接召回",
                rate >= threshold,
                "fail",
                f"candidate_3d_recall={_pct(rate)}, profitable_trades={int(_num(row.get('profitable_trades')))}",
                "若失败，说明统一策略主体下仍有原生赚钱买点没有挂回。",
                rate,
                f">= {_pct(threshold)}",
            )

    if not bridge_by_source.empty:
        for source_label, threshold in KEY_NATIVE_SOURCE_MIN_RECALL.items():
            row = bridge_by_source[
                bridge_by_source.get("candidate_nearest_source_strategy_label", pd.Series(dtype=str)).fillna("").astype(str).eq(source_label)
            ]
            rate = _num(row["candidate_within_3_trade_days_rate"].iloc[0]) if not row.empty else 0.0
            trades = int(_num(row["profitable_trades"].iloc[0])) if not row.empty else 0
            _check(
                checks,
                f"native_source_recall_{source_label}",
                f"{source_label} 不能被融合跑丢",
                rate >= threshold and trades > 0,
                "fail",
                f"candidate_3d_recall={_pct(rate)}, profitable_trades={trades}",
                "关键原生源召回低于阈值时，禁止把对应策略源替换为统一代理规则。",
                rate,
                f">= {_pct(threshold)}",
            )

    native_m30_rate = _num(native_m30.get("native_m30_confirmed_rate"))
    proxy_m30_rate = _num(native_m30.get("proxy_m30_confirmed_rate"))
    proxy_kill_count = int(_num(native_m30.get("proxy_kill_count")))
    _check(
        checks,
        "native_m30_semantics_preserved",
        "原生 30m 确认语义保留",
        native_m30_rate >= 0.99,
        "fail",
        f"native_m30_confirmed_rate={_pct(native_m30_rate)}",
        "原生 30m 确认不足时，不能进入实盘级融合验收。",
        native_m30_rate,
        ">= 99%",
    )
    _check(
        checks,
        "proxy_m30_not_hard_gate",
        "代理 30m 不作为默认硬 gate",
        proxy_m30_rate < native_m30_rate and proxy_kill_count > 0,
        "warn",
        f"proxy_m30_confirmed_rate={_pct(proxy_m30_rate)}, proxy_kill_count={proxy_kill_count}",
        "代理 30m 可作为研究过滤，但默认合同必须使用原生 30m 语义，避免误杀原生赢家。",
        proxy_m30_rate,
        "< native_m30_rate",
    )

    inst_source = _state_source(current_summary, "institutional_mainwave_current_builder_v1")
    heat_state = str(inst_source.get("index_mom60_heat_state") or "")
    max_index_mom60 = _num(inst_source.get("max_index_mom60"), default=0.0)
    inst_rows = int(_num(inst_source.get("rows")))
    cooldown = inst_source.get("mainwave_dynamic_cooldown") if isinstance(inst_source.get("mainwave_dynamic_cooldown"), dict) else {}
    cooldown_policy = str(cooldown.get("policy") or "")
    cooldown_active = bool(cooldown.get("cooldown_active", False))
    _check(
        checks,
        "current_score120_respects_5pct_strategy_gate",
        "当前 Score120 按原策略执行 index_mom60<=5% 硬准入",
        max_index_mom60 <= 0.05 or inst_rows == 0,
        "fail",
        f"rows={inst_rows}, max_index_mom60={_pct(max_index_mom60)}, heat_state={heat_state or '--'}",
        "必须执行 institutional_mainwave index_mom60<=5% 才进入影子盘/买入候选的新合同。",
        max_index_mom60,
        "<= 5% strategy gate",
    )
    _check(
        checks,
        "current_score120_dynamic_cooldown_contract_visible",
        "当前 Score120 暴露机构主升动态冷却合同",
        cooldown_policy == "institutional_mainwave_consecutive_loss_dynamic_recovery",
        "fail",
        f"policy={cooldown_policy or '--'}, active={cooldown_active}",
        "必须把连续2笔已平仓亏损后的动态冷却作为 institutional_mainwave 的正式准入合同暴露给影子盘、API 和页面。",
        cooldown_policy,
        "institutional_mainwave_consecutive_loss_dynamic_recovery",
    )
    _check(
        checks,
        "current_score120_dynamic_cooldown_blocks_rows",
        "机构主升动态冷却触发时不出票",
        (not cooldown_active) or inst_rows == 0,
        "fail",
        f"rows={inst_rows}, active={cooldown_active}, reason={cooldown.get('cooldown_reason', '--')}",
        "动态冷却暂停期只能观察，不允许进入影子盘/买入候选。",
        inst_rows,
        "0 when cooldown_active",
    )

    hard_stop_cooldown_hits = []
    for path in FORMAL_GATE_FILES:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "hard_stop_cooldown_pause_new_buy" in text or "hard_stop_30m_count>=2" in text:
            hard_stop_cooldown_hits.append(str(path))
    _check(
        checks,
        "formal_gate_no_hard_stop_cooldown_pause",
        "正式链路不再使用硬止损次数冷却暂停新买",
        not hard_stop_cooldown_hits,
        "fail",
        f"hits={hard_stop_cooldown_hits or '--'}",
        "机构主升新买冷却必须使用连续2笔已平仓亏损后的动态恢复合同，硬止损次数只能作为观察指标。",
        len(hard_stop_cooldown_hits),
        "0",
    )

    g2_source = _state_source(current_summary, "g2_gap_supplement_current_builder_v1")
    g2_has_diagnostic = bool(g2_source)
    g2_fresh = _bool(g2_source.get("fresh_for_entry_date")) if g2_source else False
    _check(
        checks,
        "g2_supplement_freshness_visible",
        "G2 补位源新鲜度显式可见",
        g2_has_diagnostic and ("fresh_for_entry_date" in g2_source or "source_freshness" in g2_source),
        "warn",
        f"fresh_for_entry_date={g2_fresh}, latest_live_update_date={g2_source.get('latest_live_update_date', '--') if g2_source else '--'}",
        "G2 无候选时必须先区分源过期还是策略误杀。",
        g2_fresh,
        "diagnostic present",
    )
    _check(
        checks,
        "current_g2_supplement_source_fresh",
        "当前 G2 补位源覆盖请求交易日",
        g2_fresh,
        "warn",
        f"requested_entry_date={g2_source.get('requested_entry_date', current_summary.get('entry_date', '--')) if g2_source else '--'}, "
        f"latest_live_update_date={g2_source.get('latest_live_update_date', '--') if g2_source else '--'}, "
        f"stale_reason={g2_source.get('stale_reason', '--') if g2_source else '--'}",
        "这是当前交易可用性风险，不代表五策略融合失败；补齐 G2 上游源后再判断当天是否应有补位票。",
        g2_fresh,
        "True for same entry_date",
    )

    checks_df = pd.DataFrame(checks)
    hard_fail_count = int((checks_df["status"] == "fail").sum()) if not checks_df.empty else 0
    warn_count = int((checks_df["status"] == "warn").sum()) if not checks_df.empty else 0
    pass_count = int((checks_df["status"] == "pass").sum()) if not checks_df.empty else 0

    recall_compare = _recall_compare(daily_by_strategy, bridge_by_strategy)
    summary = {
        "ok": hard_fail_count == 0,
        "status": "pass" if hard_fail_count == 0 else "fail",
        "mode": "g3_strategy_fusion_guardrails_v1",
        "principle": "减少到5个交易策略主体可以，但原生买点生成器、原生30m确认语义和源新鲜度诊断不能丢。",
        "pass_count": pass_count,
        "warn_count": warn_count,
        "hard_fail_count": hard_fail_count,
        "required_strategy_count": len(REQUIRED_STRATEGIES),
        "native_bridge": {
            "total_return": native_total_return,
            "max_drawdown": native_max_drawdown,
            "closed_trades": native_closed,
            "include_range_v3": include_range_v3,
        },
        "daily_proxy": {
            "total_return": daily_total_return,
            "closed_trades": int(_num(daily_summary.get("closed_trades"))),
        },
        "native_m30": {
            "native_m30_confirmed_rate": native_m30_rate,
            "proxy_m30_confirmed_rate": proxy_m30_rate,
            "proxy_kill_count": proxy_kill_count,
        },
        "current_sources": {
            "institutional_rows": inst_rows,
            "institutional_max_index_mom60": max_index_mom60,
            "institutional_mainwave_dynamic_cooldown": cooldown,
            "g2_fresh_for_entry_date": g2_fresh,
            "g2_latest_live_update_date": g2_source.get("latest_live_update_date") if g2_source else None,
            "g2_stale_reason": g2_source.get("stale_reason") if g2_source else None,
        },
        "artifacts": {
            "checks": str(OUT_DIR / "guardrail_checks.csv"),
            "summary": str(OUT_DIR / "summary.json"),
            "report": str(OUT_DIR / "REPORT_CN.md"),
        },
    }
    return checks_df, summary, recall_compare


def _recall_compare(daily: pd.DataFrame, bridge: pd.DataFrame) -> pd.DataFrame:
    cols = ["trade_strategy", "trade_strategy_label", "profitable_trades", "candidate_within_3_trade_days_rate"]
    if daily.empty or bridge.empty:
        return pd.DataFrame()
    d = daily[cols].rename(columns={"candidate_within_3_trade_days_rate": "daily_proxy_candidate_3d_recall"})
    b = bridge[cols].rename(columns={"candidate_within_3_trade_days_rate": "native_bridge_candidate_3d_recall"})
    out = d.merge(b, on=["trade_strategy", "trade_strategy_label", "profitable_trades"], how="outer")
    out["recall_lift"] = pd.to_numeric(out["native_bridge_candidate_3d_recall"], errors="coerce").fillna(0) - pd.to_numeric(
        out["daily_proxy_candidate_3d_recall"], errors="coerce"
    ).fillna(0)
    return out.sort_values("recall_lift", ascending=False)


def _write_report(checks: pd.DataFrame, summary: dict[str, Any], recall_compare: pd.DataFrame) -> None:
    failed = checks[checks["status"].eq("fail")].copy() if not checks.empty else pd.DataFrame()
    warned = checks[checks["status"].eq("warn")].copy() if not checks.empty else pd.DataFrame()
    lines = [
        "# G3 策略融合防跑丢验收",
        "",
        "## 总结",
        "",
        f"- 状态：{'通过' if summary['ok'] else '失败'}",
        f"- 硬失败：{summary['hard_fail_count']}，风险提示：{summary['warn_count']}，通过项：{summary['pass_count']}",
        f"- 原生桥接收益：{_pct(summary['native_bridge']['total_return'])}，最大回撤：{_pct(summary['native_bridge']['max_drawdown'])}，交易：{summary['native_bridge']['closed_trades']}",
        f"- 日线代理收益：{_pct(summary['daily_proxy']['total_return'])}，证明不能用代理规则替代原生源。",
        "",
        "## 硬失败",
        "",
        _md_table(failed, set(), set(), max_rows=30) if not failed.empty else "无。",
        "",
        "## 风险提示",
        "",
        _md_table(warned, set(), set(), max_rows=30) if not warned.empty else "无。",
        "",
        "## 召回提升",
        "",
        _md_table(
            recall_compare,
            {"daily_proxy_candidate_3d_recall", "native_bridge_candidate_3d_recall", "recall_lift"},
            set(),
            max_rows=10,
        )
        if not recall_compare.empty
        else "暂无召回对比。",
        "",
        "## 当前交易结论",
        "",
        "1. 可以保留 5 个统一交易策略主体，但不允许用日线代理规则替代原生买点生成器。",
        "2. Score120、G2量能续强、旧G3强势突破、旧G3恐慌修复、旧G3弱势/震荡修复是必须保留的原生赚钱源。",
        "3. Range V3弱势低吸暂列研究源；进入默认合同前需要单独证明在统一卖出合同下不会扩大回撤。",
        "4. 30m 默认使用原生确认语义；代理30m只可作为研究过滤，不作为正式硬 gate。",
        "5. G2补位当天无票时，必须先检查源新鲜度，再判断是否策略融合误杀。",
        "",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    checks, summary, recall_compare = _build_guardrails()
    checks.to_csv(OUT_DIR / "guardrail_checks.csv", index=False, encoding="utf-8-sig")
    recall_compare.to_csv(OUT_DIR / "recall_compare.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(checks, summary, recall_compare)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
