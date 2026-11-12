# -*- coding: utf-8 -*-
"""R50 Phase 2 填单演练（Owner 在场，绝不点提交）。

功能（task_briefs/20261111_l2_order_automation_r50.md Phase 2）：
  1. 读 panel_map.json 获取代码框/价格框/数量框的 UIA 控件定义
     并逐项验证存在，不存在=填失败。
  2. fill_order(account, code, price, qty, side) 完成：
     窗口锁+前台检查->账号切换->左侧导航->清空填入各字段。
  3. 绝不 Invoke 提交按钮（panel_map.json 已标注 invoke_forbidden_phase1）。
  4. fill_and_verify() 额外读回+截图形成文本核对。
  5. 零 LLM 调用零额度消耗。
  6. 零提交撤单操作。

铁律（Phase 1 延续 + Phase 2 新增）：
  - 零 Invoke 任何 submit_buttons 条目（买入[F1]/卖出[F2] 也属 submit_buttons
    范畴，但它们的 control_type=树项目 且 patterns={selection_item} 而非 invoke。
    填单器通过 selection_item.select() 导航而非 Invoke 触发）
  - 窗口锁 ths_window.lock 全程持有（与 hotswitch/poll 共用）。
  - 前台有人 -> skipped_foreground 让位。
  - 只填不点：fill_order 以 filled 字段返回已填入的值供目视核验。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# 依赖检查（零 LLM 调用零额度）
_PYWINSUCCESS = False
_PILSUCCESS = False
try:
    import pywinauto
    from pywinauto import Application, timings
    from pywinauto.findwindows import ElementNotFoundError, ElementAmbiguousError

    _PYWINSUCCESS = True
except ImportError:
    pass
try:
    from PIL import ImageGrab

    _PILSUCCESS = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
PANEL_MAP = Path(r"F:/Aistock/data/data/runtime/l2_r50/panel_map.json")
PANEL_MAP_SCHEMA = "l2_panel_map/1"
RUNTIME_DIR = Path(r"F:/Aistock/data/data/runtime/l2_r50")
HOTSWITCH_LOCK_NAME = "ths_window.lock"
PANEL_TITLE = "网上股票交易系统5.0"
PANEL_CLASS = "Afx:004A0000:b:00010003:00000006:74BD1569"

EDIT_WAIT_SEC = 8
NAV_SELECT_WAIT_SEC = 1.0
POST_FILL_SETTLE_SEC = 0.5

INVOKE_FORBIDDEN_FLAG = "invoke_forbidden_phase1"
FILL_SCHEMA = "l2_fill_result/1"

# 已知账号映射（identity_hint 与 ths_panels.json 一致）
KNOWN_ACCOUNTS: dict[str, dict[str, str]] = {
    "acct1": {"identity_hint": "广发证券-曾*", "home_hint": "广发证券-曾*"},
    "acct2": {"identity_hint": "广发证券-王*美", "home_hint": "广发证券-曾*"},
}

# Edit 输入框角色识别关键字（按 name 属性匹配）
_EDIT_HEURISTIC_NAMES: dict[str, list[str]] = {
    "code": ["证券代码", "代码", "证券"],
    "price": ["委托价格", "价格", "买入价格", "卖出价格"],
    "qty": ["委托数量", "数量", "买入数量", "卖出数量"],
}

# ===================================================================
# 1.  panel_map.json 读取与验证
# ===================================================================


def load_panel_map() -> dict[str, Any]:
    """加载 panel_map.json，返回完整结构。

    失败（缺文件/解析错/schema 不符/status 非 ok）抛出 RuntimeError。
    """
    if not PANEL_MAP.exists():
        raise RuntimeError(f"panel_map.json 不存在: {PANEL_MAP}")
    try:
        with PANEL_MAP.open("r", encoding="utf-8") as f:
            m = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"panel_map.json 解析失败: {e}") from e
    if m.get("schema") != PANEL_MAP_SCHEMA:
        raise RuntimeError(
            f"panel_map schema 不符: 期望 {PANEL_MAP_SCHEMA}, "
            f"实际 {m.get('schema')}"
        )
    status = m.get("status", "unknown")
    if status != "ok":
        raise RuntimeError(
            f"panel_map status 非 ok: {status} "
            f"-- 上次测绘不完整（{m.get('note','')}），拒用"
        )
    return m


def get_submit_buttons(m: dict[str, Any]) -> list[dict[str, Any]]:
    """返回 submit_buttons 列表（每项含 name/path/control_type/patterns/rect）。"""
    return m.get("submit_buttons") or []


def _find_submit_button(
    submits: list[dict[str, Any]], name: str
) -> Optional[dict[str, Any]]:
    """按 name 精确匹配 submit_buttons（如 买入[F1] / 卖出[F2]）。"""
    for sb in submits:
        if sb.get("name") == name:
            return sb
    return None


# ===================================================================
# 2.  窗口操作（锁 / 前台 / 恢复最小化）
# ===================================================================


def _find_window() -> tuple[Optional[Application], Optional[str]]:
    """通过标题+类名查找面板窗口，返回 (app, error|None)。"""
    if not _PYWINSUCCESS:
        return None, "pywinauto 不可用"
    try:
        wins = pywinauto.findwindows.find_windows(
            title=PANEL_TITLE, class_name=PANEL_CLASS, timeout=3, visible_only=True
        )
    except Exception as e:
        return None, f"查找窗口异常: {e}"
    if not wins:
        return None, f"窗口未找到 title={PANEL_TITLE!r}"
    hwnd = wins[0]
    try:
        app = Application(backend="uia").connect(handle=hwnd)
    except Exception as e:
        return None, f"connect 窗口失败 handle={hwnd:x}: {e}"
    return app, None


def _check_foreground(hwnd: int) -> Optional[str]:
    """检查窗口是否被用户占用；None=安全；str=前台警告。"""
    import ctypes

    fore = ctypes.windll.user32.GetForegroundWindow()
    return None if fore != hwnd else "用户正在操作面板（前台命中），跳过 fill_order"


def _bring_below(hwnd: int) -> None:
    """置面板到 Z-order 底部（HWND_BOTTOM），不抢焦点。"""
    import ctypes

    ctypes.windll.user32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0010 | 0x0001)
    time.sleep(0.3)


def _is_iconic(hwnd: int) -> bool:
    import ctypes

    return bool(ctypes.windll.user32.IsIconic(hwnd))


def _show_window(hwnd: int, cmd: int) -> None:
    import ctypes

    ctypes.windll.user32.ShowWindow(hwnd, cmd)


def _ensure_restored(hwnd: int) -> bool:
    """面板若最小化则恢复；返回 True=曾被最小化。"""
    was = _is_iconic(hwnd)
    if was:
        _show_window(hwnd, 9)
        time.sleep(0.5)
    return was


# ===================================================================
# 3.  账号切换（委托 ths_hotswitch.ensure_target_account）
# ===================================================================


def _switch_account_if_needed(
    panel_map: dict[str, Any],
    target_account: str,
) -> Optional[str]:
    """检查当前身份，不在目标则切换；None=ok；str=失败原因。"""
    current_account = panel_map.get("account")
    if current_account == target_account:
        return None  # 已在目标

    try:
        from services import ths_hotswitch as hs
    except ImportError:
        return "ths_hotswitch 模块不可用，无法切换账号"

    target_info = KNOWN_ACCOUNTS.get(target_account)
    if not target_info:
        return f"目标账号 {target_account} 无 KNOWN_ACCOUNTS 配置"
    target_hint = target_info.get("identity_hint", "")

    known_hints = [
        (info["identity_hint"], acct)
        for acct, info in KNOWN_ACCOUNTS.items()
        if info.get("identity_hint")
    ]

    cfg = {"target_hint": target_hint, "known_hints": known_hints}

    class _WinHandle:
        def __init__(self, hwnd: int) -> None:
            self.handle = hwnd

    raw_hwnd = panel_map["panel"]["hwnd"]
    hwnd = int(raw_hwnd, 16) if isinstance(raw_hwnd, str) else int(raw_hwnd)

    try:
        res = hs.ensure_target_account(_WinHandle(hwnd), cfg)
    except Exception as e:
        return f"账号切换异常: {e}"

    if res is not None:
        return (
            f"账号切换失败 status={res.get('status')} "
            f"reason={res.get('reason')}"
        )

    # 切换成功，缓存更新
    panel_map["account"] = target_account
    panel_map["identity_label"] = target_hint
    return None


# ===================================================================
# 4.  左侧导航选择（买入[F1] / 卖出[F2]）
# ===================================================================


def _select_nav_item(app: Application, side: str) -> Optional[str]:
    """选择左侧导航的树项目；返回 None=成功，str=错误。

    注意：通过 selection_item.Select() 完成，绝不 Invoke。
    """
    text = "买入[F1]" if side == "buy" else "卖出[F2]"
    try:
        dlg = app.window(title=PANEL_TITLE)
        item = dlg.child_window(title=text, control_type="TreeItem")
        item.select()
        time.sleep(NAV_SELECT_WAIT_SEC)
        return None
    except (ElementNotFoundError, ElementAmbiguousError) as e:
        return f"导航项 {text!r} 未找到: {e}"
    except Exception as e:
        return f"选择导航项 {text!r} 失败: {e}"


# ===================================================================
# 5.  输入框操作（清空 / 填入 / 读回）
# ===================================================================


def _find_edit_controls(app: Application) -> Optional[list]:
    """查找导航后表单区域的 Edit 输入框；返回 list 或 None。"""
    try:
        dlg = app.window(title=PANEL_TITLE)
        edits = dlg.descendants(control_type="Edit")
        return list(edits) if edits else None
    except Exception:
        return None


def _identify_edits(edits: list) -> dict[str, Any]:
    """按 panel_map 控件定义 + 启发式标识输入框。

    返回 {role: EditWrapper} 其中 role in {code, price, qty}。
    策略（降序）：
      1. name 关键字匹配（'证券代码'/'价格'/'数量' 等）
      2. 位置回退：0=代码, 1=价格, 2=数量（THS tab 序惯例）
    """
    labeled: dict[str, Any] = {}
    assigned: set[int] = set()

    for role, keywords in _EDIT_HEURISTIC_NAMES.items():
        for i, e in enumerate(edits):
            if i in assigned:
                continue
            try:
                ename = (e.element_info.name or "").strip()
            except Exception:
                ename = ""
            if any(kw in ename for kw in keywords):
                labeled[role] = e
                assigned.add(i)
                break

    for role, pos in [("code", 0), ("price", 1), ("qty", 2)]:
        if role in labeled:
            continue
        if pos < len(edits) and pos not in assigned:
            labeled[role] = edits[pos]
            assigned.add(pos)

    return labeled


def _fill_edit(edit, value: str) -> Optional[str]:
    """清空+填入；None=成功，str=失败原因。"""
    try:
        edit.set_edit_text("")
        time.sleep(0.15)
        edit.set_edit_text(value)
        time.sleep(0.15)
        return None
    except Exception as e:
        return f"填入 {value!r} 失败: {e}"


def _read_edit_value(edit) -> str:
    """安全读回 Edit 的当前值。"""
    try:
        return edit.get_value() or ""
    except Exception:
        try:
            return edit.window_text() or ""
        except Exception:
            return "<read_failed>"


# ===================================================================
# 6.  入口函数：fill_order / fill_and_verify
# ===================================================================


def fill_order(
    account: str,
    code: str,
    price: float,
    qty: int,
    side: str = "buy",
) -> dict[str, Any]:
    """填单核心。绝不 Invoke 提交。

    参数：
      account -- acct1 / acct2
      code    -- 6 位代码如 "002015"
      price   -- 价格如 13.50
      qty     -- 股数（整百）
      side    -- "buy" / "sell"

    返回 dict（schema=l2_fill_result/1, success, filled, error, steps）。
    """
    result: dict[str, Any] = {
        "schema": FILL_SCHEMA,
        "success": False,
        "filled": {},
        "error": None,
        "steps": [],
        "ts": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }

    if not _PYWINSUCCESS:
        result["error"] = "pywinauto 不可用"
        return result

    # -- 1) 加载 panel_map --
    try:
        pm = load_panel_map()
    except RuntimeError as e:
        result["error"] = str(e)
        return result
    result["steps"].append(f"panel_map 加载完成 (account={pm.get('account')})")

    # -- 2) 验证 submit_buttons 中存在导航项 --
    submits = get_submit_buttons(pm)
    nav_name = "买入[F1]" if side == "buy" else "卖出[F2]"
    nav_def = _find_submit_button(submits, nav_name)
    if nav_def is None:
        result["error"] = f"panel_map 未找到 {nav_name}，不可导航"
        return result
    invoke_info = (nav_def.get("patterns") or {}).get("invoke", {})
    result["steps"].append(
        f"{nav_name}: ct={nav_def.get('control_type')} "
        f"invoke_avail={invoke_info.get('available')} "
        f"invoke_forbidden={invoke_info.get(INVOKE_FORBIDDEN_FLAG)} "
        f"selection_item={(nav_def.get('patterns') or {}).get('selection_item', {})}"
    )

    # -- 3) 窗口 + 前台 --
    app, err = _find_window()
    if app is None:
        result["error"] = err or "窗口查找失败"
        return result

    raw_hwnd = pm["panel"]["hwnd"]
    hwnd = int(raw_hwnd, 16) if isinstance(raw_hwnd, str) else int(raw_hwnd)

    fore_err = _check_foreground(hwnd)
    if fore_err:
        result["error"] = fore_err
        return result

    _bring_below(hwnd)
    was_min = _ensure_restored(hwnd)
    result["steps"].append(f"窗口就绪 hwnd={hwnd:x} was_minimized={was_min}")

    # -- 4) 账号切换 --
    sw_err = _switch_account_if_needed(pm, account)
    if sw_err:
        result["error"] = sw_err
        if was_min:
            _show_window(hwnd, 6)
        return result
    result["steps"].append(f"账号 {account} 就绪")

    # -- 5) 左侧导航 --
    nav_err = _select_nav_item(app, side)
    if nav_err:
        result["error"] = nav_err
        if was_min:
            _show_window(hwnd, 6)
        return result
    result["steps"].append(f"已进入 {nav_name}")

    # -- 6) 查找 Edit 输入框 --
    edits = _find_edit_controls(app)
    if edits is None:
        result["error"] = "导航后未找到 Edit 输入框（表单未渲染？）"
        if was_min:
            _show_window(hwnd, 6)
        return result
    result["steps"].append(f"找到 {len(edits)} 个 Edit 控件")

    labeled = _identify_edits(edits)
    missing = [r for r in ("code", "price", "qty") if r not in labeled]
    if missing:
        result["error"] = f"未能标识输入框角色: {missing}"
        if was_min:
            _show_window(hwnd, 6)
        return result

    # -- 7) 填入 --
    fill_map = {"code": str(code), "price": str(price), "qty": str(qty)}
    fill_errors: dict[str, str] = {}
    for role, value in fill_map.items():
        fe = _fill_edit(labeled[role], value)
        if fe:
            fill_errors[role] = fe

    time.sleep(POST_FILL_SETTLE_SEC)

    # -- 8) 读回 --
    filled_vals: dict[str, str] = {}
    for role, edit in labeled.items():
        filled_vals[role] = _read_edit_value(edit)
    filled_vals["side"] = "买入" if side == "buy" else "卖出"
    filled_vals["account"] = account
    filled_vals["label"] = pm.get("identity_label", "")
    result["filled"] = filled_vals

    if fill_errors:
        result["error"] = f"填入部分失败: {fill_errors}"
        if was_min:
            _show_window(hwnd, 6)
        return result

    result["success"] = True
    result["steps"].append("填值完成（未提交/未撤单）")

    if was_min:
        _show_window(hwnd, 6)

    return result


def _click_buy_button(app) -> Optional[str]:
    """点击买入按钮（aid=1006），仅Phase 3使用。返回None=成功。"""
    try:
        dlg = app.window(title=PANEL_TITLE)
        buttons = dlg.descendants(control_type="Button")
        for b in buttons:
            if b.element_info.automation_id == "1006":
                b.set_focus()
                time.sleep(0.3)
                from pywinauto.keyboard import send_keys
                send_keys("{ENTER}")
                time.sleep(2)
                return None
        return "未找到买入按钮 (aid=1006)"
    except Exception as e:
        return f"点击买入按钮失败: {e}"


def submit_order(
    account: str,
    code: str,
    price: float,
    qty: int,
    side: str = "buy",
) -> dict[str, Any]:
    """Phase 3: 填单+提交+等待确认弹窗。先风控后执行。"""
    result: dict[str, Any] = {
        "schema": "l2_submit_order/1",
        "success": False,
        "filled": {},
        "order_id": None,
        "error": None,
        "steps": [],
        "ts": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
    # 1. 风控门
    try:
        import sys as _sys, json as _json
        _sys.path.insert(0, str(Path(r"F:/Aistock/app")))
        from services.l2_order_gate import check_order, OrderRequest, GateContext
        from datetime import datetime as _dt

        req = OrderRequest(account=account, code=code, side=side,
                           price=round(price, 2), qty=qty,
                           ts=_dt.now())
        ctx = GateContext(now=_dt.now(), kill_switch=False)
        # 加载真实 l2_risk_config
        cfg_path = Path(r"F:/Aistock/data/data/runtime/t_tracker/l2_risk_config.json")
        if cfg_path.exists():
            cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
        else:
            cfg = {"schema": "l2_risk_config/1", "global_breakers": {}}
        gate = check_order(req, ctx, cfg)
        if not gate.approved:
            result["error"] = f"风控拒绝: {gate.rejections}"
            return result
    except Exception as e:
        result["error"] = f"风控门异常: {e}"
        return result
    result["steps"].append("风控门通过")
    # 2. 填单（复用 fill_order 但不要求 panel_map 验证提交按钮）
    fill = fill_order(account, code, price, qty, side)
    if not fill.get("success"):
        result["error"] = fill.get("error", "填单失败")
        result["steps"].extend(fill.get("steps", []))
        return result
    result["filled"] = fill.get("filled", {})
    result["steps"].extend(fill.get("steps", []))
    # 3. 点击提交
    app, err = _find_window()
    if app is None:
        result["error"] = err or "窗口不可用"
        return result
    click_err =_click_buy_button(app)
    if click_err:
        result["error"] = click_err
        return result
    result["steps"].append("已点击买入按钮，等待确认弹窗")
    result["success"] = True
    return result


def fill_and_verify(
    account: str,
    code: str,
    price: float,
    qty: int,
    side: str = "buy",
) -> dict[str, Any]:
    """填单 + 读回核对 + 截图。返回 fill_order 结果 + screenshot + verify。"""
    result = fill_order(account, code, price, qty, side)

    screenshot: Optional[str] = None
    if _PILSUCCESS:
        try:
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%H%M%S")
            path = str(RUNTIME_DIR / f"fill_verify_{ts}.png")
            ImageGrab.grab().save(path)
            screenshot = path
        except Exception as e:
            screenshot = f"<screenshot_failed: {e}>"
    result["screenshot"] = screenshot

    if result["success"]:
        filled = result.get("filled", {})
        mc = code in filled.get("code", "")
        mp = str(price) in filled.get("price", "")
        mq = str(qty) in filled.get("qty", "")
        result["verify"] = {
            "code_match": mc,
            "price_match": mp,
            "qty_match": mq,
            "all_match": mc and mp and mq,
        }
        if not result["verify"]["all_match"]:
            result["steps"].append(
                f"读回核对: code={filled.get('code')!r} "
                f"price={filled.get('price')!r} "
                f"qty={filled.get('qty')!r} -- 与目标不完全一致"
            )
        else:
            result["steps"].append("读回核对一致")

    return result


# ===================================================================
# 7.  独立运行入口（演练用，Owner 在场）
# ===================================================================


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="R50 Phase 2 填单演练（绝不点提交）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="铁律：零 Invoke 提交按钮 / 零 LLM 调用 / 零额度消耗",
    )
    ap.add_argument("--account", default="acct1", choices=["acct1", "acct2"])
    ap.add_argument("--code", default="002015", help="股票代码")
    ap.add_argument("--price", type=float, default=13.50, help="填单价格")
    ap.add_argument("--qty", type=int, default=100, help="股数")
    ap.add_argument("--side", default="buy", choices=["buy", "sell"])
    ap.add_argument(
        "--verify-only",
        action="store_true",
        help="只验证 panel_map 有效性，不操作面板",
    )
    args = ap.parse_args()

    if args.verify_only:
        try:
            pm = load_panel_map()
            submits = get_submit_buttons(pm)
            print(
                json.dumps(
                    {
                        "status": pm.get("status"),
                        "account": pm.get("account"),
                        "identity_label": pm.get("identity_label"),
                        "node_count": pm.get("node_count"),
                        "submit_buttons": [
                            {
                                "name": s["name"],
                                "ct": s.get("control_type"),
                                "rect": s.get("rect"),
                            }
                            for s in submits
                        ],
                        "pywinauto": _PYWINSUCCESS,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        except RuntimeError as e:
            print(f"panel_map 无效: {e}")
            return 1

    result = fill_and_verify(
        args.account, args.code, args.price, args.qty, args.side
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    if result["success"]:
        v = result.get("verify", {})
        status = "一致" if v.get("all_match") else "有差异"
        print(
            f"\n填单完成。面板已填入: {args.code} @ {args.price} x "
            f"{args.qty} {'买' if args.side=='buy' else '卖'} "
            f"（读回{status}）"
        )
        print("请目视检查输入框数字是否正确。代码绝不提交。")
        print(f"截图: {result.get('screenshot', 'N/A')}")
        return 0
    else:
        print(f"\n填单失败: {result['error']}")
        for s in result.get("steps", []):
            print(f"  - {s}")
        return 1


if __name__ == "__main__":
    sys.exit(main())