# -*- coding: utf-8 -*-
"""R49 单面板 Alt 热切换双账号读取（状态机 + 采集计划构建）。

实况（2026-11-11 Owner 指令）：全系统唯一交易面板 xiadan.exe，
Alt+1 = acct1（广发-曾*，常态位）/ Alt+2 = acct2（广发-王*美）热切换账号。
R47 的"双面板分把锁"假设按热切换模式适配：两账号共享同一物理窗口，
一切窗口操作串行在 legacy 共享锁 ths_window.lock 上。

铁律（task_briefs/20261111_acct2_bind_switch_r49.md）：
  1. 切换前置：前台无人校验（Owner 正用面板 → skipped_foreground，绝不抢）；
  2. 切换必须 OCR 身份自证：Alt+N 发出后读不到目标账号令牌 → switch_failed
     fail-closed（防热键失效/落错账号错读，绝不猜）；
  3. 读毕切回常态位（home，默认 acct1 曾*）；切回失败 → 数据有效但
     switchback_failed 留痕告警；
  4. 面板身份未知（既非绑定账号任一令牌）→ identity_mismatch 零按键零采集；
  5. 绝不触任何下单界面；formal 表零写。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

HOTSWITCH_LOCK_NAME = "ths_window.lock"   # 与 bridge.HOTSWITCH_LOCK_NAME 一致
SWITCH_WAIT_ATTEMPTS = 12                 # Alt+N 后身份轮询次数（×0.6s ≈ 7s+OCR）
SWITCH_WAIT_SLEEP_S = 0.6

STATUS_SWITCH_FAILED = "switch_failed"
STATUS_SWITCHBACK_FAILED = "switchback_failed"
STATUS_IDENTITY_MISMATCH = "identity_mismatch"


# --------------------------------------------------------------------------- #
# 身份读取（零交互：PrintWindow 离屏渲染 + OCR，不吃焦点不点菜单）
# --------------------------------------------------------------------------- #

def read_window_identity(window: Any,
                         save_png: Optional[Path] = None
                         ) -> tuple[str, Optional[str]]:
    """读面板顶栏身份标签「券商-姓名」。返回 (label, screenshot_path|None)。"""
    import os
    import tempfile

    from services.ths_account_bridge import parse_identity_label
    from services.ths_holdings_poll import _ocr_image, print_window_image

    hwnd = int(getattr(window, "handle", 0) or 0)
    img = print_window_image(hwnd)
    png: Optional[Path]
    if save_png is not None:
        save_png.parent.mkdir(parents=True, exist_ok=True)
        png = save_png
    else:
        png = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) \
            / "ths_hotswitch_identity.png"
    try:
        img.save(png)
    except Exception:  # noqa: BLE001 - 截图落盘失败不阻断身份判定
        return "", None
    _items, lines = _ocr_image(Path(png))
    return parse_identity_label(lines), str(png)


def _match_known(label: str, known_hints: list[tuple[str, str]]) -> Optional[str]:
    """身份标签 → 已绑定账号（hint 子串匹配）。未知返回 None。"""
    for hint, account in known_hints or []:
        if hint and label and hint in label:
            return str(account)
    return None


def _switch_and_verify(window: Any, hotkey: str, expect_hint: str) -> dict[str, Any]:
    """发送 Alt+<hotkey> → 轮询身份标签直到读到 expect_hint（身份自证）。"""
    from pywinauto.keyboard import send_keys

    window.set_focus()          # 热键是窗口加速键：须前台接收（前台无人已校验）
    time.sleep(0.15)
    send_keys("%" + str(hotkey), pause=0.05)
    last_label = ""
    for _ in range(SWITCH_WAIT_ATTEMPTS):
        time.sleep(SWITCH_WAIT_SLEEP_S)
        try:
            last_label, _png = read_window_identity(window)
        except Exception:  # noqa: BLE101 - 切换期窗口重绘，读失败重试
            continue
        if expect_hint and expect_hint in last_label:
            return {"ok": True, "identity_label": last_label}
    return {"ok": False, "identity_label": last_label,
            "attempts": SWITCH_WAIT_ATTEMPTS}


# --------------------------------------------------------------------------- #
# 状态机（ensure / restore；返回 None=通过，dict=终态结果）
# --------------------------------------------------------------------------- #

def _ensure_restored(window: Any) -> None:
    """最小化窗口先恢复（PrintWindow 对 iconic 窗口渲染空白）。

    仅 fills 导出路径会带最小化窗到达此处（巡读 capture 在此之前已
    skipped_minimized 让位零交互铁律）；复最小化由调用方按 was_minimized
    既有语义处理。"""
    try:
        if bool(window.is_minimized()):
            window.restore()
            time.sleep(0.3)
    except Exception:  # noqa: BLE001 - 假窗/已销毁：交给后续身份读取失败兜底
        pass


def ensure_target_account(window: Any, cfg: dict[str, Any]) -> Optional[dict[str, Any]]:
    """确保面板显示目标账号；不在则热切换并自证。

    cfg: {target_hint, target_hotkey, known_hints: [(hint, account), ...]}
    返回 None=已在目标/切换自证通过；dict=fail-closed 终态
    （identity_mismatch / switch_failed），调用方原样上报、零采集。
    """
    _ensure_restored(window)
    label, png = read_window_identity(window)
    target = str(cfg.get("target_hint") or "")
    if target and target in label:
        return None  # 已在目标账号：零切换零按键
    if _match_known(label, cfg.get("known_hints") or []) is None:
        return {"status": STATUS_IDENTITY_MISMATCH, "identity_label": label,
                "identity_hint": target, "screenshot": png,
                "note": "面板登录身份未知（非绑定账号令牌），fail-closed 零切换"}
    res = _switch_and_verify(window, cfg.get("target_hotkey"), target)
    if not res.get("ok"):
        return {"status": STATUS_SWITCH_FAILED, "identity_label": res.get("identity_label"),
                "identity_hint": target, "attempts": res.get("attempts"),
                "screenshot": png,
                "note": "Alt 切换后未读到目标账号身份令牌（热键失效/落错账号），fail-closed"}
    return None


def restore_home_account(window: Any, cfg: dict[str, Any]) -> Optional[dict[str, Any]]:
    """读毕切回常态位账号（home）。返回 None=已回/切回自证通过；dict=失败详情。"""
    home = str(cfg.get("home_hint") or "")
    hotkey = cfg.get("home_hotkey")
    if not home or not hotkey:
        return {"status": STATUS_SWITCHBACK_FAILED, "reason": "home_no_hotkey",
                "note": "常态位账号未绑定热键，无法切回（保留现场）"}
    try:
        _ensure_restored(window)
        label, _png = read_window_identity(window)
        if home in label:
            return None
        res = _switch_and_verify(window, hotkey, home)
    except Exception as exc:  # noqa: BLE101
        return {"status": STATUS_SWITCHBACK_FAILED,
                "error": f"{type(exc).__name__}: {exc}"}
    if not res.get("ok"):
        return {"status": STATUS_SWITCHBACK_FAILED,
                **{k: res[k] for k in ("identity_label", "attempts") if k in res}}
    return None


# --------------------------------------------------------------------------- #
# 采集计划构建（巡读 / fills 导出共用）
# --------------------------------------------------------------------------- #

def build_hotswitch_plan(registry: Any, live_panels: list[dict[str, Any]],
                         account_filter: Optional[str] = None,
                         ) -> dict[str, dict[str, Any]]:
    """注册表 × live 面板 → 有序采集计划 {account: {panel, identity_hint,
    hotkey?, hotswitch?}}。

    顺序：home 常态位在前（先读曾→切王读王→切回曾）；热切换组末位账号
    restore_after=True（读毕切回常态位）。非热切换账号（无 hotkey 规则）
    保持 R47 独占面板语义。acct1 无绑定面板时入计划走默认 connect（legacy）。
    """
    resolved = registry.resolve(live_panels or [])
    rules = {str(r.get("account")): r for r in resolved.rules
             if isinstance(r, dict)}
    home = registry.home_account()
    hot_accounts = sorted({a for accts in resolved.hotswitch_groups.values()
                           for a in accts})
    order = [home]  # home 常态位恒在前（先读曾→切王读王→切回曾）
    order += [a for a in hot_accounts if a != home]
    order += [a for a in sorted(resolved.by_account)
              if a not in hot_accounts and a != home]

    home_rule = rules.get(home) or {}
    home_restorable = bool(home_rule.get("hotkey") and home_rule.get("identity_hint"))
    known_hints = [(str(r.get("identity_hint")), acct) for acct, r in sorted(rules.items())
                   if r.get("hotkey") and r.get("identity_hint")]

    plan: dict[str, dict[str, Any]] = {}
    for account in order:
        if account_filter and account != account_filter:
            continue
        rule = rules.get(account) or {}
        panel = resolved.by_account.get(account)
        if account == "acct1" and panel is None:
            panel = None  # legacy 默认 connect 路径
        entry: dict[str, Any] = {"panel": panel,
                                 "identity_hint": rule.get("identity_hint")}
        if rule.get("hotkey") and panel is not None and rule.get("identity_hint"):
            entry["hotkey"] = rule.get("hotkey")
            entry["hotswitch"] = {
                "target_hint": rule.get("identity_hint"),
                "target_hotkey": rule.get("hotkey"),
                "known_hints": list(known_hints),
                "home_hint": home_rule.get("identity_hint") if home_restorable else "",
                "home_hotkey": home_rule.get("hotkey") if home_restorable else "",
                "restore_after": False,   # 末位标记见 _mark_restore_last
                "lock_name": HOTSWITCH_LOCK_NAME,
            }
        plan[account] = entry

    # 热切换组末位（本轮实际要读的最后一个组员）读毕切回常态位
    last_by_group: dict[int, str] = {}
    for account in plan:
        entry = plan[account] or {}
        pid = int((entry.get("panel") or {}).get("pid") or 0)
        if pid and entry.get("hotswitch"):
            last_by_group[pid] = account
    for account in last_by_group.values():
        entry = plan[account]
        if home_restorable:
            entry["hotswitch"]["restore_after"] = True
    return plan
