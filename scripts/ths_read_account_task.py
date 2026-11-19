# -*- coding: utf-8 -*-
"""同花顺远航单账号只读验收按需任务包装（#22：仅调用 read-account --with-fills，绝不切号/绝不下单）。

用于在 Windows 交互式计划任务（\AiStock_THSReadAccount，运行于 WinSta0\Default）中执行只读采集，
并将 JSON 报告原子落盘到 runtime/ths_read_account/latest.json 供外部验收读取。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import ths_client as tc  # noqa: E402

BJ_TZ = timezone(timedelta(hours=8))
DEFAULT_HAPP_EXE = Path(r"D:\同花顺远航\同花顺远航版\bin\happ.exe")
DEFAULT_OUT_DIR = Path(r"F:\Aistock\data\data\runtime\ths_read_account")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同花顺远航单账号只读验收包装脚本（绝不切号/绝不下单）")
    parser.add_argument("--account", default="acct1", help="目标单账号（默认 acct1）")
    parser.add_argument("--happ-exe", type=Path, default=DEFAULT_HAPP_EXE, help="远航版主程序路径")
    parser.add_argument("--with-fills", action="store_true", default=True, help="同时只读历史成交（默认开启）")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="报告输出目录")
    args = parser.parse_args(argv)

    report = tc.run_read_account(
        account=args.account,
        happ_exe=args.happ_exe,
        allow_switch=False,
        with_fills=bool(args.with_fills),
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BJ_TZ).strftime("%Y%m%d_%H%M%S")
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"

    dated = args.out_dir / f"read_account_{args.account}_{stamp}.json"
    dated.write_text(payload, encoding="utf-8")

    latest = args.out_dir / "latest.json"
    tmp = latest.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, latest)
    return int(report.get("rc", 1))


if __name__ == "__main__":
    raise SystemExit(main())
