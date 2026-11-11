# -*- coding: utf-8 -*-
"""R38c/R47 fills→broker_state 每日自动同步 CLI（引擎 services/broker_state_autosync）。

用法：
  python scripts/broker_state_sync.py --sync [--date 2026-11-11]
                                       [--init-base init_base.json]
                                       [--account acct2]
                                       [--check] [--now "2026-11-11 15:15:00"]
  --sync         默认：多账号循环——acct1 读 fills/YYYYMMDD.json（legacy），
                 其他账号读 fills/YYYYMMDD_acctN.json；逐账号幂等重算并更新
                 broker_state.json（accounts 包裹 + acct1 顶层镜像 legacy 兼容）
  --account      只跑指定账号（默认 acct1+acct2 循环）
  --init-base    首次接入：提供锚点（期初持仓/现金，JSON），随首次有效同步落盘
  --check        干跑：只出对照报告，不写 broker_state
  --out-dir      覆盖 t_tracker 根目录（测试用）
  --state-file   覆盖 broker_state.json 路径（测试用）

R47 账号跳过语义（fail-closed）：
  * acct1 恒尝试（缺 fills → 拒绝 rc=1，legacy 语义）；
  * 其他账号=fills 在场或已有 accounts.<acctN> 历史切片才尝试；
    两者皆无 → not_configured 跳过（当前 acct2 未接入实况，非失败）；
    已接入账号缺/unavailable fills → 拒绝 + 告警（绝不静默跳过）。

退出码：0=全部 write/noop/check/skip；1=任一账号 refused（零改动）。零交易、零 formal 表写。
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

KNOWN_ACCOUNTS = ("acct1", "acct2")


def _parse_date(text: Optional[str]) -> date:
    if not text:
        return datetime.now(BEIJING_TZ).date()
    return date.fromisoformat(text)


def _parse_now(text: Optional[str]) -> datetime:
    if not text:
        return datetime.now(BEIJING_TZ).replace(tzinfo=None)
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


def _fills_file_for(root: Path, stamp: str, account: str) -> Path:
    """acct1=legacy 名（回退 _acct1 副本）；其他账号=_acctN 名。"""
    if account == "acct1":
        legacy = root / "fills" / f"{stamp}.json"
        if legacy.is_file():
            return legacy
        return root / "fills" / f"{stamp}_acct1.json"
    return root / "fills" / f"{stamp}_{account}.json"


def _report_paths(root: Path, stamp: str, account: str) -> tuple[Path, Path]:
    suffix = "" if account == "acct1" else f"_{account}"
    report_file = root / bsa.REPORT_DIRNAME / f"{stamp}{suffix}.json"
    history_file = root / bsa.REPORT_DIRNAME / "history.jsonl"
    return report_file, history_file


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="R38c/R47 broker_state 多账号成交同步 CLI")
    parser.add_argument("--sync", action="store_true", default=True,
                        help="执行同步（默认动作）")
    parser.add_argument("--date", help="YYYY-MM-DD（默认今天，北京时）")
    parser.add_argument("--now", help="覆盖当前时刻 YYYY-MM-DD HH:MM:SS（测试用）")
    parser.add_argument("--init-base", help="锚点 JSON 文件（首次接入，配合 --account）")
    parser.add_argument("--account", help="只同步指定账号（默认 acct1+acct2 循环）")
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
    alerts_file = root / bsa.ALERTS_FILENAME

    if args.account and args.account not in KNOWN_ACCOUNTS:
        print(json.dumps({"rc": 1, "error": f"unknown account: {args.account}"},
                         ensure_ascii=False))
        return 1

    if args.state_file:
        state_file = Path(args.state_file)
    else:
        from utils.paths import runtime_path
        state_file = runtime_path("gen3_state_alpha", "broker_state.json")

    init_base = None
    if args.init_base:
        init_base = json.loads(Path(args.init_base).read_text(encoding="utf-8"))

    accounts = [a for a in KNOWN_ACCOUNTS if not args.account or a == args.account]
    try:
        full_state = bsa.read_full_state(state_file, accounts[0] if accounts else "acct1")
    except bsa.SyncRefusal as refusal:
        print(json.dumps({"rc": 1, "action": "refused",
                          "reason": refusal.reason, "detail": refusal.detail},
                         ensure_ascii=False, default=str))
        return 1

    results: dict[str, dict] = {}
    rc = 0
    for account in accounts:
        fills_file = _fills_file_for(root, stamp, account)
        report_file, history_file = _report_paths(root, stamp, account)
        if not bsa.should_attempt_account(full_state=full_state,
                                          fills_file=fills_file, account=account):
            results[account] = {"rc": 0, "action": "not_configured",
                                "reason": "no fills file and no account slice"}
            continue
        # --init-base 只对显式 --account 生效（防误挂锚点到 acct1）
        account_init = init_base if args.account == account else None
        result = bsa.run_sync(
            state_file=state_file, fills_file=fills_file, alerts_file=alerts_file,
            report_file=report_file, history_file=history_file,
            for_date=for_date, now=now, init_base=account_init,
            dry_run=args.check, account=account)
        results[account] = result
        if int(result.get("rc", 1)) != 0:
            rc = 1
        try:  # 下一个账号基于最新盘面
            full_state = bsa.read_full_state(state_file, account)
        except bsa.SyncRefusal:
            pass

    payload = {"schema": "broker_state_sync_multi/1",
               "date": for_date.isoformat(), "accounts": results, "rc": rc}
    print(json.dumps(payload, ensure_ascii=False, default=str))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
