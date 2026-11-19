# -*- coding: utf-8 -*-
"""R49 单面板双账号热切换（状态机 + 采集计划构建）。

实况（2026-11-11 Owner 指令）：全系统唯一交易面板 xiadan.exe，
acct1（广发-曾*，常态位）/ acct2（广发-王*美）共享同一物理面板，
一切窗口操作串行在 legacy 共享锁 ths_window.lock 上。

R52 通道化修正（2026-11-14 凌晨实机取证）：
  R49c 的两个失效根因（实机 UIA 普查 + fills 证据链复盘）：
  ① ths_client._capture_ths_views 的 R51 身份门禁跑在 ensure_target_account
     之前——面板停在另一账号时门禁先行 mismatch，切换代码从未执行
     （20261114.unavailable.json 的 combo='' 只是"备用锚未查询"，非锚死亡；
     实证 ComboBox aid=2322 存在且 Value 正常可读）。已改为 hotswitch 在前。
  ② 展开后的下拉列表项在 UIA 树中被多路重复暴露（同名同 rect ×N），
     match_account_item_names 数出多项命中 → ambiguous_match 假歧义。
     已加 (name, rect) 去重 + 非账号项（编辑账户等）剔除。
  通道选择（Owner 定优先级）：a. Alt+数字热键（发给面板主窗，需前台+焦点，
  SendInput 真实键击；Owner 活跃时绝不切——前台且输入空闲<90s →
  skipped_foreground）→ b. ComboBox 多锚 SelectionItem（aid=2322 主锚，
  Value 含「证券」的 ComboBox 兜底锚）。

铁律（task_briefs/20261111_acct2_bind_switch_r49.md + R49c/R52 微切片）：
  1. 切换前置：前台无人校验（Owner 正用面板 → skipped_foreground，绝不抢）；
  2. 切换必须 OCR 身份自证：Select/热键只是"已发出"，读不到目标账号令牌
     → switch_failed fail-closed（防选择失效/落错账号错读，绝不猜）；
  3. 读毕切回常态位（home，默认 acct1 曾*）；切回失败 → 数据有效但
     switchback_failed 留痕告警；
  4. 面板身份未知（既非绑定账号任一令牌）→ identity_mismatch 零选择零采集；
  5. 交互授权仅限账号切换通道：Alt+数字热键（面板主窗）与账号切换器
     （ComboBox aid=2322 及其展开子按钮/列表项）；其余一切控件零触碰；
     绝不触任何下单界面；formal 表零写。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

HOTSWITCH_LOCK_NAME = "ths_window.lock"   # 与 bridge.HOTSWITCH_LOCK_NAME 一致
SWITCH_WAIT_ATTEMPTS = 12                 # Select 后身份轮询次数（×0.6s ≈ 7s+OCR）
SWITCH_WAIT_SLEEP_S = 0.6
SWITCH_WAIT_ATTEMPTS_HOTKEY = 3           # Alt+N 哑键击快速判死（实机实证恒无效果）

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

# R52：账号切换通道（按 Owner 优先级排序执行，任一通道自证通过即止）
CHANNEL_HOTKEY = "hotkey_alt_n"           # Alt+数字热键（面板主窗，前台+焦点）
CHANNEL_COMBOBOX = "combobox_selection"   # ComboBox 多锚 SelectionItem
DEFAULT_CHANNELS = (CHANNEL_HOTKEY, CHANNEL_COMBOBOX)
OWNER_ACTIVE_IDLE_S = 90.0                # 前台且输入空闲<此值 → Owner 正用，绝不切

STATUS_SWITCH_FAILED = "switch_failed"
STATUS_SWITCHBACK_FAILED = "switchback_failed"
STATUS_IDENTITY_MISMATCH = "identity_mismatch"
STATUS_SKIPPED_FOREGROUND = "skipped_foreground"

# R52 下拉里的非账号项（2026-11-14 实机实证「编辑账户」与账号项同级暴露；
# 绝不选择——选它会打开账户管理对话框，偏离账号视图切换）
NON_ACCOUNT_ITEM_NAMES = frozenset({
    "编辑账户", "添加账户", "管理账户", "添加交易账户", "登录账户",
})


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


def _combo_anchor_text(name: str, value: str) -> str:
    """ComboBox 备用锚取文（纯函数，可测）：Value（账号文本）优先于 Name；
    R51 旧语义 (name or value) 会被空白 Name（实机实证为单个空格）占位
    → 恒空锚。空白按空串处理。"""
    return (str(value or "").strip() or str(name or "").strip())


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
                        # R52：GetValue 是方法（R51 原代码漏调用取到的是
                        # 绑定方法对象）；CurrentValue 属性兜底。
                        value = str(_safe(lambda: vp.GetValue()) or "")
                        if not value.strip():
                            value = str(_safe(lambda: vp.CurrentValue) or "")
                # R52：Value 优先于 Name——实机实证 Name 是单个空格
                # （" " 真值使 R51 的 (name or value) 恒取空白锚 → 恒空）。
                return _combo_anchor_text(name, value)
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
# R52 前置守卫 + 通道A：Alt+数字热键（面板主窗，前台+焦点）
# --------------------------------------------------------------------------- #

def input_idle_seconds() -> float:
    """GetLastInputInfo → 距上次真实用户输入的秒数。

    非 Windows/ctypes 异常 → inf（宁可漏切也不误判 Owner 在用）。"""
    try:
        import ctypes

        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(lii)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return float("inf")
        return max(0.0, (ctypes.windll.kernel32.GetTickCount()
                         - lii.dwTime) / 1000.0)
    except Exception:  # noqa: BLE001 - 守卫失效方向=fail-safe 不切
        return float("inf")


def note_self_injection() -> None:
    """标记"最近一次输入是本自动化注入的"（鼠标/键击），供 Owner 活跃
    判定剔除自身扰动——fills 导出链合法注入菜单点击后，不得把自家输入
    误判成 Owner 手动操作而拒绝热切换（2026-11-14 实机教训）。"""
    global _LAST_SELF_INJECT_TICK
    try:
        import ctypes

        _LAST_SELF_INJECT_TICK = float(
            ctypes.windll.kernel32.GetTickCount())
    except Exception:  # noqa: BLE001 - 标记失败只影响判定精度，不阻断
        pass


_LAST_SELF_INJECT_TICK = 0.0   # GetTickCount ms；0=本进程尚无自注入


def _owner_active_decision(idle_s: float, since_self_inject_s: float,
                           threshold: float = OWNER_ACTIVE_IDLE_S) -> bool:
    """纯函数（可测）：输入空闲 idle_s 内是否为 Owner 真实操作。

    若最近一次输入发生在本进程自注入之后（idle < since_self - 容差），
    说明那次输入是 Owner 的（比我们更晚）→ 活跃；若空闲时长 ≈ 自注入
    以来时长（最后输入就是我们自己）→ 不算 Owner 活跃。"""
    if idle_s >= threshold:
        return False
    if since_self_inject_s <= 0 or since_self_inject_s == float("inf"):
        return True   # 本进程从未注入：空闲即 Owner
    return idle_s < since_self_inject_s - 0.3


def _owner_idle_seconds() -> float:
    """剔除本进程自注入后的 Owner 输入空闲秒数（无注入 = 原始 idle）。"""
    idle = input_idle_seconds()
    global _LAST_SELF_INJECT_TICK
    if _LAST_SELF_INJECT_TICK > 0:
        try:
            import ctypes

            since_self = max(
                0.0, (ctypes.windll.kernel32.GetTickCount()
                      - _LAST_SELF_INJECT_TICK) / 1000.0)
            if idle < since_self - 0.3:
                return idle          # 注入后有真实输入 → Owner 的
            return float("inf")      # 最后一次输入是我们自己 → 视作无 Owner 输入
        except Exception:  # noqa: BLE001
            return idle
    return idle


def owner_using_panel(window: Any) -> bool:
    """铁律门（R52）：面板在前台 且 Owner 输入空闲 < OWNER_ACTIVE_IDLE_S
    → Owner 手在面板上，绝不切换。不在前台 → False（可接管前台）。
    本自动化自己的注入（菜单点击/键击）不计入 Owner 活跃。"""
    try:
        import ctypes

        hwnd = int(getattr(window, "handle", 0) or 0)
        if not hwnd or ctypes.windll.user32.GetForegroundWindow() != hwnd:
            return False
    except Exception:  # noqa: BLE001 - 判不了就按"有人在用"处理
        return True
    return _owner_idle_seconds() < OWNER_ACTIVE_IDLE_S


def _bring_panel_foreground(window: Any) -> dict[str, Any]:
    """把面板带到前台并给键盘焦点（AttachThreadInput 跨线程授权）。

    仅在 owner_using_panel=False（Owner 不在面板上）时由热键通道调用。"""
    import ctypes

    hwnd = int(getattr(window, "handle", 0) or 0)
    user32 = ctypes.windll.user32
    fg = int(user32.GetForegroundWindow() or 0)
    if fg == hwnd:
        return {"ok": True, "foreground": True, "attached": False}
    fg_thread = int(user32.GetWindowThreadProcessId(fg, None)) if fg else 0
    target_thread = int(user32.GetWindowThreadProcessId(hwnd, None))
    attached = bool(fg_thread and target_thread and fg_thread != target_thread)
    if attached:
        user32.AttachThreadInput(fg_thread, target_thread, True)
    user32.ShowWindow(hwnd, 9)   # SW_RESTORE
    # hwndInsertAfter=0 (HWND_TOP) + SWP_NOMOVE|SWP_NOSIZE|SWP_SHOWWINDOW
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0043)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.SetFocus(hwnd)
    if attached:
        user32.AttachThreadInput(fg_thread, target_thread, False)
    time.sleep(0.15)
    ok = int(user32.GetForegroundWindow() or 0) == hwnd
    return {"ok": ok, "foreground": ok, "attached": attached}


def switch_account_via_hotkey(window: Any, hotkey: str) -> dict[str, Any]:
    """通道A 真机路径：Owner 不在用 → 面板前台化 → SendInput Alt+<hotkey>。

    只是把"已发出"，是否生效由调用方 OCR 身份自证裁决（fail-closed）。
    hotkey 仅接受数字（注册表 hotkey 契约 '1'/'2'）。
    comtypes/Windows 缺席 → {"ok": False, "reason": "not_windows"}。"""
    key = str(hotkey or "").strip()
    if not key.isdigit() or len(key) != 1:
        return {"ok": False, "reason": "hotkey_not_digit", "hotkey": hotkey}
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = int(getattr(window, "handle", 0) or 0)
        if not hwnd:
            return {"ok": False, "reason": "no_window_handle"}
        if user32.GetForegroundWindow() != hwnd:
            pre = _bring_panel_foreground(window)
            if not pre.get("ok"):
                return {"ok": False, "reason": "foreground_failed",
                        "detail": pre}

        VK_MENU = 0x12
        VK_KEY = 0x30 + int(key)          # '1'..'9'
        KEYEVENTF_KEYUP = 0x0002
        INPUT_KEYBOARD = 1

        class _KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [("ki", _KEYBDINPUT), ("padding", ctypes.c_byte * 32)]

        class _INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]

        def _key(vk: int, up: bool = False) -> _INPUT:
            inp = _INPUT()
            inp.type = INPUT_KEYBOARD
            inp.union.ki = _KEYBDINPUT(vk, 0,
                                       KEYEVENTF_KEYUP if up else 0, 0, None)
            return inp

        seq = [_key(VK_MENU), _key(VK_KEY), _key(VK_KEY, up=True),
               _key(VK_MENU, up=True)]
        arr = (_INPUT * len(seq))(*seq)
        sent = user32.SendInput(len(seq), arr, ctypes.sizeof(_INPUT))
        note_self_injection()
        if sent != len(seq):
            return {"ok": False, "reason": "sendinput_failed", "sent": sent}
        return {"ok": True, "channel": CHANNEL_HOTKEY, "hotkey": key}
    except Exception as exc:  # noqa: BLE001 - 非 Windows/COM 缺席
        return {"ok": False, "reason": "hotkey_error",
                "error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- #
# 账号切换（R52 通道B：ComboBox 多锚 SelectionItem 选择路径）
# --------------------------------------------------------------------------- #

def normalize_account_items(names: list[str]) -> list[str]:
    """下拉列表项名清洗（纯函数，可测）：去重（UIA 多桥重复暴露同名项）
    + 剔除非账号项（编辑账户等）。保序取首现。"""
    out: list[str] = []
    for raw in (names or []):
        n = str(raw or "").strip()
        if not n or n in NON_ACCOUNT_ITEM_NAMES or n in out:
            continue
        out.append(n)
    return out

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

    def _combo_value(elem) -> str:
        unk = _safe(elem.GetCurrentPattern, UIAC.UIA_ValuePatternId)
        if unk is None:
            return ""
        vp = _safe(unk.QueryInterface, UIAC.IUIAutomationValuePattern)
        return str(_safe(vp.GetValue) or "") if vp is not None else ""

    def _find_combobox_multi(root_elem):
        """R52 多锚：aid=2322 主锚（R50 契约）→ Value 含「证券」的 ComboBox
        兜底锚（客户端改 aid 时仍可定位账号切换器；绝不模糊到非 ComboBox）。"""
        for elem, _d in _walk(root_elem):
            if (_prop(elem, "CurrentAutomationId", "") == COMBOBOX_AUTOMATION_ID
                    and int(_prop(elem, "CurrentControlType", 0) or 0)
                    == COMBOBOX_CONTROL_TYPE):
                return elem, COMBOBOX_AUTOMATION_ID
        for elem, _d in _walk(root_elem):
            if int(_prop(elem, "CurrentControlType", 0) or 0) \
                    != COMBOBOX_CONTROL_TYPE:
                continue
            text = str(_prop(elem, "CurrentName", "") or "") or _combo_value(elem)
            if "证券" in text:
                return elem, f"value:{text.strip()[:16]}"
        return None, ""

    def _find_combobox(root_elem):
        return _find_combobox_multi(root_elem)[0]

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
        """收集 SelectionItem 列表项 (name, elem, rect_key)：子树优先（不过滤）；
        全窗兜底扫描才按"横向与切换器重叠 + 纵向紧邻其下"过滤，防把别处
        列表项当账号。rect_key 供 (name, rect) 去重（UIA 多桥重复暴露）。"""
        items = []
        cl, ct, cr, cb = combo_rect or (None, None, None, None)
        for elem, _d in _walk(scope_elem):
            if int(_prop(elem, "CurrentControlType", 0) or 0) \
                    != LIST_ITEM_CONTROL_TYPE:
                continue
            if not _safe(elem.GetCurrentPropertyValue,
                         UIAC.UIA_IsSelectionItemPatternAvailablePropertyId):
                continue
            rect_key = ""
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
            try:
                rc = _prop(elem, "CurrentBoundingRectangle")
                rect_key = ",".join(str(getattr(rc, k, "")) for k in
                                    ("left", "top", "right", "bottom"))
            except Exception:  # noqa: BLE001 - rect 不可读 → 仅按名字去重
                rect_key = ""
            items.append((_item_name(elem), elem, rect_key))
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

    # 1) 定位账号切换器（aid=2322 主锚 + Value 兜底锚；均缺席=fail-closed）
    combo, anchor = _find_combobox_multi(root)
    if combo is None:
        return {"ok": False, "reason": "combobox_absent"}
    rect = _prop(combo, "CurrentBoundingRectangle")
    combo_rect = (getattr(rect, "left", None), getattr(rect, "top", None),
                  getattr(rect, "right", None), getattr(rect, "bottom", None))

    # 2) 展开下拉：ExpandCollapse 优先（语义通道），缺席走 DropDown 子按钮
    #    Invoke（勘察实证唯一展开通道）。封装为闭包——点击升级段需重展开
    #    取新鲜下拉（防下拉已闭合→盲点风险）。
    def _do_expand() -> str:
        via = ""
        if _safe(combo.GetCurrentPropertyValue,
                 UIAC.UIA_IsExpandCollapsePatternAvailablePropertyId):
            unk = _safe(combo.GetCurrentPattern, UIAC.UIA_ExpandCollapsePatternId)
            if unk:
                _safe(unk.QueryInterface(
                    UIAC.IUIAutomationExpandCollapsePattern).Expand)
                via = "expandcollapse"
        if not via:
            for elem, _d in _walk(combo, max_depth=2):
                if _prop(elem, "CurrentAutomationId", "") != DROPDOWN_CHILD_ID:
                    continue
                unk = _safe(elem.GetCurrentPattern, UIAC.UIA_InvokePatternId)
                if unk:
                    _safe(unk.QueryInterface(
                        UIAC.IUIAutomationInvokePattern).Invoke)
                    via = "dropdown_invoke"
                    break
        return via

    expand_via = _do_expand()
    if not expand_via:
        return {"ok": False, "reason": "expand_failed"}

    # 3) 列表项出现（展开有动画，轮询取新鲜树）→ 去重 → 文本唯一匹配。
    #    同样封装为闭包（点击升级段复用）。
    def _scan_match():
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
            triples = _collect_items(live_combo, combo_rect)
            scope_used = "combo_subtree"
            if not any(n for n, _e, _k in triples):
                triples = _collect_items(fresh_root, combo_rect, rect_filter=True)
                scope_used = "window_rect_filtered"
            # R52：同名同 rect 重复项去重（实机实证 UIA 多桥重复暴露同一列表
            # 项，不去重会把唯一账号误判成 ambiguous_match fail-closed）
            seen_keys: set[tuple[str, str]] = set()
            deduped: list[tuple[str, Any]] = []
            for n, e, k in triples:
                key = (n, k)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                deduped.append((n, e))
            # 非账号项剔除（编辑账户等——选它=打开账户管理对话框，绝不允许）
            pairs = [(n, e) for n, e in deduped
                     if n not in NON_ACCOUNT_ITEM_NAMES]
            names = [n for n, _e in pairs]
            idx, reason = match_account_item_names(names, expect_hint)
            if idx is not None:
                matched = pairs[idx]
                break
        return matched, names, scope_used, (reason if idx is None else "")

    matched, names, scope_used, reason = _scan_match()
    if matched is None:
        _collapse_best_effort()
        return {"ok": False, "reason": reason, "items": names,
                "expand": expand_via, "scope": scope_used, "anchor": anchor}

    matched_name, matched_elem = matched

    # 3b) 动作升级链（R52 实机实证：本客户端多桥暴露的列表项 SelectionItem
    # Select 是 no-op——顶栏 OCR 恒旧账号）。顺序：SelectionItem → Invoke →
    # 真实单击（仅 Owner 离席守卫通过时）。每步后快速 OCR 自证，翻到目标
    # 令牌即成功；最终权威自证在 _switch_account_and_verify 的完整轮询。

    def _read_label_quick() -> str:
        try:
            import os
            import tempfile

            from services.ths_account_bridge import parse_identity_label
            from services.ths_holdings_poll import _ocr_image, print_window_image

            img = print_window_image(int(hwnd))
            png = (Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
                   / "ths_hotswitch_quick.png")
            img.save(png)
            _items, lines = _ocr_image(Path(png))
            return parse_identity_label(lines)
        except Exception:  # noqa: BLE001 - 快速自证读失败按空处理
            return ""

    def _quick_flip(reads: int) -> bool:
        for _ in range(reads):
            time.sleep(SWITCH_WAIT_SLEEP_S)
            if expect_hint and expect_hint in _read_label_quick():
                return True
        return False

    def _owner_active() -> bool:
        try:
            import ctypes

            if ctypes.windll.user32.GetForegroundWindow() != int(hwnd):
                return False
        except Exception:  # noqa: BLE001 - 判不了按有人在用处理
            return True
        return _owner_idle_seconds() < OWNER_ACTIVE_IDLE_S

    def _item_invoke(elem) -> bool:
        unk = _safe(elem.GetCurrentPattern, UIAC.UIA_InvokePatternId)
        if unk is None:
            return False
        ip = _safe(unk.QueryInterface, UIAC.IUIAutomationInvokePattern)
        if ip is None:
            return False
        _safe(ip.Invoke)
        return True

    def _item_click(elem) -> bool:
        """真实单击账号列表项中心（坐标严格取自该项 BoundingRectangle；
        调用前必须已过 Owner 离席守卫）。"""
        try:
            import ctypes

            rect = _prop(elem, "CurrentBoundingRectangle")
            x = (int(getattr(rect, "left", 0)) + int(getattr(rect, "right", 0))) // 2
            y = (int(getattr(rect, "top", 0)) + int(getattr(rect, "bottom", 0))) // 2
            if x <= 0 or y <= 0:
                return False
            user32 = ctypes.windll.user32
            user32.SetCursorPos(x, y)
            time.sleep(0.08)
            user32.mouse_event(0x0002, 0, 0, 0, 0)   # LEFTDOWN
            user32.mouse_event(0x0004, 0, 0, 0, 0)   # LEFTUP
            note_self_injection()
            return True
        except Exception:  # noqa: BLE001
            return False

    actions: list[str] = []
    try:
        unk = matched_elem.GetCurrentPattern(UIAC.UIA_SelectionItemPatternId)
        sp = unk.QueryInterface(UIAC.IUIAutomationSelectionItemPattern)
        sp.Select()
        actions.append("selectionitem")
        if _quick_flip(reads=2):
            return {"ok": True, "matched": matched_name, "items": names,
                    "expand": expand_via, "scope": scope_used,
                    "anchor": anchor, "actions": actions}
        if _item_invoke(matched_elem):
            actions.append("invoke")
            if _quick_flip(reads=2):
                return {"ok": True, "matched": matched_name, "items": names,
                        "expand": expand_via, "scope": scope_used,
                        "anchor": anchor, "actions": actions}
        # 真实单击兜底：重展开取新鲜下拉+新鲜列表项（点击紧随扫描，防
        # 下拉已闭合→盲点误击其他控件），且必须再次通过 Owner 离席守卫。
        fresh_via = _do_expand()
        fresh, _names2, _scope2, _reason2 = _scan_match()
        if fresh is not None and fresh_via and not _owner_active():
            if _item_click(fresh):
                actions.append("click")
                if _quick_flip(reads=3):
                    return {"ok": True, "matched": matched_name,
                            "items": names, "expand": expand_via,
                            "scope": scope_used, "anchor": anchor,
                            "actions": actions}
    except Exception as exc:  # noqa: BLE001
        _collapse_best_effort()
        return {"ok": False, "reason": "select_error",
                "error": f"{type(exc).__name__}: {exc}",
                "items": names, "matched": matched_name,
                "expand": expand_via, "scope": scope_used, "anchor": anchor,
                "actions": actions}
    _collapse_best_effort()
    return {"ok": False, "reason": "select_not_committed",
            "items": names, "matched": matched_name,
            "expand": expand_via, "scope": scope_used, "anchor": anchor,
            "actions": actions}


def _verify_target_label(window: Any, expect_hint: str,
                         attempts: Optional[int] = None) -> dict[str, Any]:
    """切换动作后的 OCR 身份自证轮询（动作只是"已发出"，读到目标令牌才算数）。"""
    last_label = ""
    n = int(attempts if attempts is not None else SWITCH_WAIT_ATTEMPTS)
    for _ in range(n):
        time.sleep(SWITCH_WAIT_SLEEP_S)
        try:
            last_label, _png = read_window_identity(window)
        except Exception:  # noqa: BLE101 - 切换期窗口重绘，读失败重试
            continue
        if expect_hint and expect_hint in last_label:
            return {"ok": True, "identity_label": last_label}
    return {"ok": False, "reason": "verify_failed",
            "identity_label": last_label, "attempts": n}


def _switch_account_and_verify(window: Any, expect_hint: str,
                               cfg: Optional[dict[str, Any]] = None
                               ) -> dict[str, Any]:
    """R52 通道编排：按 cfg["channels"]（默认 热键→ComboBox）逐通道尝试，
    任一通道发出且 OCR 身份自证读到目标令牌即成功。

    返回 {ok, identity_label?, channel?, reason?, attempts[]}。
    单通道预失败时 reason 原样透传该通道 reason（排障面兼容 R49c）；
    多通道全败 → reason=all_channels_failed + attempts 明细。"""
    cfg = cfg or {}
    channels = tuple(cfg.get("channels") or DEFAULT_CHANNELS)
    attempts: list[dict[str, Any]] = []
    hwnd = int(getattr(window, "handle", 0) or 0)
    for channel in channels:
        res: dict[str, Any]
        verify_reads: int = SWITCH_WAIT_ATTEMPTS
        if channel == CHANNEL_HOTKEY:
            hotkey = str(cfg.get("target_hotkey") or "").strip()
            if not hotkey:
                attempts.append({"channel": channel, "ok": False,
                                 "reason": "no_hotkey_binding"})
                continue
            try:
                res = switch_account_via_hotkey(window, hotkey)
            except Exception as exc:  # noqa: BLE001 - 键击层异常 → 下一通道
                res = {"ok": False, "reason": "hotkey_error",
                       "error": f"{type(exc).__name__}: {exc}"}
            if not res.get("ok"):
                attempts.append({"channel": channel, "ok": False,
                                 **{k: v for k, v in res.items()
                                    if k != "ok"}})
                continue
            # 热键通道快速判死（实机实证 Alt+N 恒无效果——3 读即放弃）
            verify_reads = SWITCH_WAIT_ATTEMPTS_HOTKEY
        elif channel == CHANNEL_COMBOBOX:
            try:
                sel = _uia_select_account(hwnd, expect_hint)
            except Exception as exc:  # noqa: BLE001 - comtypes 缺失/COM 异常
                attempts.append({"channel": channel, "ok": False,
                                 "reason": "uia_error",
                                 "error": f"{type(exc).__name__}: {exc}"})
                continue
            if not sel.get("ok"):
                attempts.append({"channel": channel, "ok": False,
                                 "reason": sel.get("reason",
                                                   "uia_select_failed"),
                                 "detail": {k: v for k, v in sel.items()
                                            if k != "ok"}})
                continue
            res = {"ok": True, "matched": sel.get("matched")}
        else:
            attempts.append({"channel": channel, "ok": False,
                             "reason": "unknown_channel"})
            continue
        verified = _verify_target_label(window, expect_hint,
                                        attempts=verify_reads)
        if verified.get("ok"):
            return {"ok": True, "identity_label": verified["identity_label"],
                    "channel": channel,
                    "selected_item": res.get("matched"),
                    "attempts": attempts}
        attempts.append({"channel": channel, "ok": False,
                         "reason": "verify_failed",
                         "identity_label": verified.get("identity_label")})
    if len(attempts) == 1:
        fail = attempts[0]
        detail = {k: v for k, v in fail.items()
                  if k not in ("ok", "reason", "channel")}
        inner = detail.pop("detail", None) or {}
        detail.update(inner)  # 通道 B 明细（items/expand/anchor）扁平上浮
        return {"ok": False, "reason": fail.get("reason"),
                "identity_label": str(fail.get("identity_label") or ""),
                "attempts": attempts, "detail": detail}
    return {"ok": False, "reason": "all_channels_failed", "identity_label": "",
            "attempts": attempts}


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
    """确保面板显示目标账号；不在则按通道链切换并自证。

    R52 铁律前置：面板在前台且用户输入空闲 < OWNER_ACTIVE_IDLE_S
    （Owner 手在面板上）→ skipped_foreground 终态，绝不抢。
    cfg: {target_hint, known_hints, channels?, target_hotkey?, ...}
    返回 None=已在目标/切换自证通过；dict=fail-closed 终态
    （skipped_foreground / identity_mismatch / switch_failed），调用方
    原样上报、零采集。
    """
    _ensure_restored(window)
    label, png = read_window_identity(window)
    target = str(cfg.get("target_hint") or "")
    if target and target in label:
        return None  # 已在目标账号：零切换零交互
    if owner_using_panel(window):
        return {"status": STATUS_SKIPPED_FOREGROUND, "identity_label": label,
                "identity_hint": target, "screenshot": png,
                "note": "面板在前台且用户活跃（Owner 手在面板上），绝不切换"}
    if _match_known(label, cfg.get("known_hints") or []) is None:
        return {"status": STATUS_IDENTITY_MISMATCH, "identity_label": label,
                "identity_hint": target, "screenshot": png,
                "note": "面板登录身份未知（非绑定账号令牌），fail-closed 零切换"}
    res = _switch_account_and_verify(window, target, cfg)
    if not res.get("ok"):
        return {"status": STATUS_SWITCH_FAILED, "identity_label": res.get("identity_label"),
                "identity_hint": target, "reason": res.get("reason"),
                "attempts": res.get("attempts"), "screenshot": png,
                "note": f"账号切换通道全部未通过（reason={res.get('reason')}），fail-closed"}
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
        if owner_using_panel(window):
            return {"status": STATUS_SWITCHBACK_FAILED,
                    "reason": STATUS_SKIPPED_FOREGROUND,
                    "note": "面板在前台且用户活跃，切回让位（保留现场留痕）"}
        # home 的热键与目标账号热键不同（曾=1 / 王=2）：切回时换绑定
        restore_cfg = dict(cfg or {})
        restore_cfg["target_hotkey"] = str(cfg.get("home_hotkey")
                                           or cfg.get("target_hotkey") or "")
        res = _switch_account_and_verify(window, home, restore_cfg)
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
    R52：切换通道链 channels=[hotkey_alt_n, combobox_selection]（Alt+N
    复活为通道A——需前台+焦点；ComboBox 多锚 SelectionItem 为通道B），
    target_hotkey/home_hotkey 取注册表 hotkey 字段。
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
                "channels": [CHANNEL_HOTKEY, CHANNEL_COMBOBOX],  # R52 通道链
                "target_hotkey": str(rule.get("hotkey") or ""),
                "home_hotkey": (str(home_rule.get("hotkey") or "")
                                if home_restorable else ""),
                "switch_mode": "channels_r52",   # R52 机制标注
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
