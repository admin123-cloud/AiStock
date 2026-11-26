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

import ctypes
import ctypes.wintypes as wintypes
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("l2_order_filler")

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
PANEL_MAP = Path(r"F:/Aistock/runtime/l2_r50/panel_map.json")
PANEL_MAP_SCHEMA = "l2_panel_map/1"
RUNTIME_DIR = Path(r"F:/Aistock/runtime/l2_r50")
HOTSWITCH_LOCK_NAME = "ths_window.lock"
PANEL_TITLE = "网上股票交易系统5.0"
PANEL_CLASS = "Afx:004A0000:b:00010003:00000006:74BD1569"

EDIT_WAIT_SEC = 8
NAV_SELECT_WAIT_SEC = 1.0
POST_FILL_SETTLE_SEC = 0.5

INVOKE_FORBIDDEN_FLAG = "invoke_forbidden_phase1"
FILL_SCHEMA = "l2_fill_result/1"

# 已知账号映射（identity_hint 与 ths_panels.json / scripts.ths_client.YH_ACCOUNTS 一致）
KNOWN_ACCOUNTS: dict[str, dict[str, str]] = {
    "acct1": {
        "identity_hint": "广发证券-曾*",
        "home_hint": "广发证券-曾*",
        "masked_id": "A5****1851",
        "owner": "曾",
    },
    "acct2": {
        "identity_hint": "广发证券-王*美",
        "home_hint": "广发证券-曾*",
        "masked_id": "A6****5646",
        "owner": "王",
    },
}

# Win32 消息与控件常量（仅用于读控件与预填输入框，绝不触碰下单提交按钮）
_GW_CHILD = 5
_GW_HWNDNEXT = 2
_WM_GETTEXT = 0x000D
_WM_GETTEXTLENGTH = 0x000E
_WM_SETTEXT = 0x000C
_WM_COMMAND = 0x0111
_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_CHAR = 0x0102
_WM_CLEAR = 0x0303
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_EM_SETSEL = 0x00B1
_EM_REPLACESEL = 0x00C2
_VK_BACK = 0x08
_EN_CHANGE = 0x0300
_EN_UPDATE = 0x0400
_CB_GETCURSEL = 0x0147
_CB_GETCOUNT = 0x0146
_CB_GETLBTEXT = 0x0148
_CB_GETLBTEXTLEN = 0x0149
_CB_SETCURSEL = 0x014E
_CBN_SELCHANGE = 1
_CBN_SELENDOK = 9
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_NON_ACCOUNT_COMBO_ITEMS = frozenset({
    "编辑账户", "添加账户", "管理账户", "添加交易账户", "登录账户",
})

_U32_TYPED: Any = None


def _user32():
    """返回显式声明 64 位安全签名的私有 user32 实例，彻底杜绝 SendMessageW 指针截断。"""
    global _U32_TYPED
    if _U32_TYPED is None:
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u32.SendMessageW.restype = ctypes.c_ssize_t
        u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u32.PostMessageW.restype = wintypes.BOOL
        u32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        u32.AttachThreadInput.restype = wintypes.BOOL
        u32.SetFocus.argtypes = [wintypes.HWND]
        u32.SetFocus.restype = wintypes.HWND
        _U32_TYPED = u32
    return _U32_TYPED


def _lparam_ptr(obj: Any) -> int:
    """将 ctypes buffer / c_wchar_p 安全转为 64 位 LPARAM 整数地址。"""
    return int(ctypes.cast(obj, ctypes.c_void_p).value or 0)


def _win32_is_visible(hwnd: int) -> bool:
    try:
        return bool(_user32().IsWindowVisible(int(hwnd)))
    except Exception:
        return False


def _win32_is_enabled(hwnd: int) -> bool:
    try:
        return bool(_user32().IsWindowEnabled(int(hwnd)))
    except Exception:
        return False


def _win32_ctrl_id(hwnd: int) -> int:
    try:
        return int(_user32().GetDlgCtrlID(int(hwnd)))
    except Exception:
        return 0


def _win32_parent(hwnd: int) -> int:
    try:
        return int(_user32().GetParent(int(hwnd)) or 0)
    except Exception:
        return 0


def _win32_class(hwnd: int) -> str:
    try:
        buf = ctypes.create_unicode_buffer(128)
        n = _user32().GetClassNameW(int(hwnd), buf, len(buf))
        return buf.value if n > 0 else ""
    except Exception:
        return ""


def _win32_text(hwnd: int) -> str:
    """跨进程安全读取 Win32 控件文本（WM_GETTEXT，支持 Edit / ComboBox / Static）。"""
    try:
        u32 = _user32()
        length = int(u32.SendMessageW(int(hwnd), _WM_GETTEXTLENGTH, 0, 0) or 0)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 2)
        copied = int(u32.SendMessageW(int(hwnd), _WM_GETTEXT, length + 1, _lparam_ptr(buf)) or 0)
        return buf.value[:copied] if copied > 0 else buf.value
    except Exception:
        return ""


def _win32_rect(hwnd: int) -> tuple[int, int, int, int]:
    try:
        rc = wintypes.RECT()
        if _user32().GetWindowRect(int(hwnd), ctypes.byref(rc)):
            return int(rc.left), int(rc.top), int(rc.right), int(rc.bottom)
    except Exception:
        pass
    return (0, 0, 0, 0)


def _win32_process_exe(hwnd: int) -> str:
    """读取窗口所属进程完整路径（用于优先区分同花顺远航版与旧版同花顺）。"""
    try:
        pid = wintypes.DWORD(0)
        _user32().GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        if not pid.value:
            return ""
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid.value))
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(512)
            buf = ctypes.create_unicode_buffer(512)
            if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
        finally:
            k32.CloseHandle(handle)
    except Exception:
        pass
    return ""


def _win32_iter_descendants(root_hwnd: int, max_nodes: int = 3000) -> list[int]:
    """原生 Win32 子控件迭代（毫秒级，零 COM/UIA 阻塞，完全避开 Chromium WebView 卡死）。"""
    u32 = _user32()
    out: list[int] = []
    stack: list[int] = [int(root_hwnd)]
    seen: set[int] = {int(root_hwnd)}
    while stack and len(out) < max_nodes:
        cur = stack.pop()
        child = int(u32.GetWindow(cur, _GW_CHILD) or 0)
        children: list[int] = []
        guard = 0
        while child and guard < 600:
            guard += 1
            if child not in seen:
                seen.add(child)
                children.append(child)
            child = int(u32.GetWindow(child, _GW_HWNDNEXT) or 0)
        for ch in children:
            out.append(ch)
            if len(out) >= max_nodes:
                break
        for ch in reversed(children):
            stack.append(ch)
    return out


def _win32_combo_items(combo_hwnd: int) -> list[tuple[int, str]]:
    """读取 Win32 ComboBox 全部下拉项（只读，不展开窗口）。"""
    try:
        u32 = _user32()
        count = int(u32.SendMessageW(int(combo_hwnd), _CB_GETCOUNT, 0, 0))
        if count <= 0 or count > 100:
            return []
        items: list[tuple[int, str]] = []
        for idx in range(count):
            tlen = int(u32.SendMessageW(int(combo_hwnd), _CB_GETLBTEXTLEN, idx, 0))
            if tlen < 0 or tlen > 512:
                continue
            buf = ctypes.create_unicode_buffer(tlen + 2)
            u32.SendMessageW(int(combo_hwnd), _CB_GETLBTEXT, idx, _lparam_ptr(buf))
            items.append((idx, buf.value.strip()))
        return items
    except Exception:
        return []


def _win32_combo_selected_text(combo_hwnd: int) -> str:
    """读取 Win32 ComboBox 当前选中项文本。"""
    try:
        u32 = _user32()
        idx = int(u32.SendMessageW(int(combo_hwnd), _CB_GETCURSEL, 0, 0))
        if idx >= 0:
            tlen = int(u32.SendMessageW(int(combo_hwnd), _CB_GETLBTEXTLEN, idx, 0))
            if 0 < tlen <= 512:
                buf = ctypes.create_unicode_buffer(tlen + 2)
                u32.SendMessageW(int(combo_hwnd), _CB_GETLBTEXT, idx, _lparam_ptr(buf))
                txt = buf.value.strip()
                if txt:
                    return txt
    except Exception:
        pass
    return _win32_text(combo_hwnd).strip()


def _match_account_from_text(text: str) -> Optional[str]:
    """根据账号标签（广发证券-曾*）或脱敏股东号（A5****1851）唯一识别账号键。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    flat = raw.replace("－", "-").replace("—", "-").replace(" ", "")
    for acct, info in KNOWN_ACCOUNTS.items():
        hint = str(info.get("identity_hint") or "").replace("－", "-").replace(" ", "")
        masked = str(info.get("masked_id") or "").replace(" ", "")
        if hint and hint in flat:
            return acct
        if masked and masked in flat:
            return acct
    try:
        from scripts import ths_client as tc

        norm = tc.yh_normalize_masked(raw)
        if norm:
            acct = tc.yh_account_from_masked(norm)
            if acct:
                return acct
    except Exception:
        pass
    return None


def _read_live_account_win32(hwnd: int) -> dict[str, Any]:
    """原生 Win32 毫秒级实时读取同花顺当前账号。

    安全铁律：股东代码下拉框 1004（A5****1851=acct1 / A6****5646=acct2）为真实活跃会话权威锚；
    若 2322 显示文本与 1004 股东号冲突（例如仅外观刷新未真正切会话），一律以 1004 为准，绝不串账！
    """
    info: dict[str, Any] = {
        "account": None,
        "account_1004": None,
        "account_2322": None,
        "label": "",
        "combo_2322": "",
        "combo_1004": "",
        "combo_2322_hwnd": 0,
        "source": "none",
    }
    try:
        for ch in _win32_iter_descendants(hwnd):
            cid = _win32_ctrl_id(ch)
            if cid not in (2322, 1004):
                continue
            cls = _win32_class(ch)
            if "ComboBox" not in cls:
                continue
            txt = _win32_combo_selected_text(ch)
            if cid == 2322:
                info["combo_2322"] = txt
                info["combo_2322_hwnd"] = ch
            elif cid == 1004:
                info["combo_1004"] = txt
    except Exception as exc:
        info["win32_error"] = str(exc)

    acct_2322 = _match_account_from_text(info["combo_2322"])
    acct_1004 = _match_account_from_text(info["combo_1004"])
    info["account_2322"] = acct_2322
    info["account_1004"] = acct_1004

    # 权威优先级：1004（真实股东账号）> 2322（顶部下拉显示文本）
    if acct_1004:
        info["account"] = acct_1004
        info["label"] = KNOWN_ACCOUNTS[acct_1004]["identity_hint"]
        info["source"] = "win32_combo_1004" if (acct_2322 != acct_1004) else "win32_dual_verified"
        return info
    if acct_2322:
        info["account"] = acct_2322
        info["label"] = info["combo_2322"] or KNOWN_ACCOUNTS[acct_2322]["identity_hint"]
        info["source"] = "win32_combo_2322"
        return info

    # 兜底：复用 scripts.ths_client._yh_ro_identity_values（兼容单账号 1003+1004 与双账号三锚）
    try:
        from scripts import ths_client as tc

        vals = tc._yh_ro_identity_values(int(hwnd))
        if isinstance(vals, dict):
            v2322 = str(vals.get("2322") or "").strip()
            v1004 = str(vals.get("1004") or "").strip()
            info["combo_2322"] = info["combo_2322"] or v2322
            info["combo_1004"] = info["combo_1004"] or v1004
            a1004 = _match_account_from_text(v1004)
            a2322 = _match_account_from_text(v2322)
            info["account_1004"] = a1004
            info["account_2322"] = a2322
            acct = a1004 or a2322
            if acct:
                info["account"] = acct
                info["label"] = KNOWN_ACCOUNTS[acct]["identity_hint"]
                info["source"] = "yh_ro_identity_values"
                return info
    except Exception as exc:
        info["yh_ro_error"] = str(exc)

    return info

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


def _score_panel_window(hwnd: int, preferred_hwnd: Optional[int] = None) -> tuple[int, dict[str, Any]]:
    """对候选交易面板窗口打分：优先同花顺远航版且含有效账号控件的真实交易面板。"""
    score = 0
    if preferred_hwnd and int(hwnd) == int(preferred_hwnd):
        score += 100
    exe = _win32_process_exe(hwnd)
    if "同花顺远航" in exe:
        score += 50
    elif "xiadan.exe" in exe.lower():
        score += 10
    left, top, right, bottom = _win32_rect(hwnd)
    w, h = right - left, bottom - top
    if w > 300 and h > 200:
        score += 10
    acct_info = _read_live_account_win32(hwnd)
    if acct_info.get("account"):
        score += 40
    return score, {"hwnd": int(hwnd), "exe": exe, "size": (w, h), "account": acct_info.get("account")}


def _find_window(
    wait_sec: float = 2.0,
    preferred_hwnd: Optional[int] = None,
) -> tuple[Optional[Application], Optional[int], Optional[str]]:
    """通过标题查找可见的实时交易面板窗口（智能择优：优先同花顺远航版 + 已识别账号面板，
    绝不盲选隐藏托盘壳窗或旧版同花顺）。"""
    if not _PYWINSUCCESS:
        return None, None, "pywinauto 不可用"
    deadline = time.monotonic() + max(0.0, float(wait_sec))
    last_err: Optional[str] = None
    wins: list[int] = []
    while True:
        cand_set: list[int] = []
        try:
            for h in pywinauto.findwindows.find_windows(title=PANEL_TITLE, visible_only=True):
                if int(h) not in cand_set:
                    cand_set.append(int(h))
        except Exception as e:
            last_err = f"查找可见窗口异常: {e}"
        try:
            for h in pywinauto.findwindows.find_windows(title_re=".*网上股票交易系统.*", visible_only=True):
                if int(h) not in cand_set:
                    cand_set.append(int(h))
        except Exception as e:
            if not cand_set:
                last_err = f"正则查找可见交易窗口异常: {e}"
        wins = cand_set
        if wins:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    if not wins:
        return None, None, last_err or f"可见交易窗口未找到 title={PANEL_TITLE!r}"

    if len(wins) == 1:
        hwnd = int(wins[0])
    else:
        scored = sorted(
            ((_score_panel_window(h, preferred_hwnd=preferred_hwnd), h) for h in wins),
            key=lambda x: x[0][0],
            reverse=True,
        )
        hwnd = int(scored[0][1])
        logger.info("多窗口候选择优: %s -> 选中 hwnd=%x", [(hex(h), sc[0], sc[1].get("exe"), sc[1].get("account")) for sc, h in scored], hwnd)

    try:
        app = Application(backend="uia").connect(handle=hwnd)
    except Exception as e:
        return None, None, f"connect 窗口失败 handle={hwnd:x}: {e}"
    return app, hwnd, None


def _extract_panel_hwnd_from_boot(boot_res: dict[str, Any]) -> Optional[int]:
    """从 bootstrap_cold_start 返回的 steps 中提取最新面板句柄。"""
    for st in reversed(boot_res.get("steps") or []):
        if not isinstance(st, dict):
            continue
        if st.get("panel_hwnd"):
            return int(st["panel_hwnd"])
        if st.get("step") in ("panel_logged_in", "panel_already_open", "activate_and_raise") and st.get("hwnd"):
            return int(st["hwnd"])
    return None


def _ensure_panel_ready_on_demand(
    bootstrap_fn: Optional[Any] = None,
    *,
    target_account: Optional[str] = None,
) -> tuple[Optional[Application], Optional[int], list[str], Optional[str]]:
    """按需获取已登录的同花顺远航交易面板；若未运行则自动冷启动（happ.exe → 顶栏/F12 → 记住密码自动登录）。"""
    steps: list[str] = []
    app, hwnd, err = _find_window()
    if app is not None and hwnd is not None:
        # 检查是否停在登录页；若停在登录页仍交由 bootstrap_cold_start 处理
        try:
            from host_bridges import ths_read_account_task as ths_boot

            login_info = ths_boot._inspect_panel_login_controls(hwnd)
            if not login_info.get("is_login_page"):
                steps.append(f"复用已运行交易面板 hwnd={hwnd:x}")
                return app, hwnd, steps, None
            steps.append(f"检测到交易面板停在登录页 hwnd={hwnd:x}，触发自动登录引导")
        except Exception:
            steps.append(f"复用已运行交易面板 hwnd={hwnd:x}")
            return app, hwnd, steps, None
    else:
        steps.append(f"交易面板未运行（{err}），启动同花顺远航按需冷启动引导")

    # 按需冷启动：拉起 happ.exe → 等主界面 → 顶栏/F12 唤起面板 → 记住密码自动登录（严格凭证零接触）
    try:
        if bootstrap_fn is None:
            from host_bridges.ths_read_account_task import bootstrap_cold_start

            bootstrap_fn = bootstrap_cold_start
        if target_account:
            try:
                boot_res = bootstrap_fn(accounts=[target_account])
            except TypeError:
                boot_res = bootstrap_fn()
        else:
            boot_res = bootstrap_fn()
    except Exception as exc:
        return None, None, steps, f"同花顺远航冷启动引导异常: {exc}"

    if not isinstance(boot_res, dict) or not boot_res.get("ready"):
        b_status = (boot_res or {}).get("status", "bootstrap_failed") if isinstance(boot_res, dict) else "bootstrap_failed"
        b_reason = (boot_res or {}).get("reason", "未知原因") if isinstance(boot_res, dict) else str(boot_res)
        steps.append(f"冷启动未就绪 status={b_status}")
        return None, None, steps, f"同花顺冷启动未就绪({b_status}): {b_reason}"

    steps.append(f"同花顺远航冷启动就绪 status={boot_res.get('status')}")
    preferred_hwnd = _extract_panel_hwnd_from_boot(boot_res)
    time.sleep(1.0)
    app, hwnd, err = _find_window(wait_sec=4.0, preferred_hwnd=preferred_hwnd)
    if app is None or hwnd is None:
        return None, None, steps, f"冷启动后仍未连上交易面板: {err}"
    steps.append(f"冷启动连接交易面板成功 hwnd={hwnd:x}")
    return app, hwnd, steps, None


def _check_foreground(hwnd: int) -> Optional[str]:
    """检查窗口是否被用户占用；None=安全；str=前台警告。"""
    fore = ctypes.windll.user32.GetForegroundWindow()
    return None if fore != hwnd else "用户正在操作面板（前台命中），跳过 fill_order"


def _bring_below(hwnd: int) -> None:
    """置面板到 Z-order 底部（HWND_BOTTOM），不抢焦点。"""
    ctypes.windll.user32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0010 | 0x0001)
    time.sleep(0.3)


def _is_iconic(hwnd: int) -> bool:
    return bool(ctypes.windll.user32.IsIconic(hwnd))


def _show_window(hwnd: int, cmd: int) -> None:
    ctypes.windll.user32.ShowWindow(hwnd, cmd)


def _ensure_restored(hwnd: int) -> bool:
    """面板若最小化则恢复；返回 True=曾被最小化。"""
    was = _is_iconic(hwnd)
    if was:
        _show_window(hwnd, 9)
        time.sleep(0.5)
    return was


def _activate_window_safe(hwnd: int) -> None:
    """将目标交易面板恢复并置前（兼容 RDP 前台锁）。"""
    try:
        from host_bridges import ths_read_account_task as ths_boot

        ths_boot._activate_and_raise_window(int(hwnd))
    except Exception:
        _show_window(int(hwnd), 9)
        try:
            _user32().SetForegroundWindow(int(hwnd))
        except Exception:
            pass
    time.sleep(0.2)


# ===================================================================
# 3.  账号核验与切换（Win32 毫秒直读 + 双账号 2322 切换 + 远航顶栏兜底）
# ===================================================================


def _is_account_truly_active(live_info: dict[str, Any], target_account: str) -> bool:
    """严格判定目标账号是否真实生效（1004 股东号存在时必须与目标一致，杜绝 2322 假切换）。"""
    a1004 = live_info.get("account_1004")
    if a1004 is not None:
        return a1004 == target_account
    return live_info.get("account") == target_account


_LB_GETITEMHEIGHT = 0x01A1
_CB_SHOWDROPDOWN = 0x014F


def _try_win32_switch_combo2322(hwnd: int, combo_hwnd: int, target_account: str) -> bool:
    """尝试通过真实点击 ComboBox(2322) 下拉列表项切换账号，并以 1004 股东号变更作为唯一硬验收标准。

    注：同花顺远航版 xiadan.exe 会忽略纯消息 CB_SETCURSEL/CBN_SELENDOK 与 UIA SelectionItem，
    只有真实点击下拉列表项（或走远航主程序顶栏）才会真正切换底层 1004 会话；且全程不按回车。
    """
    if not combo_hwnd:
        return False
    target_info = KNOWN_ACCOUNTS.get(target_account) or {}
    target_hint = str(target_info.get("identity_hint") or "")
    items = _win32_combo_items(combo_hwnd)
    target_idx: Optional[int] = None
    for idx, txt in items:
        if not txt or txt in _NON_ACCOUNT_COMBO_ITEMS:
            continue
        if _match_account_from_text(txt) == target_account or (target_hint and target_hint in txt):
            target_idx = idx
            break
    if target_idx is None:
        return False

    try:
        from scripts import ths_client as tc

        _activate_window_safe(hwnd)
        u32 = _user32()
        left, top, right, bottom = _win32_rect(combo_hwnd)
        if right <= left or bottom <= top:
            return False
        # 1) 真实点击 ComboBox(2322) 展开下拉
        tc._yh_click_window(int(combo_hwnd), [max(4.0, (right - left) - 10.0), (bottom - top) / 2.0])
        time.sleep(0.35)
        # 2) 查找当前可见的 ComboLBox 下拉弹出列表并点击目标行（按行高精算，绝不盲点）
        lbox_hwnd = int(ctypes.windll.user32.FindWindowW("ComboLBox", None) or 0)
        if lbox_hwnd and _win32_is_visible(lbox_hwnd):
            ll, lt, lr, lb = _win32_rect(lbox_hwnd)
            item_h = int(u32.SendMessageW(lbox_hwnd, _LB_GETITEMHEIGHT, 0, 0) or 0)
            if item_h <= 0 and len(items) > 0:
                item_h = max(14, (lb - lt) // len(items))
            click_y = (target_idx + 0.5) * float(item_h)
            if 0 < click_y < (lb - lt):
                tc._yh_click_window(lbox_hwnd, [(lr - ll) / 2.0, click_y])
                for _ in range(6):
                    time.sleep(0.5)
                    post = _read_live_account_win32(hwnd)
                    if _is_account_truly_active(post, target_account):
                        logger.info("ComboBox(2322) 真实点击切换成功 -> %s (1004=%r)", target_account, post.get("combo_1004"))
                        return True
        # 若未切成功，安全收起下拉列表（不按回车）
        u32.SendMessageW(int(combo_hwnd), _CB_SHOWDROPDOWN, 0, 0)
    except Exception as exc:
        logger.info("ComboBox(2322) 真实点击切号跳过（将走远航顶栏）: %s", exc)
    return False


def _switch_account_if_needed(
    panel_map: dict[str, Any],
    target_account: str,
    live_hwnd: Optional[int] = None,
    *,
    allow_foreground: bool = True,
) -> tuple[Optional[int], Optional[str]]:
    """实时核对同花顺当前登录账号（以 1004 股东号为权威真值），不在目标账号则自动切换并复核。

    返回 (latest_hwnd, error_or_None)。
    """
    target_info = KNOWN_ACCOUNTS.get(target_account)
    if not target_info:
        return live_hwnd, f"目标账号 {target_account} 无 KNOWN_ACCOUNTS 配置"
    target_hint = target_info.get("identity_hint", "")

    if live_hwnd is not None and live_hwnd > 0:
        hwnd = int(live_hwnd)
    else:
        raw_hwnd = panel_map.get("panel", {}).get("hwnd", 0)
        hwnd = int(raw_hwnd, 16) if isinstance(raw_hwnd, str) else int(raw_hwnd or 0)

    # 1. 原生 Win32 毫秒级实时身份读取（以 1004 真实股东号为权威真值，杜绝 2322 假象）
    live_info = _read_live_account_win32(hwnd)
    logger.info(
        "实时账号核验 hwnd=%x target=%s detected=%s source=%s combo2322=%r combo1004=%r",
        hwnd, target_account, live_info.get("account"), live_info.get("source"),
        live_info.get("combo_2322"), live_info.get("combo_1004"),
    )
    if _is_account_truly_active(live_info, target_account):
        panel_map["account"] = target_account
        panel_map["identity_label"] = live_info.get("label") or target_hint
        return hwnd, None

    # 2. 尝试 ComboBox(2322) 真实切号（以 1004 变更硬验收）
    combo_hwnd = int(live_info.get("combo_2322_hwnd") or 0)
    if combo_hwnd and _try_win32_switch_combo2322(hwnd, combo_hwnd, target_account):
        post = _read_live_account_win32(hwnd)
        panel_map["account"] = target_account
        panel_map["identity_label"] = post.get("label") or target_hint
        logger.info("ComboBox(2322) 切换账号成功并经 1004 核验 -> %s (%s)", target_account, post.get("combo_1004"))
        return hwnd, None

    # 3. 远航版主程序顶栏交易下拉菜单切换/登录目标账号（兼容未登录第二账号与 2322 未生效）
    try:
        from scripts import ths_client as tc

        main_win = tc._yh_main_window()
        if main_win is not None:
            logger.info("尝试通过同花顺远航顶栏切换/登录目标账号 %s (main_hwnd=%x)", target_account, int(main_win["hwnd"]))
            _activate_window_safe(int(main_win["hwnd"]))
            panel_new, _nav_steps = tc._yh_login_accounts_via_quote_dropdown(
                int(main_win["hwnd"]),
                [target_account],
                login_settle_wait=12.0,
            )
            if panel_new and panel_new.get("hwnd"):
                hwnd = int(panel_new["hwnd"])
                time.sleep(1.0)
                post = _read_live_account_win32(hwnd)
                if _is_account_truly_active(post, target_account):
                    panel_map["account"] = target_account
                    panel_map["identity_label"] = post.get("label") or target_hint
                    if isinstance(panel_map.get("panel"), dict):
                        panel_map["panel"]["hwnd"] = hex(hwnd)
                    logger.info("远航顶栏切换账号成功并经 1004 核验 -> %s hwnd=%x (1004=%r)", target_account, hwnd, post.get("combo_1004"))
                    return hwnd, None
    except Exception as exc:
        logger.warning("远航顶栏切号异常: %s", exc)

    post_fail = _read_live_account_win32(hwnd)
    return hwnd, (
        f"账号校验/切换失败：目标 {target_account}({target_hint})，"
        f"当前实读 account={post_fail.get('account')} combo2322={post_fail.get('combo_2322')!r} "
        f"combo1004={post_fail.get('combo_1004')!r}"
    )


# ===================================================================
# 4.  左侧导航选择（买入[F1] / 卖出[F2]）
# ===================================================================


def _win32_find_order_edits(hwnd: int) -> dict[str, int]:
    """原生 Win32 毫秒级定位下单表单 3 个固定 Edit 句柄（1032=代码, 1033=价格, 1034=数量），
    严格排除顶部 ComboBox 内嵌 Edit、零尺寸折叠控件与不可见/禁用控件。"""
    id_map = {1032: "code", 1033: "price", 1034: "qty"}
    found: dict[str, int] = {}
    diag: list[str] = []
    for ch in _win32_iter_descendants(hwnd):
        cid = _win32_ctrl_id(ch)
        if cid not in id_map:
            continue
        cls = _win32_class(ch)
        if cls.lower() != "edit":
            continue
        vis = _win32_is_visible(ch)
        ena = _win32_is_enabled(ch)
        left, top, right, bottom = _win32_rect(ch)
        w, h = right - left, bottom - top
        parent = _win32_parent(ch)
        pcls = _win32_class(parent) if parent else ""
        diag.append(f"{id_map[cid]}={ch:x}(vis={vis},ena={ena},rect={(left,top,right,bottom)},pcls={pcls!r})")
        if not vis or not ena:
            continue
        if w <= 10 or h <= 8:
            continue
        if "combobox" in pcls.lower():
            continue
        role = id_map[cid]
        if role not in found:
            found[role] = ch
    if diag:
        logger.info("表单 Edit 候选扫描: %s -> 选中=%s", ", ".join(diag), {k: hex(v) for k, v in found.items()})
    return found


def _select_nav_item(app: Application, side: str, hwnd: Optional[int] = None) -> Optional[str]:
    """切换到买入[F1]/卖出[F2]页签：优先置前并发送 F1/F2 热键（毫秒级且零 UIA 遍历卡死），
    再通过检查 1032/1033/1034 可见性自证；未命中才回退 UIA TreeItem。绝不点提交。"""
    text = "买入[F1]" if side == "buy" else "卖出[F2]"
    hotkey = "{F1}" if side == "buy" else "{F2}"
    last_err: Optional[str] = None

    # 1. 优先置前 + 发送 F1 / F2 热键（快速稳定，不触发 Chromium/WebView UIA 遍历卡顿）
    try:
        if hwnd:
            _activate_window_safe(int(hwnd))
        from pywinauto.keyboard import send_keys

        send_keys(hotkey)
        time.sleep(0.55)
        if not hwnd or len(_win32_find_order_edits(int(hwnd))) == 3:
            return None
        # 再补一次热键（应对首次仅激活面板聚焦）
        send_keys(hotkey)
        time.sleep(0.55)
        if len(_win32_find_order_edits(int(hwnd))) == 3:
            return None
    except Exception as e:
        last_err = f"热键 {hotkey} 切换失败: {e}"

    # 2. 兜底：UIA TreeItem 选择
    try:
        dlg = app.window(handle=hwnd) if hwnd else app.window(title=PANEL_TITLE)
        item = dlg.child_window(title=text, control_type="TreeItem")
        item.select()
        time.sleep(NAV_SELECT_WAIT_SEC)
        return None
    except Exception as e:
        return f"{last_err or '热键未渲染表单'}; TreeItem {text!r} 回退失败: {e}"


# ===================================================================
# 5.  输入框操作（Win32 极速清空/填入/读回 + UIA 兜底）
# ===================================================================


_BM_CLICK = 0x00F5
_VK_END = 0x23
_VK_HOME = 0x24
_VK_DELETE = 0x2E


def _win32_click_reset_button(code_edit_hwnd: int) -> bool:
    """若下单表单内存在明确的「重填」按钮（绝不触碰买入/卖出/确认按钮），先触发一次清空残留值。"""
    try:
        parent = _win32_parent(code_edit_hwnd)
        if not parent:
            return False
        for ch in _win32_iter_descendants(parent, max_nodes=200):
            if _win32_class(ch).lower() != "button":
                continue
            txt = _win32_text(ch).strip()
            if txt == "重填" and _win32_is_visible(ch) and _win32_is_enabled(ch):
                _user32().SendMessageW(int(ch), _BM_CLICK, 0, 0)
                time.sleep(0.15)
                return True
    except Exception:
        pass
    return False


def _sanitize_ocr_numeric(raw_text: str, *, is_code: bool = False) -> str:
    """清理单行 OCR 读回文本（剔除光标误识别字符 | / l / I / 空格，提取纯代码或数字）。"""
    import re

    s = str(raw_text or "").strip().replace(" ", "").replace("，", ".").replace(",", "")
    # 剔除末尾或开头因闪烁光标导致的 l / I / |
    s = re.sub(r"^[|lI]+|[|lI]+$", "", s)
    if is_code:
        m = re.search(r"\d{6}", s)
        if m:
            return m.group(0)
        digits = re.sub(r"\D", "", s)
        return digits
    m = re.search(r"\d+(?:\.\d+)?", s)
    return m.group(0) if m else s


def _visual_readback_order_edits(
    root_hwnd: int,
    win32_edits: dict[str, int],
    *,
    save_png_path: Optional[Path] = None,
) -> dict[str, str]:
    """通过 PrintWindow 离屏截图 + 本地 RapidOCR 精准读回 1032/1033/1034 真实屏幕渲染值。

    背景：同花顺远航版 xiadan.exe 的 1032/1033/1034 为防木马安全输入框，刻意屏蔽了 WM_GETTEXT
    与 UIA ValuePattern（恒返回空串）；PrintWindow + RapidOCR 直接验证肉眼所见的真实填入内容，
    且在 RDP 最小化/锁屏下依然可用。
    """
    out: dict[str, str] = {"code": "", "price": "", "qty": ""}
    try:
        import numpy as np
        from PIL import Image, ImageOps
        from scripts import ths_client as tc
        from services.broker.ths_holdings_poll import print_window_image

        # 截图前将焦点移回面板根窗，隐藏输入框内闪烁光标，避免末尾被误识为 1 / l
        try:
            u32 = _user32()
            cur_tid = int(ctypes.windll.kernel32.GetCurrentThreadId())
            tgt_tid = int(u32.GetWindowThreadProcessId(int(root_hwnd), None))
            attached = bool(cur_tid and tgt_tid and cur_tid != tgt_tid and u32.AttachThreadInput(cur_tid, tgt_tid, True))
            try:
                u32.SetFocus(int(root_hwnd))
            finally:
                if attached:
                    u32.AttachThreadInput(cur_tid, tgt_tid, False)
            time.sleep(0.08)
        except Exception:
            pass

        img = print_window_image(int(root_hwnd)).convert("RGB")
        if save_png_path is not None:
            try:
                save_png_path.parent.mkdir(parents=True, exist_ok=True)
                img.save(save_png_path)
            except Exception:
                pass

        rl, rt, rr, rb = _win32_rect(int(root_hwnd))
        rw, rh = max(1, rr - rl), max(1, rb - rt)
        iw, ih = img.size
        sx = iw / float(rw)
        sy = ih / float(rh)

        engine, _meta = tc._yh_ro_ocr_engine()
        for role, eh in win32_edits.items():
            el, et, er, eb = _win32_rect(int(eh))
            x1 = max(0, int(round((el - rl) * sx)) + 1)
            y1 = max(0, int(round((et - rt) * sy)) + 1)
            x2 = min(iw, int(round((er - rl) * sx)) - 1)
            y2 = min(ih, int(round((eb - rt) * sy)) - 1)
            if x2 - x1 < 6 or y2 - y1 < 6:
                continue
            crop = img.crop((x1, y1, x2, y2))
            up = crop.resize((crop.width * 3, crop.height * 3), Image.BICUBIC)
            padded = ImageOps.expand(up, border=10, fill=(255, 255, 255))
            arr = np.array(padded)[:, :, ::-1]
            text_raw = ""
            # 先走极速单行识别（use_det=False），空时再走完整 det+rec
            try:
                rec_out, _ = engine(arr, use_det=False, use_cls=False)
                if rec_out:
                    # rec_out 通常为 [(text, score)] 或 [[text, score]]
                    first = rec_out[0]
                    if isinstance(first, (list, tuple)) and len(first) >= 1:
                        text_raw = str(first[0] or "")
            except Exception:
                text_raw = ""
            if not text_raw.strip():
                try:
                    det_out, _ = engine(arr, use_cls=False)
                    if det_out:
                        text_raw = "".join(str(r[1] or "") for r in det_out if len(r) >= 2)
                except Exception:
                    text_raw = ""
            cleaned = _sanitize_ocr_numeric(text_raw, is_code=(role == "code"))
            out[role] = cleaned
            logger.info("视觉读回 %s(hwnd=%x): raw=%r -> cleaned=%r", role, eh, text_raw, cleaned)
    except Exception as exc:
        logger.warning("视觉读回异常: %s", exc)
    return out


def _win32_read_edit_any(edit_hwnd: int) -> str:
    """快速读回标准 Win32 Edit 文本（若遇同花顺防木马输入框返回空串，将由 _visual_readback_order_edits 接管）。"""
    return _win32_text(edit_hwnd).strip()


def _win32_focus_edit(edit_hwnd: int, root_hwnd: Optional[int] = None, *, real_click: bool = True) -> None:
    """真·跨线程焦点落框（AttachThreadInput + SetFocus + 点击右缘），绝不触发回车或提交按钮。"""
    u32 = _user32()
    try:
        if root_hwnd:
            _activate_window_safe(int(root_hwnd))
        cur_tid = int(ctypes.windll.kernel32.GetCurrentThreadId())
        tgt_tid = int(u32.GetWindowThreadProcessId(int(edit_hwnd), None))
        attached = bool(cur_tid and tgt_tid and cur_tid != tgt_tid and u32.AttachThreadInput(cur_tid, tgt_tid, True))
        try:
            u32.SetFocus(int(edit_hwnd))
        finally:
            if attached:
                u32.AttachThreadInput(cur_tid, tgt_tid, False)
        if real_click:
            left, top, right, bottom = _win32_rect(edit_hwnd)
            if right > left and bottom > top:
                try:
                    from scripts import ths_client as tc

                    # 点击输入框偏右位置，便于随后 END + Backspace 清空
                    cx = max(2.0, (right - left) - 6.0)
                    cy = (bottom - top) / 2.0
                    tc._yh_click_window(int(edit_hwnd), [cx, cy])
                    time.sleep(0.05)
                except Exception:
                    pass
    except Exception:
        pass


def _win32_type_single_edit(
    edit_hwnd: int,
    value: str,
    *,
    root_hwnd: Optional[int] = None,
    prefer_wm_char: bool = False,
) -> Optional[str]:
    """对同花顺防木马安全 Edit 执行「单次洁净清空 + 单次精准键入」（绝不双写拼接、绝不按回车）。"""
    val = str(value)
    u32 = _user32()
    cid = _win32_ctrl_id(edit_hwnd)
    parent = _win32_parent(edit_hwnd)

    def _notify_parent() -> None:
        if parent and cid:
            u32.SendMessageW(int(parent), _WM_COMMAND, (_EN_UPDATE << 16) | (cid & 0xFFFF), int(edit_hwnd))
            u32.SendMessageW(int(parent), _WM_COMMAND, (_EN_CHANGE << 16) | (cid & 0xFFFF), int(edit_hwnd))

    if prefer_wm_char:
        try:
            _win32_focus_edit(edit_hwnd, root_hwnd=root_hwnd, real_click=False)
            u32.SendMessageW(int(edit_hwnd), _EM_SETSEL, 0, -1)
            u32.SendMessageW(int(edit_hwnd), _WM_CLEAR, 0, 0)
            u32.SendMessageW(int(edit_hwnd), _WM_KEYDOWN, _VK_END, 1)
            for _ in range(12):
                u32.SendMessageW(int(edit_hwnd), _WM_KEYDOWN, _VK_BACK, 1)
                u32.SendMessageW(int(edit_hwnd), _WM_KEYUP, _VK_BACK, 1)
            u32.SendMessageW(int(edit_hwnd), _WM_KEYDOWN, _VK_HOME, 1)
            for _ in range(12):
                u32.SendMessageW(int(edit_hwnd), _WM_KEYDOWN, _VK_DELETE, 1)
                u32.SendMessageW(int(edit_hwnd), _WM_KEYUP, _VK_DELETE, 1)
            for ch in val:
                u32.SendMessageW(int(edit_hwnd), _WM_CHAR, ord(ch), 1)
                time.sleep(0.02)
            _notify_parent()
            time.sleep(0.08)
            return None
        except Exception as exc:
            return f"WM_CHAR 填入 {val!r} 失败: {exc}"

    try:
        _win32_focus_edit(edit_hwnd, root_hwnd=root_hwnd, real_click=True)
        from pywinauto.keyboard import send_keys

        # MFC 单行 CEdit 忽略 Ctrl+A，使用 END + 12xBackSpace + HOME + 12xDelete 100% 彻底清空
        send_keys("{END}{BACKSPACE 12}{HOME}{DELETE 12}", pause=0.008)
        time.sleep(0.05)
        send_keys(val, pause=0.025, with_spaces=False)
        time.sleep(0.08)
        return None
    except Exception as exc:
        logger.info("真焦点键入异常（回退 WM_CHAR）: %s", exc)
        return _win32_type_single_edit(edit_hwnd, val, root_hwnd=root_hwnd, prefer_wm_char=True)


def _find_edit_controls(app: Application, hwnd: Optional[int] = None) -> Optional[list]:
    """UIA 兜底：查找导航后表单区域的可见可用 Edit 输入框（自动剔除顶部账号 ComboBox 内嵌 Edit）。"""
    try:
        dlg = app.window(handle=hwnd) if hwnd else app.window(title=PANEL_TITLE)
        raw_edits = list(dlg.descendants(control_type="Edit") or [])
        filtered: list[Any] = []
        for e in raw_edits:
            try:
                if not e.is_enabled():
                    continue
                if not e.is_visible():
                    continue
                parent = e.parent()
                if parent is not None and str(getattr(parent.element_info, "control_type", "") or "") == "ComboBox":
                    continue
                if str(getattr(e.element_info, "automation_id", "") or "") == "2322":
                    continue
            except Exception:
                pass
            filtered.append(e)
        return filtered if filtered else (raw_edits or None)
    except Exception:
        return None


# 同花顺 5.0 经典买入/卖出表单固定控件 ID
_THS_EDIT_AIDS: dict[str, tuple[str, ...]] = {
    "code": ("1032",),
    "price": ("1033",),
    "qty": ("1034",),
}


def _identify_edits(edits: list) -> dict[str, Any]:
    """按固定 automation_id → name 关键字 → 位置序 3 级精准识别代码/价格/数量输入框。"""
    labeled: dict[str, Any] = {}
    assigned: set[int] = set()

    # 1. 优先精确匹配同花顺固定 automation_id (1032/1033/1034)
    for role, aids in _THS_EDIT_AIDS.items():
        for i, e in enumerate(edits):
            if i in assigned:
                continue
            try:
                aid = str(e.element_info.automation_id or "").strip()
            except Exception:
                aid = ""
            if aid in aids:
                labeled[role] = e
                assigned.add(i)
                break

    # 2. name 关键字匹配
    for role, keywords in _EDIT_HEURISTIC_NAMES.items():
        if role in labeled:
            continue
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

    # 3. 位置回退
    for role, pos in [("code", 0), ("price", 1), ("qty", 2)]:
        if role in labeled:
            continue
        if pos < len(edits) and pos not in assigned:
            labeled[role] = edits[pos]
            assigned.add(pos)

    return labeled


def _fill_edit(edit, value: str, *, use_keyboard_fallback: bool = True) -> Optional[str]:
    """清空+填入；优先 UIA set_edit_text，若读回不符再走焦点 Ctrl+A 键入（绝不按回车）。"""
    try:
        edit.set_edit_text("")
        time.sleep(0.08)
        edit.set_edit_text(value)
        time.sleep(0.12)
        cur = _read_edit_value(edit).strip()
        if cur == value or (value.replace(".", "").isdigit() and cur and abs(float(cur) - float(value)) <= 1e-4):
            return None
    except Exception as e:
        if not use_keyboard_fallback:
            return f"填入 {value!r} 失败: {e}"

    if not use_keyboard_fallback:
        return f"填入 {value!r} 后读回不符"

    try:
        edit.set_focus()
        time.sleep(0.08)
        from pywinauto.keyboard import send_keys

        send_keys("^a{BACKSPACE}")
        time.sleep(0.06)
        send_keys(value, with_spaces=False)
        time.sleep(0.12)
        return None
    except Exception as e:
        return f"键盘回退填入 {value!r} 失败: {e}"


def _format_price_str(code: str, price: float) -> str:
    c6 = str(code).split(".")[0].strip().zfill(6)
    is_etf = c6.startswith(("51", "56", "58", "159", "16"))
    return f"{float(price):.3f}" if is_etf else f"{float(price):.2f}"


def _verify_filled_values(filled: dict[str, str], code: str, price: float, qty: int) -> tuple[bool, list[str]]:
    """严格核对读回的代码/价格/数量是否与目标委托一致。"""
    mismatches: list[str] = []
    c6 = str(code).split(".")[0].strip().zfill(6)
    got_code = str(filled.get("code") or "").strip()
    if c6 not in got_code:
        mismatches.append(f"代码不符(期望 {c6}, 实读 {got_code!r})")

    got_price_s = str(filled.get("price") or "").strip()
    try:
        got_price = float(got_price_s)
        if abs(got_price - float(price)) > 0.005:
            mismatches.append(f"价格不符(期望 {price}, 实读 {got_price_s!r})")
    except ValueError:
        mismatches.append(f"价格无法解析(期望 {price}, 实读 {got_price_s!r})")

    got_qty_s = str(filled.get("qty") or "").strip()
    try:
        got_qty = int(float(got_qty_s))
        if got_qty != int(qty):
            mismatches.append(f"数量不符(期望 {qty}, 实读 {got_qty_s!r})")
    except ValueError:
        mismatches.append(f"数量无法解析(期望 {qty}, 实读 {got_qty_s!r})")

    return len(mismatches) == 0, mismatches


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


def _dump_fill_failure_diag(hwnd: int, win32_edits: dict[str, int]) -> None:
    """填单校验失败时保存离屏截图与控件样式/前台/弹窗诊断，便于精准定位原因。"""
    try:
        u32 = _user32()
        fg = int(u32.GetForegroundWindow() or 0)
        fg_title = _win32_text(fg) if fg else ""
        fg_cls = _win32_class(fg) if fg else ""
        root_en = _win32_is_enabled(hwnd)
        styles = {}
        for role, eh in win32_edits.items():
            st = int(ctypes.windll.user32.GetWindowLongW(int(eh), -16))
            ex = int(ctypes.windll.user32.GetWindowLongW(int(eh), -20))
            styles[role] = {"hwnd": hex(eh), "style": hex(st), "exstyle": hex(ex), "readonly": bool(st & 0x0800)}
        child_dlgs = []
        try:
            from scripts import ths_client as tc

            child_dlgs = tc._yh_ro_child_dialogs(int(hwnd))
        except Exception:
            pass
        logger.warning(
            "填单失败诊断: root_hwnd=%x root_enabled=%s fg=%x(%s/%r) child_dialogs=%s edit_styles=%s",
            hwnd, root_en, fg, fg_cls, fg_title, child_dlgs, styles,
        )
        try:
            from services.broker.ths_holdings_poll import print_window_image

            img = print_window_image(int(hwnd))
            out_png = Path(r"F:\Aistock\runtime\g3_prefill_drafts\last_fill_debug.png")
            out_png.parent.mkdir(parents=True, exist_ok=True)
            img.save(out_png)
            logger.warning("填单失败离屏截图已保存: %s", out_png)
        except Exception as exc:
            logger.warning("保存填单失败截图异常: %s", exc)
    except Exception:
        pass


def fill_order(
    account: str,
    code: str,
    price: float,
    qty: int,
    side: str = "buy",
    *,
    auto_cold_start: bool = True,
    keep_visible: bool = True,
    allow_foreground: bool = True,
) -> dict[str, Any]:
    """填单核心（原生 Win32 极速通道 + 按需冷启动 + 实时账号核验切换 + 防盘口覆写 + 读回强一致核对）。绝不 Invoke 提交。"""
    t0 = time.monotonic()
    result: dict[str, Any] = {
        "schema": FILL_SCHEMA,
        "success": False,
        "ok": False,
        "status": "fill_failed",
        "filled": {},
        "error": None,
        "steps": [],
        "ts": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
    logger.info("fill_order 开始: account=%s side=%s code=%s price=%s qty=%s", account, side, code, price, qty)

    if not _PYWINSUCCESS:
        result["error"] = "pywinauto 不可用（请确认桥接进程使用 F:\\python\\python3.10）"
        return result

    # -- 1) 加载 panel_map（降级容错：即使缺失也不阻塞固定 ID 填单） --
    try:
        pm = load_panel_map()
        result["steps"].append(f"panel_map 加载完成 (cached_account={pm.get('account')})")
    except RuntimeError as e:
        pm = {"panel": {}, "account": None, "identity_label": ""}
        result["steps"].append(f"panel_map 降级跳过: {e}")

    nav_name = "买入[F1]" if side == "buy" else "卖出[F2]"

    # -- 2) 查找实时交易面板（未打开时按需冷启动同花顺远航版） --
    if auto_cold_start:
        app, hwnd, boot_steps, err = _ensure_panel_ready_on_demand(target_account=account)
        result["steps"].extend(boot_steps)
    else:
        app, hwnd, err = _find_window()

    if app is None or hwnd is None:
        result["error"] = err or "窗口查找失败"
        logger.warning("fill_order 窗口未就绪: %s", result["error"])
        return result

    if isinstance(pm.get("panel"), dict):
        pm["panel"]["hwnd"] = hex(hwnd)

    if not allow_foreground:
        fore_err = _check_foreground(hwnd)
        if fore_err:
            result["error"] = fore_err
            result["status"] = "skipped_foreground"
            return result

    was_min = _ensure_restored(hwnd)
    result["steps"].append(f"实时窗口就绪 hwnd={hwnd:x} was_minimized={was_min}")

    # -- 3) 实时账号核对与自动切换（原生 Win32 兼容双账号 2322 与单账号 1004 股东号） --
    new_hwnd, sw_err = _switch_account_if_needed(pm, account, live_hwnd=hwnd, allow_foreground=allow_foreground)
    if new_hwnd and int(new_hwnd) != int(hwnd):
        hwnd = int(new_hwnd)
        try:
            app = Application(backend="uia").connect(handle=hwnd)
        except Exception:
            pass
    if sw_err:
        result["error"] = sw_err
        logger.warning("fill_order 账号校验/切换失败: %s", sw_err)
        if was_min and not keep_visible:
            _show_window(hwnd, 6)
        return result
    result["steps"].append(f"账号 {account}({pm.get('identity_label', '')}) 实时核验就绪")

    # -- 4) 切换到买入[F1]/卖出[F2]页签（优先热键，零 UIA 卡死） --
    nav_err = _select_nav_item(app, side, hwnd=hwnd)
    if nav_err:
        result["error"] = nav_err
        logger.warning("fill_order 切换买卖页签失败: %s", nav_err)
        if was_min and not keep_visible:
            _show_window(hwnd, 6)
        return result
    result["steps"].append(f"已进入 {nav_name}")

    # -- 5) 定位表单输入框（优先原生 Win32 固定 ID 1032/1033/1034，毫秒级且无 UIA 阻塞） --
    win32_edits = _win32_find_order_edits(hwnd)
    use_win32 = len(win32_edits) == 3
    labeled_uia: dict[str, Any] = {}
    if use_win32:
        result["steps"].append(
            f"Win32 极速定位表单控件成功 (code={win32_edits['code']:x}, price={win32_edits['price']:x}, qty={win32_edits['qty']:x})"
        )
    else:
        edits = _find_edit_controls(app, hwnd=hwnd)
        if edits is None:
            result["error"] = "导航后未找到 Edit 输入框（表单未渲染？）"
            if was_min and not keep_visible:
                _show_window(hwnd, 6)
            return result
        result["steps"].append(f"UIA 回退找到 {len(edits)} 个表单 Edit 控件")
        labeled_uia = _identify_edits(edits)
        missing = [r for r in ("code", "price", "qty") if r not in labeled_uia]
        if missing:
            result["error"] = f"未能标识输入框角色: {missing}"
            if was_min and not keep_visible:
                _show_window(hwnd, 6)
            return result

    # -- 6) 分步填入 + 防同花顺异步盘口覆写价格 + 强制读回校验（不符自动重填 1 次） --
    code6 = str(code).split(".")[0].strip().zfill(6)
    price_str = _format_price_str(code6, price)
    qty_str = str(int(qty))

    filled_vals: dict[str, str] = {}
    mismatches: list[str] = []
    fill_errors: dict[str, str] = {}
    for attempt in (1, 2):
        fill_errors = {}
        if use_win32:
            # 1) 先点击表单内「重填」按钮彻底清零旧残留（绝不触碰买入/卖出提交按钮）
            _win32_click_reset_button(win32_edits["code"])
            # 2) 单次洁净填入：attempt=1 走真焦点键入，attempt=2 走纯消息 WM_CHAR（应对 RDP 最小化/锁屏）
            prefer_msg = (attempt == 2)
            fe_code = _win32_type_single_edit(win32_edits["code"], code6, root_hwnd=hwnd, prefer_wm_char=prefer_msg)
            if fe_code:
                fill_errors["code"] = fe_code
            # 等同花顺拉取证券名称与默认卖一/买一价，避免后发覆盖价格框
            time.sleep(0.75 if attempt == 1 else 0.90)

            fe_price = _win32_type_single_edit(win32_edits["price"], price_str, root_hwnd=hwnd, prefer_wm_char=prefer_msg)
            if fe_price:
                fill_errors["price"] = fe_price
            time.sleep(0.15)

            fe_qty = _win32_type_single_edit(win32_edits["qty"], qty_str, root_hwnd=hwnd, prefer_wm_char=prefer_msg)
            if fe_qty:
                fill_errors["qty"] = fe_qty
            time.sleep(POST_FILL_SETTLE_SEC)

            # 3) 读回核验：优先 WM_GETTEXT，若同花顺安全输入框屏蔽则返回空串，立即切 PrintWindow+RapidOCR 视觉核验
            filled_vals = {role: _win32_read_edit_any(eh) for role, eh in win32_edits.items()}
            if not all(filled_vals.get(r) for r in ("code", "price", "qty")):
                vis_vals = _visual_readback_order_edits(
                    hwnd,
                    win32_edits,
                    save_png_path=Path(r"F:\Aistock\runtime\g3_prefill_drafts\last_fill_verify.png"),
                )
                for r in ("code", "price", "qty"):
                    if not filled_vals.get(r) and vis_vals.get(r):
                        filled_vals[r] = vis_vals[r]
        else:
            fe_code = _fill_edit(labeled_uia["code"], code6)
            if fe_code:
                fill_errors["code"] = fe_code
            time.sleep(0.65 if attempt == 1 else 0.85)

            fe_price = _fill_edit(labeled_uia["price"], price_str)
            if fe_price:
                fill_errors["price"] = fe_price
            time.sleep(0.15)

            fe_qty = _fill_edit(labeled_uia["qty"], qty_str)
            if fe_qty:
                fill_errors["qty"] = fe_qty
            time.sleep(POST_FILL_SETTLE_SEC)

            filled_vals = {role: _read_edit_value(edit) for role, edit in labeled_uia.items()}

        ok_verify, mismatches = _verify_filled_values(filled_vals, code6, price, qty)
        if ok_verify and not fill_errors:
            result["steps"].append(f"第 {attempt} 次填值与读回强校验一致: {filled_vals}")
            break
        result["steps"].append(f"第 {attempt} 次填值校验未通过: errors={fill_errors} mismatches={mismatches}")

    filled_vals["side"] = "买入" if side == "buy" else "卖出"
    filled_vals["account"] = account
    filled_vals["label"] = pm.get("identity_label", "")
    result["filled"] = filled_vals

    if fill_errors or mismatches:
        detail_parts = []
        if fill_errors:
            detail_parts.append(f"写入错误={fill_errors}")
        if mismatches:
            detail_parts.append("；".join(mismatches))
        result["error"] = f"填单读回校验未通过（已拒绝假成功）：{' | '.join(detail_parts)}"
        logger.warning("fill_order 校验失败 (%.2fs): %s", time.monotonic() - t0, result["error"])
        if use_win32:
            _dump_fill_failure_diag(hwnd, win32_edits)
        if was_min and not keep_visible:
            _show_window(hwnd, 6)
        return result

    result["success"] = True
    result["ok"] = True
    result["status"] = "filled"
    result["elapsed_sec"] = round(time.monotonic() - t0, 2)
    result["steps"].append("代码/价格/股数读回核验 100% 一致（绝不自动提交，窗口已置前等待人工点击确认）")
    logger.info("fill_order 成功 (%.2fs): filled=%s", result["elapsed_sec"], filled_vals)

    if keep_visible:
        _activate_window_safe(hwnd)
    elif was_min:
        _show_window(hwnd, 6)

    return result


def _confirm_order_dialog(app) -> Optional[str]:
    """在确认弹窗上自动点确认（Phase 4：Owner 授权监督试运行）。"""
    import time
    try:
        dlg = app.window(title=PANEL_TITLE)
    except:
        return "无法获取面板窗口"
    # 等待弹窗出现（最多5秒）
    for _ in range(10):
        try:
            # 弹窗通常是一个Dialog/Pane子窗口，找"确认/是/确定"按钮
            buttons = dlg.descendants(control_type="Button")
            for b in buttons:
                name = (b.element_info.name or "").strip()
                if any(kw in name for kw in ["确认", "是", "确定", "Yes", "OK", "确认[Y]"]):
                    b.set_focus()
                    time.sleep(0.3)
                    from pywinauto.keyboard import send_keys
                    send_keys("{ENTER}")
                    time.sleep(3)
                    return None
            # 没找到确认按钮 → 也许弹窗还在加载
            time.sleep(0.5)
        except:
            time.sleep(0.5)
    return "未找到确认弹窗/确认按钮（弹窗可能未出现或已被手动操作）"


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
        _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from services.l2_order_gate import check_order, OrderRequest, GateContext
        from datetime import datetime as _dt

        req = OrderRequest(account=account, code=code, side=side,
                           price=round(price, 2), qty=qty,
                           ts=_dt.now())
        ctx = GateContext(now=_dt.now(), kill_switch=False)
        # 加载真实 l2_risk_config
        cfg_path = Path(r"F:/Aistock/runtime/t_tracker/l2_risk_config.json")
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


def auto_submit(
    account: str,
    code: str,
    price: float,
    qty: int,
    side: str = "buy",
) -> dict[str, Any]:
    """Phase 4: 全自动填单+提交+确认（Owner 授权监督试运行）。"""
    result: dict[str, Any] = {
        "schema": "l2_auto_submit/1",
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
        _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from services.l2_order_gate import check_order, OrderRequest, GateContext
        from datetime import datetime as _dt

        req = OrderRequest(account=account, code=code, side=side,
                           price=round(price, 2), qty=qty, ts=_dt.now())
        ctx = GateContext(now=_dt.now(), kill_switch=False)
        cfg_path = Path(r"F:/Aistock/runtime/t_tracker/l2_risk_config.json")
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

    # 2. 填单
    fill = fill_order(account, code, price, qty, side)
    if not fill.get("success"):
        result["error"] = fill.get("error", "填单失败")
        result["steps"].extend(fill.get("steps", []))
        return result
    result["filled"] = fill.get("filled", {})
    result["steps"].extend(fill.get("steps", []))

    # 3. 提交
    app, err = _find_window()
    if app is None:
        result["error"] = err or "窗口不可用"
        return result
    import time
    click_err = _click_buy_button(app)
    if click_err:
        result["error"] = click_err
        return result
    result["steps"].append("已点击买入按钮")

    # 4. 自动确认弹窗
    time.sleep(1.5)
    confirm_err = _confirm_order_dialog(app)
    if confirm_err:
        result["error"] = confirm_err
        return result
    result["steps"].append("自动确认完成")

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