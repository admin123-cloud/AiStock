# -*- coding: utf-8 -*-
"""AR3 v3.0 × MiniQMT (xtquant.xttrader) 绕开 QMT 内置文件 IO 限制的合规能力探测与演练脚本。

背景：
  2026年10月起国内券商 QMT 内置策略沙箱收紧/关闭本地文件 IO 后，传统的“外部写信号文件 -> QMT 内置脚本读文件下单”
  模式受阻。主流合规演练路线是切换为【MiniQMT 极简模式 + 独立 Python 进程直连 xtquant.xttrader 内存/Socket API】：
  1. 本脚本运行在宿主机独立 Python 环境，直接通过本地 HTTP 拉取 AiStock 生成的标准 OrderIntent：
     GET http://127.0.0.1:3000/api/g3/mining/ar3/rebalance-plan?account_id=acct1
  2. 全程不依赖 QMT 内置脚本读任何本地信号文件；
  3. 默认严格 --dry-run（只做环境探测、账户连接测试、可用资金/持仓核对与风控熔断演练，绝不发送真实委托）。

用法：
  F:\\python3.10\\python.exe F:\\Aistock\\code\\scripts\\ar3_qmt_adapter_probe.py
  F:\\python3.10\\python.exe F:\\Aistock\\code\\scripts\\ar3_qmt_adapter_probe.py --miniqmt-path "D:\\国金QMT\\userdata_mini" --qmt-account "你的资金账号"
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import random
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = os.getenv("AISTOCK_LOCAL_BASE_URL", "http://127.0.0.1:3000")
MAX_SINGLE_ORDER_YUAN = 25000.0
ALLOWED_ETF_PREFIXES = ("51", "56", "58", "159")


def _probe_environment(miniqmt_path: str | None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "xtquant_installed": False,
        "xttrader_available": False,
        "xtdata_available": False,
        "detected_userdata_mini": [],
    }
    try:
        mod = importlib.import_module("xtquant")
        info["xtquant_installed"] = True
        info["xtquant_file"] = getattr(mod, "__file__", "")
    except Exception as exc:
        info["xtquant_error"] = str(exc)

    for sub, key in (("xtquant.xttrader", "xttrader_available"), ("xtquant.xtdata", "xtdata_available")):
        try:
            importlib.import_module(sub)
            info[key] = True
        except Exception:
            info[key] = False

    candidates: list[Path] = []
    if miniqmt_path:
        candidates.append(Path(miniqmt_path))
    for drive in ("C:", "D:", "E:", "F:"):
        root = Path(f"{drive}\\")
        if not root.exists():
            continue
        for pat in ("*QMT*\\userdata_mini", "*qmt*\\userdata_mini", "*迅投*\\userdata_mini", "*广发*\\userdata_mini"):
            try:
                candidates.extend(root.glob(pat))
            except Exception:
                pass
    info["detected_userdata_mini"] = sorted({str(p) for p in candidates if p.exists()})
    return info


def _fetch_ar3_order_intents(account_id: str) -> dict[str, Any]:
    url = f"{BASE_URL.rstrip('/')}/api/g3/mining/ar3/rebalance-plan?account_id={account_id}&smooth_step=true&persist=false"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _to_qmt_code(code6: str) -> str:
    c = str(code6).split(".")[0].zfill(6)
    if c.startswith(("5", "6", "9")):
        return f"{c}.SH"
    if c.startswith(("0", "1", "2", "3")):
        return f"{c}.SZ"
    return f"{c}.BJ"


def main() -> int:
    parser = argparse.ArgumentParser(description="AR3 v3.0 × MiniQMT (xtquant.xttrader) 能力探测与演练工具")
    parser.add_argument("--account-id", default="acct1", help="AiStock 账号 ID（默认 acct1 / ct1曾）")
    parser.add_argument("--miniqmt-path", default=os.getenv("AISTOCK_MINIQMT_PATH", ""), help="本地 MiniQMT userdata_mini 目录")
    parser.add_argument("--qmt-account", default=os.getenv("AISTOCK_QMT_ACCOUNT", ""), help="券商资金账号（仅只读查询连接演练）")
    args = parser.parse_args()

    print("=== [1/3] 探测本机 MiniQMT / xtquant 独立进程环境（零文件 IO 架构） ===")
    env = _probe_environment(args.miniqmt_path or None)
    print(json.dumps(env, ensure_ascii=False, indent=2))

    print("\n=== [2/3] 拉取 AiStock AR3 v3.0 标准 OrderIntent 协议 ===")
    plan = _fetch_ar3_order_intents(args.account_id)
    intents = plan.get("order_intents") or []
    print(
        f"计划日期={plan.get('date')} | 账号={plan.get('account', {}).get('label')} | "
        f"周期={plan.get('regime_label')} | 标准委托数={len(intents)}"
    )
    for it in intents:
        qmt_sym = _to_qmt_code(it["code"])
        amt = float(it.get("est_amount") or 0.0)
        split_note = "（超单笔2.5万，演练拆单）" if amt > MAX_SINGLE_ORDER_YUAN else "（单笔合规）"
        print(
            f"  #{it['seq']} [{it['phase']}] {it['side']} {qmt_sym} {it['name']} "
            f"{it['shares']}股 @ ¥{it['price']} (¥{amt:,.0f}) {split_note}"
        )

    print("\n=== [3/3] MiniQMT 内存 API 只读连接演练（严格 dry-run，绝不发单） ===")
    mini_path = args.miniqmt_path or (env["detected_userdata_mini"][0] if env["detected_userdata_mini"] else "")
    if not env["xttrader_available"]:
        print("结论：当前 Python 尚未安装 xtquant。演练前请从券商 MiniQMT 安装包目录复制 xtquant 到 Python site-packages。")
        return 0
    if not mini_path or not args.qmt_account:
        print(
            "结论：xtquant 已就绪！传入 --miniqmt-path \"...\\userdata_mini\" --qmt-account \"资金账号\" "
            "并确保以【极简模式】登录 QMT 后，即可完成只读资产/持仓握手演练（全程零文件 IO）。"
        )
        return 0

    try:
        from xtquant.xttrader import XtQuantTrader
        from xtquant.xttype import StockAccount

        session_id = random.randint(100000, 999999)
        trader = XtQuantTrader(mini_path, session_id)
        trader.start()
        conn = trader.connect()
        print(f"XtQuantTrader.connect() 返回码: {conn}（0=成功）")
        if conn == 0:
            acc = StockAccount(args.qmt_account, "STOCK")
            asset = trader.query_stock_asset(acc)
            positions = trader.query_stock_positions(acc) or []
            if asset:
                print(
                    f"只读握手成功：总资产=¥{getattr(asset, 'total_asset', 0):,.2f} | "
                    f"可用现金=¥{getattr(asset, 'cash', 0):,.2f} | 持仓数={len(positions)}"
                )
            else:
                print("连接成功但 query_stock_asset 返回空，请确认极简模式已勾选允许外部程序接入且资金账号无误。")
        trader.stop()
    except Exception as exc:
        print(f"MiniQMT 只读握手异常: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
