# -*- coding: utf-8 -*-
"""R42 通知通道 CLI：QMT Tick 可用性告警（holding-tick-analysis GET 净化后外呼去处）。

任务书：F:/Aistock/data/data/runtime/task_briefs/20261110_purify_holding_tick_analysis_r42.md

背景：GET /api/gen3-state-alpha/holding-tick-analysis 曾在请求内发 SMTP 告警邮件
并写告警状态/监控事件文件（R30 审计表 #14 判伪读实写封禁）。R42 将 GET 净化为
纯读，外呼与状态写移到本通道——pipeline_scheduler 常驻任务 holding_tick_qmt_alert
（工作日 09:25~15:10 每 10min）经 subprocess 调用本脚本：

  python scripts/holding_tick_qmt_alert.py --once      # 探测+按闸门告警/恢复（调度语义）
  python scripts/holding_tick_qmt_alert.py --dry-run   # 只读探测+闸门评估，零写零发

语义（_run_holding_tick_qmt_alert_once）：
  codes = broker 持仓快照 ∪ holding-tick 盯盘清单；
  探测失败 → _notify_holding_tick_qmt_unavailable（邮件+状态写+事件留痕，30min 冷却，
             交易时段/邮件开关门控不变）；
  探测成功 → _mark_holding_tick_qmt_available（仅在曾告警时发恢复邮件并回写状态）。

fail-closed：未指定 --once/--dry-run 时拒绝执行（无缺省副作用）；意外异常 rc=1。
零 formal 表写；不发送任何真实委托。
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="QMT Tick 可用性告警（R42 通知通道；GET /holding-tick-analysis 已零外呼零写）")
    parser.add_argument("--once", action="store_true",
                        help="探测+按闸门执行告警/恢复（调度通道语义：可发邮件、可写告警状态/监控事件）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只读探测+闸门评估，零写零发（部署验证/诊断用）")
    args = parser.parse_args(argv)
    if bool(args.once) == bool(args.dry_run):
        parser.error("必须且只能指定 --once 或 --dry-run 之一（无缺省副作用）")

    import api.gen3_state_alpha as g3

    result = g3._run_holding_tick_qmt_alert_once(
        source="manual" if args.dry_run else "scheduler",
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.stderr.write("holding_tick_qmt_alert: unexpected failure (fail-closed)\n")
        raise SystemExit(1)
