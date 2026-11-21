# -*- coding: utf-8 -*-
"""AiStock AI 策略挖掘工坊 CLI 入口。

示例：
  F:\\python3.10\\python.exe F:\\Aistock\\code\\scripts\\run_strategy_mining.py --template mainwave_offense
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.mining import MINING_TEMPLATES  # noqa: E402
from services.mining.contract_exporter import export_candidate_contract  # noqa: E402
from services.mining.rule_miner import run_mining_experiment  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="AiStock AI 策略挖掘工坊 CLI")
    parser.add_argument(
        "--template",
        default="mainwave_offense",
        choices=list(MINING_TEMPLATES.keys()),
        help="挖掘模板 ID",
    )
    parser.add_argument("--start-date", default="2023-01-01", help="回测起始日期 YYYY-MM-DD")
    parser.add_argument("--min-amt-wan", type=float, default=5000.0, help="20日均成交额门槛(万元)")
    parser.add_argument("--rebuild-cache", action="store_true", help="强制重建特征宽表缓存")
    parser.add_argument("--export-passed", action="store_true", help="将全部通过5道硬门禁的策略导出为影子合约")
    args = parser.parse_args()

    print(f"=== 启动 AiStock AI 策略挖掘工坊：{args.template} ===")
    report = run_mining_experiment(
        template_id=args.template,
        start_date=args.start_date,
        min_amt_20d=args.min_amt_wan * 1e4,
        force_rebuild_cache=args.rebuild_cache,
    )

    u = report["universe"]
    sp = report["splits"]
    print(
        f"样本池：{u['unique_stocks']} 只流动性达标股（≥{int(u['min_amt_20d_wan'])}万），"
        f"{u['trade_days']} 交易日，共 {u['panel_rows']:,} 行"
    )
    print(
        f"三段切分：IS[{sp['is']['from']}~{sp['is']['to']}] | "
        f"Val[{sp['val']['from']}~{sp['val']['to']}] | "
        f"OOS盲测[{sp['oos']['from']}~{sp['oos']['to']}]"
    )
    print(
        f"评估结果：共 {report['summary']['candidates_evaluated']} 条候选，"
        f"通过全部 5 道硬门禁：{report['summary']['passed_all_gates']} 条"
    )
    print("-" * 110)

    for rank, item in enumerate(report["results"], start=1):
        r = item["rule"]
        s_is = item["segments"]["is"]
        s_val = item["segments"]["val"]
        s_oos = item["segments"]["oos"]
        g = item["quality_gates"]
        badge = "PASS 5/5" if g["all_passed"] else f"FAIL {g['passed_count']}/5"
        print(
            f"#{rank:02d} [{badge}] 分={g['mining_score']:5.1f} | {r['rule_id']} ({r['source']}) | "
            f"出场:持{r['exit_policy']['hold_days']}d/止损{int(r['exit_policy']['stop_loss_pct']*100)}%/止盈{int(r['exit_policy']['take_profit_pct']*100)}%"
        )
        print(
            f"    IS : {s_is['trades_count']:4d}笔 胜率{s_is['win_rate_pct']:5.1f}% 均{s_is['avg_ret_pct']:+6.2f}% 累计{s_is['cum_ret_pct']:+6.1f}% 回撤{s_is['max_drawdown_pct']:4.1f}%"
        )
        print(
            f"    Val: {s_val['trades_count']:4d}笔 胜率{s_val['win_rate_pct']:5.1f}% 均{s_val['avg_ret_pct']:+6.2f}% 累计{s_val['cum_ret_pct']:+6.1f}% 回撤{s_val['max_drawdown_pct']:4.1f}%"
        )
        print(
            f"    OOS: {s_oos['trades_count']:4d}笔 胜率{s_oos['win_rate_pct']:5.1f}% 均{s_oos['avg_ret_pct']:+6.2f}% 累计{s_oos['cum_ret_pct']:+6.1f}% "
            f"(超额{s_oos['excess_ret_pct']:+6.1f}%) 回撤{s_oos['max_drawdown_pct']:4.1f}% 夏普{s_oos['sharpe']:4.2f} 衰减比{g['decay_ratio']:.2f}"
        )
        print(f"    逻辑: {r['thesis']}")
        if not g["all_passed"]:
            print(f"    淘汰归因: {'；'.join(g['failure_reasons'])}")
        elif args.export_passed:
            exp = export_candidate_contract(item, run_id=report["run_id"])
            print(f"    >>> 已导出影子合约: {exp['path']}")

    print("-" * 110)
    print(f"完整实验报告已落盘：{report['artifact_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
