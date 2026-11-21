# -*- coding: utf-8 -*-
"""AiStock Phase C：每周日自动策略进化脚本（特征宽表更新 + 现有影子合约前向复核 + 当期定向挖掘 + 微信战报）。

安全铁律：
1. 对 ClickHouse 严格只读；
2. 新通过 5 道防过拟合硬门禁的策略仅导出为 candidate_shadow 影子合约，绝不自动激活实盘下单。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.mining.backtest_engine import RuleSpec, evaluate_rule, split_trade_dates  # noqa: E402
from services.mining.contract_exporter import export_candidate_contract  # noqa: E402
from services.mining.feature_store import build_or_load_feature_panel  # noqa: E402
from services.mining.quality_gates import evaluate_mining_quality_gates  # noqa: E402
from services.mining.rule_miner import run_mining_experiment  # noqa: E402
from utils.common.paths import runtime_path  # noqa: E402

BJ_TZ = timezone(timedelta(hours=8))


def _fetch_current_regime() -> str:
    try:
        with urllib.request.urlopen("http://127.0.0.1:3000/api/g3/market-gate", timeout=10) as r:
            doc = json.loads(r.read().decode("utf-8"))
            return str(doc.get("regime") or "balanced")
    except Exception:
        return "balanced"


def _review_existing_shadow_contracts(panel_df, bench_df) -> list[dict[str, Any]]:
    """用最新全市场宽表复核已有 candidate_shadow 合约的 OOS 表现与衰减状态。"""
    c_dir = runtime_path("mining_contracts")
    if not c_dir.exists():
        return []
    splits = split_trade_dates(bench_df["trade_date"].astype(str).tolist())
    out: list[dict[str, Any]] = []
    for f in sorted(c_dir.glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        entry = doc.get("entry") or {}
        ex = doc.get("exit") or {}
        conds = entry.get("dsl_conditions") or []
        if not conds:
            continue
        rule = RuleSpec(
            rule_id=str(doc.get("trade_strategy") or f.stem),
            name=str(doc.get("display_name") or f.stem),
            template_id=str((doc.get("origin") or {}).get("template_id") or "custom_hypothesis"),
            target_regimes=list(entry.get("target_regimes") or []),
            entry_conditions=conds,
            exit_policy={
                "hold_days": int(ex.get("hold_days") or 5),
                "stop_loss_pct": -abs(float(ex.get("hard_stop_loss_pct") or 0.07)),
                "take_profit_pct": abs(float(ex.get("take_profit_pct") or 0.15)),
            },
            top_k_per_day=int((doc.get("portfolio") or {}).get("slots") or 3),
            rank_by=str(entry.get("rank_by") or "rs_20d_vs_hs300"),
            rank_ascending=bool(entry.get("rank_ascending", False)),
            source="shadow_review",
            thesis=str(doc.get("thesis") or ""),
        )
        ev = evaluate_rule(panel_df, bench_df, rule, splits=splits)
        gate = evaluate_mining_quality_gates(ev)
        oos = ev["segments"]["oos"]
        out.append(
            {
                "strategy_id": doc.get("strategy_id") or f.stem,
                "all_passed": gate["all_passed"],
                "passed_count": gate["passed_count"],
                "mining_score": gate["mining_score"],
                "oos_trades": oos["trades_count"],
                "oos_win_rate": oos["win_rate_pct"],
                "oos_avg_ret": oos["avg_ret_pct"],
                "oos_excess": oos["excess_ret_pct"],
                "oos_dd": oos["max_drawdown_pct"],
                "status_label": "健康保持" if gate["all_passed"] else "出现衰减/需复查",
            }
        )
    return out


def _push_wechat_summary(title: str, body: str) -> str:
    try:
        from services.trading.t_notifier import send_notification_message
        res = send_notification_message(title=title, body=body, force=True)
        return f"ok={res.get('ok')} channel={res.get('channel')} detail={res.get('detail')}"
    except Exception as exc:
        return f"failed: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description="AiStock 周度自动策略进化闭环")
    parser.add_argument("--start-date", default="2023-01-01")
    parser.add_argument("--min-amt-wan", type=float, default=5000.0)
    parser.add_argument("--no-rebuild-cache", action="store_true", help="跳过重建缓存，直接复用当日已有缓存")
    parser.add_argument("--push-wechat", action="store_true", help="将周度进化摘要推送至微信")
    args = parser.parse_args()

    now_bj = datetime.now(BJ_TZ)
    date_tag = now_bj.strftime("%Y%m%d")
    regime = _fetch_current_regime()

    # 按当前大盘周期动态选择 2 个最关键的挖掘方向
    if regime in ("defensive", "extreme_panic"):
        target_templates = ["sector_rotation", "panic_rebound"]
    elif regime in ("offensive", "extreme_offensive"):
        target_templates = ["mainwave_offense", "sector_rotation"]
    else:
        target_templates = ["sector_rotation", "sideways_swing"]

    print(f"=== AiStock 每周日策略自动进化 ({now_bj.strftime('%Y-%m-%d %H:%M')}) | 当前周期: {regime} ===")
    panel_df, bench_df = build_or_load_feature_panel(
        start_date=args.start_date,
        min_amt_20d=args.min_amt_wan * 1e4,
        force_rebuild=not args.no_rebuild_cache,
    )

    # 1. 复核已有影子合约
    shadow_reviews = _review_existing_shadow_contracts(panel_df, bench_df)

    # 2. 定向发起新一轮挖掘并自动导出通过 5/5 门禁的新候选
    run_summaries: list[dict[str, Any]] = []
    newly_exported: list[str] = []
    for tpl in target_templates:
        rep = run_mining_experiment(
            template_id=tpl,
            start_date=args.start_date,
            min_amt_20d=args.min_amt_wan * 1e4,
            force_rebuild_cache=False,
        )
        top = rep["results"][0] if rep["results"] else None
        for item in rep["results"]:
            if item["quality_gates"]["all_passed"]:
                exp = export_candidate_contract(item, run_id=rep["run_id"])
                newly_exported.append(exp["strategy_id"])
        run_summaries.append(
            {
                "template_id": tpl,
                "template_name": rep["template_name"],
                "run_id": rep["run_id"],
                "evaluated": rep["summary"]["candidates_evaluated"],
                "passed": rep["summary"]["passed_all_gates"],
                "best_id": top["rule"]["rule_id"] if top else None,
                "best_score": top["quality_gates"]["mining_score"] if top else 0.0,
                "best_oos_avg": top["segments"]["oos"]["avg_ret_pct"] if top else 0.0,
                "best_oos_excess": top["segments"]["oos"]["excess_ret_pct"] if top else 0.0,
            }
        )

    # 3. 生成 Markdown 周度进化报告
    lines = [
        f"# AiStock 周度策略进化报告（{now_bj.strftime('%Y-%m-%d')}）",
        "",
        f"- **当前大盘周期**：`{regime}`",
        f"- **全市场样本池**：`{panel_df['code'].nunique()}` 只流动性达标股（≥{int(args.min_amt_wan)}万），最新数据至 `{bench_df['trade_date'].iloc[-1]}`",
        f"- **本轮定向挖掘方向**：`{', '.join(target_templates)}`",
        "",
        "## 1. 已有影子合约前向复核",
    ]
    if not shadow_reviews:
        lines.append("- 暂无已导出影子合约。")
    else:
        for sr in shadow_reviews:
            lines.append(
                f"- `{sr['strategy_id']}`：**{sr['status_label']}**（门禁 {sr['passed_count']}/5，分 {sr['mining_score']}）· "
                f"OOS {sr['oos_trades']}笔 / 胜率 {sr['oos_win_rate']}% / 均 {sr['oos_avg_ret']:+.2f}% / 超额 {sr['oos_excess']:+.1f}% / 回撤 {sr['oos_dd']:.1f}%"
            )

    lines.append("")
    lines.append("## 2. 本轮定向挖掘汇总")
    for rs in run_summaries:
        lines.append(
            f"- **{rs['template_name']}**（`{rs['run_id']}`）：评估 {rs['evaluated']} 条，通过 5/5 门禁 **{rs['passed']}** 条；"
            f"最优 `{rs['best_id']}`（分 {rs['best_score']}，OOS 均 {rs['best_oos_avg']:+.2f}%，超额 {rs['best_oos_excess']:+.1f}%）"
        )
    if newly_exported:
        lines.append(f"- **新晋级/刷新影子合约（candidate_shadow）**：`{', '.join(sorted(set(newly_exported)))}`")

    md_text = "\n".join(lines) + "\n"
    out_md = runtime_path("mining_runs") / f"weekly_evolution_{date_tag}.md"
    out_md.write_text(md_text, encoding="utf-8")
    print(md_text)
    print(f"周度进化报告已落盘：{out_md}")

    if args.push_wechat:
        wx_res = _push_wechat_summary(
            title=f"AiStock 周度策略进化 ({now_bj.strftime('%m-%d')})",
            body="\n".join(lines[2:]),
        )
        print(f"微信推送结果：{wx_res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
