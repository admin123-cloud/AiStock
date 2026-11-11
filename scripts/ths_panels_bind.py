# -*- coding: utf-8 -*-
"""R49 面板→账号绑定 CLI（幂等）：ths_panels.json 注册身份令牌 + 热切换键。

用法（host，F:/python/python3.10）：
  python scripts/ths_panels_bind.py --account acct2 \
      --identity-hint 王 --hotkey 2 --broker 广发证券
  python scripts/ths_panels_bind.py --account acct1 \
      --identity-hint 曾 --hotkey 1 --broker 广发证券 --home

幂等：同账号重复 bind 覆盖单条规则（不堆积）；--home 写热切换常态位
（读毕切回该账号）。绑定后 --show 输出当前注册表与 live 面板解析结果。
零窗口交互（只读枚举），绝不触任何交易界面。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.ths_account_bridge import load_panel_registry  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R49 面板账号绑定（幂等）")
    parser.add_argument("--account", required=True, choices=("acct1", "acct2"))
    parser.add_argument("--identity-hint", required=True,
                        help="顶栏「券商-姓名」OCR 期望子串（如 曾 / 王）")
    parser.add_argument("--hotkey", help="热切换键号（Alt+N 的 N，如 1/2）")
    parser.add_argument("--broker", default="", help="券商提示（如 广发证券）")
    parser.add_argument("--process", default="xiadan.exe")
    parser.add_argument("--title-contains", default="网上股票交易系统")
    parser.add_argument("--home", action="store_true",
                        help="同时把该账号设为热切换常态位（读毕切回它）")
    parser.add_argument("--show", action="store_true", help="只读展示注册表与解析")
    args = parser.parse_args(argv)

    registry = load_panel_registry()

    if not args.show:
        registry.bind(
            rule={"process": args.process, "title_contains": args.title_contains},
            account=args.account, broker_hint=args.broker,
            identity_hint=args.identity_hint, hotkey=args.hotkey or "")
        if args.home:
            registry.set_hotswitch(home_account=args.account)

    data = json.loads(registry.path.read_text(encoding="utf-8"))
    out: dict = {"registry_path": str(registry.path), "rules": data.get("rules"),
                 "hotswitch": data.get("hotswitch"),
                 "home_account": registry.home_account()}
    try:
        from services.ths_account_bridge import enumerate_trade_panels
        live = enumerate_trade_panels(include_minimized=True)
        resolved = registry.resolve(live)
        out["live_panels"] = live
        out["resolution"] = {
            "by_account": {a: p["pid"] for a, p in resolved.by_account.items()},
            "unbound": [p["pid"] for p in resolved.unbound],
            "ambiguous_accounts": resolved.ambiguous_accounts,
            "hotswitch_groups": {str(k): v for k, v
                                 in (resolved.hotswitch_groups or {}).items()},
        }
    except Exception as exc:  # noqa: BLE001 - 非 host 环境无面板枚举
        out["live_panels_error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
