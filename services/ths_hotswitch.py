# -*- coding: utf-8 -*-
"""R49 单面板双账号热切换（状态机 + 采集计划构建）。

实况（2026-11-11 Owner 指令）：全系统唯一交易面板 xiadan.exe，
acct1（广发-曾*，常态位）/ acct2（广发-王*美）共享同一物理面板，
一切窗口操作串行在 legacy 共享锁 ths_window.lock 上。

R49c 机制修正（2026-11-11 深夜微切片，Owner 授权）：Alt+N 热键判死
（12 轮 OCR 自证恒另一账号，URGENT_R50_HOTSWITCH_LIVE_FAIL）→ 改走
顶栏账号切换器 ComboBox(aAutomationId=2322) 的 UIA SelectionItem 选择
路径（R50 勘察 panel_map.json 实证结构）：展开下拉 → 按文本唯一匹配
目标账号列表项 → SelectionItem.Select() → OCR 身份自证。

铁律（task_briefs/20261111_acct2_bind_switch_r49.md + R49c 微切片）：
  1. 切换前置：前台无人校验（Owner 正用面板 → skipped_foreground，绝不抢）；
  2. 切换必须 OCR 身份自证：Select 只是"已发出"，读不到目标账号令牌
     → switch_failed fail-closed（防选择失效/落错账号错读，绝不猜）；
  3. 读毕切回常态位（home，默认 acct1 曾*）；切回失败 → 数据有效但
     switchback_failed 留痕告警；
  4. 面板身份未知（既非绑定账号任一令牌）→ identity_mismatch 零选择零采集；
  5. 交互授权仅限账号切换器这一处控件（ComboBox aid=2322 及其展开
     子按钮）；其余一切控件零触碰；绝不触任何下单界面；formal 表零写。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

HOTSWITCH_LOCK_NAME = "ths_window.lock"   # 与 bridge.HOTSWITCH_LOCK_NAME 一致
SWITCH_WAIT_ATTEMPTS = 12                 # Select 后身份轮询次数（×0.6s ≈ 7s+OCR）
SWITCH_WAIT_SLEEP_S = 0.6

# R49c：账号切换器 = 顶栏 ComboBox（R50 勘察 runtime/l2_r50/panel_map.json
# 实证：aid=2322，Value 可读，子按钮 aid=DropDown"打开"，关闭态列表项不暴露）
COMBOBOX_AUTOMATION_ID = "2322"
COMBOBOX_CONTROL_TYPE = 50003             # UIA ComboBox
LIST_ITEM_CONTROL_TYPE = 50007            # UIA ListItem
DROPDOWN_CHILD_ID = "DropDown"            # ComboBox 展开子按钮（Invoke）
ITEM_SCAN_ATTEMPTS = 4                    # 展开后列表项出现轮询次数
ITEM_SCAN_FIRST_SLEEP_S = 0.25
ITEM_SCAN_SLEEP_S = 0.4
UIA_MAX_NODES = 2500                      # 全树扫描上限（勘察实测 282 节点）
UIA_MAX_DEPTH = 30

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


def read_identity_from_combobox(window: Any) -> str:
    """R51 备用身份锚：账号切换器 ComboBox(aid=2322) 当前显示文本（只读）。

    2026-11-11 R50 勘察实证：面板已登录时 value='广发证券-王*美'；
    登录框态/未加载时为空串。零交互（UIA Name/Value 只读，绝不展开下拉）。
    comtypes/Windows 缺席或任何 COM 异常 → 空串（fail-safe，不阻断主锚）。
    """
    try:
        from comtypes.client import CreateObject, GetModule
        GetModule("UIAutomationCore.dll")
        import comtypes.gen.UIAutomationClient as UIAC  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - 容器/测试环境无 Windows UIA
        return ""

    try:
        uia = CreateObject(UIAC.CUIAutomation, interface=UIAC.IUIAutomation)
        hwnd = int(getattr(window, "handle", 0) or 0)
        if not hwnd:
            return ""
        root = uia.ElementFromHandle(hwnd)

        def _safe(fn, *args):
            try:
                return fn(*args)
            except Exception:  # noqa: BLE001
                return None

        def _children(elem):
            out, child, guard = [], _safe(uia.ControlViewWalker.GetFirstChildElement, elem), 0
            while child is not None and guard < 300:
                guard += 1
                out.append(child)
                child = _safe(uia.ControlViewWalker.GetNextSiblingElement, child)
            return out

        stack, seen = [(root, 0)], 0
        while stack and seen < UIA_MAX_NODES:
            elem, depth = stack.pop()
            seen += 1
            try:
                is_combo = (str(getattr(elem, "CurrentAutomationId", "") or "")
                            == COMBOBOX_AUTOMATION_ID)
            except Exception:  # noqa: BLE001
                is_combo = False
            if is_combo:
                name = str(_safe(lambda: elem.CurrentName) or "")
                value = ""
                unk = _safe(elem.GetCurrentPattern, UIAC.UIA_ValuePatternId)
                if unk is not None:
                    vp = _safe(unk.QueryInterface, UIAC.IUIAutomationValuePattern)
                    if vp is not None:
                        value = str(_safe(vp.GetValue) or "")
                return (name or value or "").strip()
            if depth >= UIA_MAX_DEPTH:
                continue
            for ch in reversed(_children(elem)):
                stack.append((ch, depth + 1))
    except Exception:  # noqa: BLE001 - COM 树遍历异常按空锚处理
        return ""
    return ""


def read_window_identity_robust(window: Any,
                                save_png: Optional[Path] = None) -> dict[str, Any]:
    """R51 身份多锚读取：PrintWindow OCR 主锚 → 空时 ComboBox(2322) 备用锚。

    返回 {ocr_label, combo_text, screenshot}；两锚都尽力而为，异常不抛
    （门禁决策在调用方 gate_identity_decision，纯函数可测）。
    """
    try:
        ocr_label, png = read_window_identity(window, save_png=save_png)
    except Exception:  # noqa: BLE001 - PrintWindow/OCR 失败按空锚降级
        ocr_label, png = "", None
    combo_text = ""
    if not ocr_label:
        combo_text = read_identity_from_combobox(window)
    return {"ocr_label": ocr_label, "combo_text": combo_text,
            "screenshot": png}


# --------------------------------------------------------------------------- #
# 账号切换（R49c：ComboBox aid=2322 SelectionItem 选择路径）
# --------------------------------------------------------------------------- #

def match_account_item_names(names: list[str],
                             expect_hint: str) -> tuple[Optional[int], str]:
    """下拉列表项文本 → 目标账号项（纯函数，可测）。

    唯一命中返回 (index, "")；否则 (None, reason)：
    no_hint / no_items / item_not_found / ambiguous_match（多项含目标令牌
    =歧义，fail-closed 绝不任选）。
    """
    hint = str(expect_hint or "").strip()
    if not hint:
        return None, "no_hint"
    clean = [str(n or "").strip() for n in (names or [])]
    if not any(clean):
        return None, "no_items"
    hits = [i for i, n in enumerate(clean) if hint in n]
    if not hits:
        return None, "item_not_found"
    if len(hits) > 1:
        return None, "ambiguous_match"
    return hits[0], ""


def _uia_select_account(hwnd: int, expect_hint: str) -> dict[str, Any]:
    """真机路径：UIA 定位账号切换器 ComboBox(aid=2322) → 展开下拉 →
    按文本唯一匹配目标账号列表项 → SelectionItem.Select()。

    Owner 2026-11-11 深夜微切片授权：交互仅限账号切换器这一处控件
    （ComboBox 及其 DropDown 展开子按钮），其余控件零触碰。
    fail-closed：ComboBox 缺席/展开失败/无项/无匹配/歧义/Select 异常
    → {"ok": False, "reason": ...}，绝不猜、绝不代点其他控件。
    comtypes 懒加载（容器/测试环境无 Windows UIA）。
    """
    from comtypes.client import CreateObject, GetModule
    GetModule("UIAutomationCore.dll")
    import comtypes.gen.UIAutomationClient as UIAC  # noqa: PLC0415

    def _safe(fn, *args):
        try:
            return fn(*args)
        except Exception:  # noqa: BLE001 - COMError/E_POINTER：按无处理
            return None

    def _prop(elem, name, default=None):
        try:
            return getattr(elem, name)
        except Exception:  # noqa: BLE001
            return default

    def _children(elem):
        out = []
        child = _safe(walker.GetFirstChildElement, elem)
        guard = 0
        while child is not None and guard < 300:
            guard += 1
            out.append(child)
            child = _safe(walker.GetNextSiblingElement, child)
        return out

    def _walk(root_elem, max_depth: int = UIA_MAX_DEPTH):
        """迭代 DFS（ControlView），节点数/深度封顶。"""
        stack = [(root_elem, 0)]
        seen = 0
        while stack and seen < UIA_MAX_NODES:
            elem, depth = stack.pop()
            seen += 1
            yield elem, depth
            if depth >= max_depth:
                continue
            for ch in reversed(_children(elem)):
                stack.append((ch, depth + 1))

    def _find_combobox(root_elem):
        for elem, _d in _walk(root_elem):
            if (_prop(elem, "CurrentAutomationId", "") == COMBOBOX_AUTOMATION_ID
                    and int(_prop(elem, "CurrentControlType", 0) or 0)
                    == COMBOBOX_CONTROL_TYPE):
                return elem
        return None

    def _item_name(elem):
        name = str(_prop(elem, "CurrentName", "") or "").strip()
        if name:
            return name
        for ch in _children(elem):   # DirectUI 项文本或在子节点上
            name = str(_prop(ch, "CurrentName", "") or "").strip()
            if name:
                return name
        return ""

    def _collect_items(scope_elem, combo_rect, rect_filter: bool = False):
        """收集 SelectionItem 列表项：子树优先（不过滤）；全窗兜底扫描
        才按"横向与切换器重叠 + 纵向紧邻其下"过滤，防把别处列表项当账号。"""
        items = []
        cl, ct, cr, cb = combo_rect or (None, None, None, None)
        for elem, _d in _walk(scope_elem):
            if int(_prop(elem, "CurrentControlType", 0) or 0) \
                    != LIST_ITEM_CONTROL_TYPE:
                continue
            if not _safe(elem.GetCurrentPropertyValue,
                         UIAC.UIA_IsSelectionItemPatternAvailablePropertyId):
                continue
            if rect_filter and None not in (cl, ct, cr, cb):
                rect = _prop(elem, "CurrentBoundingRectangle")
                l = getattr(rect, "left", None)
                t = getattr(rect, "top", None)
                r = getattr(rect, "right", None)
                b = getattr(rect, "bottom", None)
                if None in (l, t, r, b):
                    continue
                if min(r, cr) - max(l, cl) <= 0:       # 横向不重叠
                    continue
                if not (ct - 60 <= t <= cb + 520):     # 纵向不紧邻（下拉向下开）
                    continue
            items.append((_item_name(elem), elem))
        return items

    def _collapse_best_effort():
        """未选中任何项时收起下拉（仍是切换器控件自身交互；失败不阻断）。"""
        try:
            combo2 = _find_combobox(uia.ElementFromHandle(int(hwnd)))
            if combo2 is None:
                return
            if _safe(combo2.GetCurrentPropertyValue,
                     UIAC.UIA_IsExpandCollapsePatternAvailablePropertyId):
                unk = _safe(combo2.GetCurrentPattern,
                            UIAC.UIA_ExpandCollapsePatternId)
                if unk:
                    _safe(unk.QueryInterface(
                        UIAC.IUIAutomationExpandCollapsePattern).Collapse)
                    return
            for elem, _d in _walk(combo2, max_depth=2):
                if _prop(elem, "CurrentAutomationId", "") == DROPDOWN_CHILD_ID:
                    unk = _safe(elem.GetCurrentPattern, UIAC.UIA_InvokePatternId)
                    if unk:
                        _safe(unk.QueryInterface(
                            UIAC.IUIAutomationInvokePattern).Invoke)  # 再点=收起
                        return
        except Exception:  # noqa: BLE001
            pass

    uia = CreateObject(UIAC.CUIAutomation, interface=UIAC.IUIAutomation)
    walker = uia.ControlViewWalker
    root = uia.ElementFromHandle(int(hwnd))

    # 1) 定位账号切换器（aid=2322 契约；缺席=fail-closed，绝不模糊代找）
    combo = _find_combobox(root)
    if combo is None:
        return {"ok": False, "reason": "combobox_absent"}
    rect = _prop(combo, "CurrentBoundingRectangle")
    combo_rect = (getattr(rect, "left", None), getattr(rect, "top", None),
                  getattr(rect, "right", None), getattr(rect, "bottom", None))

    # 2) 展开下拉：ExpandCollapse 优先（语义通道），缺席走 DropDown 子按钮
    #    Invoke（勘察实证唯一展开通道）
    expand_via = ""
    if _safe(combo.GetCurrentPropertyValue,
             UIAC.UIA_IsExpandCollapsePatternAvailablePropertyId):
        unk = _safe(combo.GetCurrentPattern, UIAC.UIA_ExpandCollapsePatternId)
        if unk:
            _safe(unk.QueryInterface(
                UIAC.IUIAutomationExpandCollapsePattern).Expand)
            expand_via = "expandcollapse"
    if not expand_via:
        for elem, _d in _walk(combo, max_depth=2):
            if _prop(elem, "CurrentAutomationId", "") != DROPDOWN_CHILD_ID:
                continue
            unk = _safe(elem.GetCurrentPattern, UIAC.UIA_InvokePatternId)
            if unk:
                _safe(unk.QueryInterface(UIAC.IUIAutomationInvokePattern).Invoke)
                expand_via = "dropdown_invoke"
                break
    if not expand_via:
        return {"ok": False, "reason": "expand_failed"}

    # 3) 列表项出现（展开有动画，轮询取新鲜树）→ 文本唯一匹配 → Select
    names: list[str] = []
    scope_used = ""
    matched = None
    idx, reason = (None, "no_items")
    for attempt in range(ITEM_SCAN_ATTEMPTS):
        time.sleep(ITEM_SCAN_FIRST_SLEEP_S if attempt == 0
                   else ITEM_SCAN_SLEEP_S)
        fresh_root = _safe(uia.ElementFromHandle, int(hwnd))
        if fresh_root is None:
            continue
        live_combo = _find_combobox(fresh_root) or combo
        pairs = _collect_items(live_combo, combo_rect)
        scope_used = "combo_subtree"
        if not any(n for n, _e in pairs):
            pairs = _collect_items(fresh_root, combo_rect, rect_filter=True)
            scope_used = "window_rect_filtered"
        names = [n for n, _e in pairs]
        idx, reason = match_account_item_names(names, expect_hint)
        if idx is not None:
            matched = pairs[idx]
            break
    if idx is None or matched is None:
        _collapse_best_effort()
        return {"ok": False, "reason": reason, "items": names,
                "expand": expand_via, "scope": scope_used}

    matched_name, matched_elem = matched
    try:
        unk = matched_elem.GetCurrentPattern(UIAC.UIA_SelectionItemPatternId)
        sp = unk.QueryInterface(UIAC.IUIAutomationSelectionItemPattern)
        sp.Select()
    except Exception as exc:  # noqa: BLE001
        _collapse_best_effort()
        return {"ok": False, "reason": "select_error",
                "error": f"{type(exc).__name__}: {exc}",
                "items": names, "matched": matched_name,
                "expand": expand_via, "scope": scope_used}
    return {"ok": True, "matched": matched_name, "items": names,
            "expand": expand_via, "scope": scope_used}


def _select_account_and_verify(window: Any, expect_hint: str) -> dict[str, Any]:
    """ComboBox 选择目标账号 → OCR 身份自证轮询（Select 只是"已发出"，
    读到目标令牌才算数）。返回 {ok, identity_label?, reason?, ...}。"""
    hwnd = int(getattr(window, "handle", 0) or 0)
    try:
        sel = _uia_select_account(hwnd, expect_hint)
    except Exception as exc:  # noqa: BLE001 - comtypes 缺失/COM 异常 → fail-closed
        return {"ok": False, "reason": "uia_error", "identity_label": "",
                "error": f"{type(exc).__name__}: {exc}"}
    if not sel.get("ok"):
        return {"ok": False, "reason": sel.get("reason", "uia_select_failed"),
                "identity_label": "",
                "detail": {k: v for k, v in sel.items() if k != "ok"}}
    last_label = ""
    for _ in range(SWITCH_WAIT_ATTEMPTS):
        time.sleep(SWITCH_WAIT_SLEEP_S)
        try:
            last_label, _png = read_window_identity(window)
        except Exception:  # noqa: BLE101 - 切换期窗口重绘，读失败重试
            continue
        if expect_hint and expect_hint in last_label:
            return {"ok": True, "identity_label": last_label,
                    "selected_item": sel.get("matched")}
    return {"ok": False, "reason": "verify_failed", "identity_label": last_label,
            "attempts": SWITCH_WAIT_ATTEMPTS,
            "selected_item": sel.get("matched")}


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
    """确保面板显示目标账号；不在则经账号切换器（ComboBox aid=2322
    SelectionItem）切换并自证。

    cfg: {target_hint, known_hints: [(hint, account), ...], ...}
    返回 None=已在目标/切换自证通过；dict=fail-closed 终态
    （identity_mismatch / switch_failed），调用方原样上报、零采集。
    """
    _ensure_restored(window)
    label, png = read_window_identity(window)
    target = str(cfg.get("target_hint") or "")
    if target and target in label:
        return None  # 已在目标账号：零切换零交互
    if _match_known(label, cfg.get("known_hints") or []) is None:
        return {"status": STATUS_IDENTITY_MISMATCH, "identity_label": label,
                "identity_hint": target, "screenshot": png,
                "note": "面板登录身份未知（非绑定账号令牌），fail-closed 零切换"}
    res = _select_account_and_verify(window, target)
    if not res.get("ok"):
        return {"status": STATUS_SWITCH_FAILED, "identity_label": res.get("identity_label"),
                "identity_hint": target, "reason": res.get("reason"),
                "attempts": res.get("attempts"), "screenshot": png,
                "note": f"账号切换器选择/身份自证未通过（reason={res.get('reason')}），fail-closed"}
    return None


def restore_home_account(window: Any, cfg: dict[str, Any]) -> Optional[dict[str, Any]]:
    """读毕切回常态位账号（home）。返回 None=已回/切回自证通过；dict=失败详情。"""
    home = str(cfg.get("home_hint") or "")
    if not home:
        return {"status": STATUS_SWITCHBACK_FAILED, "reason": "home_no_hint",
                "note": "常态位账号未绑定身份令牌，无法切回（保留现场）"}
    try:
        _ensure_restored(window)
        label, _png = read_window_identity(window)
        if home in label:
            return None
        res = _select_account_and_verify(window, home)
    except Exception as exc:  # noqa: BLE001
        return {"status": STATUS_SWITCHBACK_FAILED, "reason": "exception",
                "error": f"{type(exc).__name__}: {exc}"}
    if not res.get("ok"):
        return {"status": STATUS_SWITCHBACK_FAILED, "reason": res.get("reason"),
                **{k: res[k] for k in ("identity_label", "attempts") if k in res}}
    return None


# --------------------------------------------------------------------------- #
# 采集计划构建（巡读 / fills 导出共用）
# --------------------------------------------------------------------------- #

def build_hotswitch_plan(registry: Any, live_panels: list[dict[str, Any]],
                         account_filter: Optional[str] = None,
                         ) -> dict[str, dict[str, Any]]:
    """注册表 × live 面板 → 有序采集计划 {account: {panel, identity_hint,
    hotswitch?}}。

    顺序：home 常态位在前（先读曾→切王读王→切回曾）；热切换组末位账号
    restore_after=True（读毕切回常态位）。非热切换账号（无热键规则）
    保持 R47 独占面板语义。acct1 无绑定面板时入计划走默认 connect（legacy）。
    R49c：切换机制=ComboBox aid=2322 SelectionItem（switch_mode 标注）；
    注册表 hotkey 字段仅作"热切换已绑定"标记保留（Alt+N 已判死不再发送）。
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
    home_restorable = bool(home_rule.get("identity_hint")
                           and (home_rule.get("hotkey")
                                or home_rule.get("switch_mode")))
    known_hints = [(str(r.get("identity_hint")), acct) for acct, r in sorted(rules.items())
                   if r.get("identity_hint")
                   and (r.get("hotkey") or r.get("switch_mode"))]

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
        if (rule.get("hotkey") or rule.get("switch_mode")) \
                and panel is not None and rule.get("identity_hint"):
            entry["hotswitch"] = {
                "target_hint": rule.get("identity_hint"),
                "known_hints": list(known_hints),
                "home_hint": home_rule.get("identity_hint") if home_restorable else "",
                "restore_after": False,   # 末位标记见下
                "switch_mode": "combobox_selection",   # R49c 机制标注
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
