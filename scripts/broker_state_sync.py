# -*- coding: utf-8 -*-
"""R38c fills→broker_state 每日自动同步 CLI（引擎 services/broker_state_autosync）。

用法：
  python scripts/broker_state_sync.py --sync [--date 2026-11-10]
                                       [--init-base init_base.json]
                                       [--check] [--now "2026-11-10 17:30:00"]
  --sync         默认：读当日 fills 幂等重算全持仓并更新 broker_state.json
                 （fail-closed：fills 缺失/unavailable/解析失败/透卖/锚点缺失
                  → 零改动 + 告警 runtime/t_tracker/broker_state_sync_alerts.jsonl，rc=1）
  --init-base    首次接入：提供锚点（期初持仓/现金，JSON），随首次有效同步落盘
  --check        干跑：只出对照报告，不写 broker_state
  --out-dir      覆盖 t_tracker 根目录（测试用）
  --state-file   覆盖 broker_state.json 路径（测试用）

退出码：0=write/noop/check；1=refused（零改动）。零交易、零 formal 表写。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import broker_state_autosync as bsa  # noqa: E402
from services.t_trade_signal import BEIJING_TZ, tracker_dir  # noqa: E402


def _parse_date(text: Optional[str]) -> date:
    if not text:
        return datetime.now(BEIJING_TZ).date()
    return date.fromisoformat(text)


def _parse_now(text: Optional[str]) -> datetime:
    if not text:
        return datetime.now(BEIJING_TZ).replace(tzinfo=None)
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="R38c broker_state 成交同步 CLI")
    parser.add_argument("--sync", action="store_true", default=True,
                        help="执行同步（默认动作）")
    parser.add_argument("--date", help="YYYY-MM-DD（默认今天，北京时）")
    parser.add_argument("--now", help="覆盖当前时刻 YYYY-MM-DD HH:MM:SS（测试用）")
    parser.add_argument("--init-base", help="锚点 JSON 文件（首次接入）")
    parser.add_argument("--check", action="store_true",
                        help="干跑：只出报告不写 broker_state")
    parser.add_argument("--out-dir", help="覆盖 t_tracker 根目录（测试用）")
    parser.add_argument("--state-file", help="覆盖 broker_state.json 路径（测试用）")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    for_date = _parse_date(args.date)
    now = _parse_now(args.now)

    root = tracker_dir(args.out_dir)
    stamp = for_date.strftime("%Y%m%d")
    fills_file = root / "fills" / f"{stamp}.json"
    alerts_file = root / bsa.ALERTS_FILENAME
    report_file = root / bsa.REPORT_DIRNAME / f"{stamp}.json"
    history_file = root / bsa.REPORT_DIRNAME / "history.jsonl"

    if args.state_file:
        state_file = Path(args.state_file)
    else:
        from utils.paths import runtime_path
        state_file = runtime_path("gen3_state_alpha", "broker_state.json")

    init_base = None
    if args.init_base:
        init_base = json.loads(Path(args.init_base).read_text(encoding="utf-8"))

    result = bsa.run_sync(
        state_file=state_file, fills_file=fills_file, alerts_file=alerts_file,
        report_file=report_file, history_file=history_file,
        for_date=for_date, now=now, init_base=init_base, dry_run=args.check)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return int(result.get("rc", 1))


if __name__ == "__main__":
    raise SystemExit(main())
