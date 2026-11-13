# -*- coding: utf-8 -*-
"""同花顺（THS）统一客户端 scripts/ths_client.py —— 单脚本收拢全部 THS 功能。

Owner 2026-11-13 要求：同花顺全部功能收拢到单一脚本。六个子命令：

  login           启动链（hexin Popen 带 cwd → 等 xiadan）+ 登录态判定 +
                  凭证记住时点登录（原 scripts/ths_xiadan_client.py 全部逻辑）
  export-fills    多账号当日成交导出（原 scripts/ths_fills_export.py 全部
                  逻辑：R51 身份门禁多锚/热切换/unavailable 标记/幂等）
  poll-holdings   盘中零交互持仓巡读（原 scripts/ths_holdings_poll.py CLI：
                  PrintWindow+UIA 只读，连查询菜单都不点；--once 为默认模式）
  survey          L2 面板 UIA 零风险勘察/绑定取证（原 scripts/l2_panel_survey.py）
  status          聚合一览（全只读）：进程/面板/登录态/各账号身份锁定/最近
                  fills、快照产物路径与时间/最近告警
  daily           全链 = login 检查 → export-fills → 汇总报告（15:05 cron 用，
                  替代 hermes 侧 ths_fills_daily.py 的调度职责）

实现约束：
  * 复用 services/ 层（ths_account_bridge / ths_hotswitch / ths_holdings_poll），
    服务层单一事实源，本文件不复制其逻辑；
  * 原四脚本保留为薄 wrapper（deprecated，转发到本入口）：hermes cron
    ths_fills_daily.py 与 schtask AiStock_THSHoldingsPoll 等外部引用零改动；
  * 全子命令统一 JSON 报告风格（schema ths_client/1，credentials_touched
    恒 false）；各域文件产物保留原协议 schema（fills/panel_map 等）。

安全铁律（与原四脚本一致，收拢不放松）：
  * 凭证零接触：密码框只读长度位（WM_GETTEXTLENGTH），绝不取/写文本；
  * 绝不触碰委托下单界面；survey 段源码自审计禁一切输入原语（_self_audit）；
  * 零交互巡读让位铁律：前台/最小化/锁忙一律跳过，绝不抢焦点；
  * 身份门禁 fail-closed（R51 多锚），THS 不可达写 unavailable 标记不造数。

用法（host 解释器 F:/python/python3.10/python.exe，有 pywinauto）：
  python scripts/ths_client.py login --dry-run     # 启动链+登录态判定
  python scripts/ths_client.py export-fills --once # 15:05 成交导出
  python scripts/ths_client.py poll-holdings --once
  python scripts/ths_client.py survey --walker control
  python scripts/ths_client.py status
  python scripts/ths_client.py daily               # 15:05 cron 全链
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

IS_WINDOWS = os.name == "nt"

# 统一 JSON 报告 schema：所有子命令顶层 envelope。各域文件产物保留其原
# schema（fills 文件 ths_fills_export/1、panel_map l2_panel_map/1 等）。
SCHEMA = "ths_client/1"

# 服务层复用（不复制逻辑）：
#   bridge   = services.ths_account_bridge（窗口锁/查询菜单/OCR/面板注册表/登录框检测）
#   poll     = services.ths_holdings_poll（巡读轮次/快照/diff/inferred 服务）
#   hotswitch= services.ths_hotswitch（R49c 账号切换器受控路径/身份多锚读取）
from services import ths_account_bridge as bridge  # noqa: E402
from services import ths_holdings_poll as poll  # noqa: E402
from services import ths_hotswitch as hotswitch  # noqa: E402
from services.ths_hotswitch import build_hotswitch_plan  # noqa: E402


def _runtime_dir(name: str) -> Path:
    """运行期产物目录（仓库外 runtime 树；utils.paths 可用时走它）。"""
    try:
        from utils.paths import runtime_path

        return Path(runtime_path(name))
    except Exception:
        base = Path(os.environ.get("AISTOCK_DATA_ROOT", r"F:\AiStock\data\data"))
        return base / "runtime" / name


def _tracker_root(out_dir: Optional[Path] = None) -> Path:
    """t_tracker 根目录（fills 导出与巡读共用；--out-dir 覆盖，测试用）。"""
    if out_dir is not None:
        return Path(out_dir)
    return _runtime_dir("t_tracker")


def _write_report(report: dict[str, Any], root: Optional[Path] = None,
                  prefix: str = "report") -> Path:
    """报告原子写（原 ths_xiadan_client._write_report；prefix 供 daily 复用）。"""
    root = Path(root) if root is not None else _runtime_dir("ths_xiadan_client")
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    path = root / f"{prefix}_{stamp}.json"
    report["report_path"] = str(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


# ══════════════════════════════════════════════════════════════════════════════
# login 段（原 scripts/ths_xiadan_client.py 全部逻辑：启动链 + 登录态状态机 +
# 凭证记住时点登录 + --export 子进程导出）。原文件已改为薄 wrapper 转发至此。
# ══════════════════════════════════════════════════════════════════════════════

class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


if IS_WINDOWS:  # pragma: no cover - exercised only on the Windows host
    USER32: Any = ctypes.windll.user32
    KERNEL32: Any = ctypes.windll.kernel32
    KERNEL32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    KERNEL32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    KERNEL32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    KERNEL32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    KERNEL32.CloseHandle.argtypes = [ctypes.c_void_p]
else:  # pragma: no cover - import guard for non-Windows static checks
    USER32 = None
    KERNEL32 = None


# Win32 constants used by the narrow automation surface.
GW_CHILD = 5
GW_HWNDNEXT = 2
GA_ROOT = 2
SW_RESTORE = 9
SW_SHOW = 5
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
HWND_TOP = 0
ASFW_ANY = -1
WM_GETTEXTLENGTH = 0x000E  # 只取长度，绝不取文本（凭证铁律）
BM_CLICK = 0x00F5
ES_PASSWORD = 0x0020
GWL_STYLE = -16
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = 0xFFFFFFFFFFFFFFFF

DIALOG_CLASS = "#32770"
HEXIN_PROCESS = "hexin.exe"
XIADAN_PROCESS = "xiadan.exe"
# 主窗口锚点与 services/ths_account_bridge 同源（DEFAULT_PANEL_RULES 实锚）
TRADE_WINDOW_TITLE_TOKENS = ("网上股票交易", "交易系统")

THS_DIR_DEFAULT = Path(r"D:\同花顺")
HEXIN_EXE_DEFAULT = THS_DIR_DEFAULT / "hexin.exe"
XIADAN_EXE_DEFAULT = THS_DIR_DEFAULT / "xiadan.exe"
PYTHON_EXE_DEFAULT = Path(r"F:\python\python3.10\python.exe")
EXPORT_SCRIPT_DEFAULT = Path(__file__).resolve()  # 统一入口自身（export-fills 子命令）
# 报告 schema 统一用文件头 SCHEMA（ths_client/1）

LAUNCH_WAIT_SEC = 60.0   # hexin 拉起 xiadan 的自动等待
DIRECT_WAIT_SEC = 30.0   # 直接 Popen xiadan.exe 后等进程
WINDOW_WAIT_SEC = 30.0   # 等 xiadan 首个可见顶层窗口
LOGIN_WAIT_SEC = 90.0    # 点「登录」后等主窗口
EXPORT_TIMEOUT_SEC = 600.0

_LOGIN_BUTTON_RE = re.compile(r"登\s*[录陆](?:\s*\(?[A-Za-z0-9]?\)?)?")


class ThsXiadanError(RuntimeError):
    """Expected UI/process contract was not present; caller must stop."""

    def __init__(self, message: str, steps: Optional[list[dict[str, Any]]] = None):
        super().__init__(message)
        self.steps = steps


def _require_windows() -> None:
    if not IS_WINDOWS or USER32 is None:
        raise ThsXiadanError("THS xiadan automation requires Windows")


# ── 窗口原语（与 tdx_postclose_client 同源，GetWindow 链避免 EnumWindows 回调挂起） ──

def _window_text(hwnd: int) -> str:
    if not hwnd or USER32 is None:
        return ""
    n = int(USER32.GetWindowTextLengthW(hwnd))
    buf = ctypes.create_unicode_buffer(max(2, n + 2))
    USER32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def _class_name(hwnd: int) -> str:
    if not hwnd or USER32 is None:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    USER32.GetClassNameW(hwnd, buf, len(buf))
    return buf.value


def _rect(hwnd: int) -> tuple[int, int, int, int]:
    _require_windows()
    value = wintypes.RECT()
    if not USER32.GetWindowRect(hwnd, ctypes.byref(value)):
        raise ThsXiadanError(f"GetWindowRect failed hwnd={hwnd}")
    return int(value.left), int(value.top), int(value.right), int(value.bottom)


def _top_level_windows(limit: int = 4096) -> list[int]:
    """Enumerate desktop children without an EnumWindows callback (host-proven)."""
    _require_windows()
    desktop = int(USER32.GetDesktopWindow())
    hwnd = int(USER32.GetWindow(desktop, GW_CHILD))
    out: list[int] = []
    seen: set[int] = set()
    while hwnd and len(out) < limit and hwnd not in seen:
        seen.add(hwnd)
        out.append(hwnd)
        hwnd = int(USER32.GetWindow(hwnd, GW_HWNDNEXT))
    return out


def _children(parent: int, limit: int = 512) -> list[int]:
    _require_windows()
    hwnd = int(USER32.GetWindow(parent, GW_CHILD))
    out: list[int] = []
    seen: set[int] = set()
    while hwnd and len(out) < limit and hwnd not in seen:
        seen.add(hwnd)
        out.append(hwnd)
        hwnd = int(USER32.GetWindow(hwnd, GW_HWNDNEXT))
    return out


def _is_visible(hwnd: int) -> bool:
    return bool(USER32 and USER32.IsWindowVisible(hwnd))


def _root(hwnd: int) -> int:
    _require_windows()
    return int(USER32.GetAncestor(hwnd, GA_ROOT)) or hwnd


def _process_id(hwnd: int) -> int:
    if not hwnd or USER32 is None:
        return 0
    pid = wintypes.DWORD()
    USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _window_from_point(x: int, y: int) -> int:
    _require_windows()
    return int(USER32.WindowFromPoint(wintypes.POINT(int(x), int(y))))


def _window_style(hwnd: int) -> int:
    if not hwnd or USER32 is None:
        return 0
    return int(USER32.GetWindowLongW(hwnd, GWL_STYLE))


def _message_int(hwnd: int, msg: int) -> int:
    """SendMessageW returning an int (WM_GETTEXTLENGTH / BM_CLICK)."""
    _require_windows()
    return int(USER32.SendMessageW(hwnd, msg, 0, 0))


def _normal_label(value: str) -> str:
    return " ".join(value.replace("&", "").split()).strip()


def _is_login_button_label(text: str) -> bool:
    label = _normal_label(text).replace("&", "")
    return bool(_LOGIN_BUTTON_RE.fullmatch(label))


def _find_login_button(login_hwnd: int) -> Optional[int]:
    for child in _children(login_hwnd):
        if _class_name(child) != "Button" or not _is_visible(child):
            continue
        if _is_login_button_label(_window_text(child)):
            return child
    return None


def _looks_like_edit_class(cls: str) -> bool:
    return cls.lower().endswith("edit")


def _bring_to_front(hwnd: int) -> None:
    _require_windows()
    root = _root(hwnd)
    foreground = int(USER32.GetForegroundWindow())
    foreground_thread = int(USER32.GetWindowThreadProcessId(foreground, None)) if foreground else 0
    target_thread = int(USER32.GetWindowThreadProcessId(root, None))
    attached = bool(foreground_thread and target_thread and foreground_thread != target_thread)
    if attached:
        USER32.AttachThreadInput(foreground_thread, target_thread, True)
    USER32.ShowWindow(root, SW_RESTORE)
    USER32.ShowWindow(root, SW_SHOW)
    USER32.AllowSetForegroundWindow(ASFW_ANY)
    USER32.SetWindowPos(
        root, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
    )
    USER32.BringWindowToTop(root)
    USER32.SetForegroundWindow(root)
    USER32.SetFocus(root)
    if attached:
        USER32.AttachThreadInput(foreground_thread, target_thread, False)


def _click_window(hwnd: int) -> None:
    """Real mouse click; used only as the single BM_CLICK fallback on 登录."""
    _require_windows()
    _bring_to_front(hwnd)
    left, top, right, bottom = _rect(hwnd)
    x = (left + right) // 2
    y = (top + bottom) // 2
    hit = _window_from_point(x, y)
    if not hit or (hit != hwnd and not USER32.IsChild(hwnd, hit)):
        raise ThsXiadanError(
            f"click target is covered by another window: target={hwnd} hit={hit}"
        )
    if not USER32.SetCursorPos(x, y):
        raise ThsXiadanError(f"SetCursorPos failed for hwnd={hwnd}")
    time.sleep(0.08)
    USER32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    USER32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


# ── 进程层（纯 ctypes Toolhelp32，无 psutil 依赖） ──

def _pids_of(exe_name: str) -> list[int]:
    """Snapshot PIDs whose image name equals exe_name (case-insensitive)."""
    if KERNEL32 is None:
        raise ThsXiadanError("process snapshot requires Windows")
    snap = KERNEL32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        raise ThsXiadanError("CreateToolhelp32Snapshot failed")
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        want = exe_name.lower()
        out: list[int] = []
        has = KERNEL32.Process32FirstW(snap, ctypes.byref(entry))
        while has:
            if str(entry.szExeFile).lower() == want:
                out.append(int(entry.th32ProcessID))
            has = KERNEL32.Process32NextW(snap, ctypes.byref(entry))
        return out
    finally:
        KERNEL32.CloseHandle(snap)


def _popen(exe: Path, cwd: Path) -> None:
    subprocess.Popen([str(exe)], cwd=str(cwd), close_fds=True)


def _launch(exe: Path) -> None:
    if not exe.is_file():
        raise ThsXiadanError(f"executable not found: {exe}")
    # 必须带 cwd=安装目录：从 git-bash 的 start 启动会失败（实盘事实）
    _popen(exe, exe.parent)


def _wait_for(predicate: Callable[[], Any], timeout: float, interval: float = 0.25) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def _ensure_processes(
    hexin_exe: Path, xiadan_exe: Path, launch_wait: float, direct_wait: float,
) -> tuple[list[int], list[dict[str, Any]]]:
    """启动链：hexin（带 cwd）→ 等 xiadan 自动拉起 → 兜底直接拉 xiadan。"""
    steps: list[dict[str, Any]] = []
    try:
        hexin_pids = _pids_of(HEXIN_PROCESS)
        launched_hexin = False
        if hexin_pids:
            steps.append({"step": "hexin_already_running", "pids": hexin_pids})
        else:
            _launch(hexin_exe)
            launched_hexin = True
            steps.append({"step": "launched_hexin", "exe": str(hexin_exe),
                          "cwd": str(hexin_exe.parent)})

        xiadan_pids = _pids_of(XIADAN_PROCESS)
        if not xiadan_pids:
            # 刚拉起 hexin 时给足自动拉起窗口；hexin 本就在跑则只给短宽限
            wait_sec = launch_wait if launched_hexin else min(launch_wait, 5.0)
            xiadan_pids = _wait_for(lambda: _pids_of(XIADAN_PROCESS), wait_sec, 1.0) or []
            steps.append({"step": "waited_auto_pull", "waited_sec": wait_sec,
                          "xiadan_seen": bool(xiadan_pids)})
        if not xiadan_pids:
            _launch(xiadan_exe)
            steps.append({"step": "launched_xiadan_direct", "exe": str(xiadan_exe),
                          "cwd": str(xiadan_exe.parent)})
            xiadan_pids = _wait_for(lambda: _pids_of(XIADAN_PROCESS), direct_wait, 1.0) or []
        if not xiadan_pids:
            raise ThsXiadanError(
                "xiadan.exe not running after hexin auto-pull wait and direct launch"
            )
        return xiadan_pids, steps
    except ThsXiadanError as exc:
        if exc.steps is None:  # 失败也要把已走的启动步骤留给报告（fail-closed 证据）
            exc.steps = steps
        raise


# ── 登录态状态机 ──

def _is_trade_window_title(title: str) -> bool:
    return any(token in title for token in TRADE_WINDOW_TITLE_TOKENS)


def _poll_state() -> tuple[str, list[dict[str, Any]], Optional[dict[str, Any]]]:
    """xiadan 顶层窗口 → (main|dialog|none, 可见窗口证据, 主窗口或 None)。

    只接受标题含交易系统锚点的非对话框窗口；可见的其他窗口不能证明登录。
    隐藏的交易系统壳窗口仍需结合可见登录框判断，不能独立证明登录。
    #32770 只有可见时才算登录框（隐藏的 #32770 是遗留对话框，实机多个）。
    """
    pids = set(_pids_of(XIADAN_PROCESS))
    visible: list[dict[str, Any]] = []
    main: Optional[dict[str, Any]] = None
    for hwnd in _top_level_windows():
        if _process_id(hwnd) not in pids:
            continue
        cls = _class_name(hwnd)
        if cls != DIALOG_CLASS:
            title = _window_text(hwnd)
            is_visible = _is_visible(hwnd)
            if _is_trade_window_title(title):
                main = {"hwnd": hwnd, "class": cls, "title": title,
                        "visible": is_visible}
                continue
        if _is_visible(hwnd):
            visible.append({"hwnd": hwnd, "class": cls})
    if main is not None:
        return "main", visible, main
    if any(w["class"] == DIALOG_CLASS for w in visible):
        return "dialog", visible, None
    return "none", visible, None


def _wait_any_window(timeout: float) -> tuple[str, list[dict[str, Any]], Optional[dict[str, Any]]]:
    state, windows, main = _poll_state()
    deadline = time.monotonic() + timeout
    while state == "none" and time.monotonic() < deadline:
        time.sleep(0.5)
        state, windows, main = _poll_state()
    return state, windows, main


def _find_visible_login_dialog(
    windows: list[dict[str, Any]],
) -> Optional[int]:
    """可见 #32770 中形状=登录框的（密码框 Edit + 「登录」钮）。

    2026-11-13 19:5x 实盘根因：盘后重启后面板停在登录框，标题壳窗
    「网上股票交易系统5.0」仍在（隐藏/最小化）→ _poll_state 报 main →
    误判 logged_in → 导出门禁 label=''。登录框可见 = 未登录，壳窗不算数。
    错误弹窗（确定钮、无密码框）不匹配 → 仍按已登录处理。
    """
    for w in windows or []:
        if w.get("class") != DIALOG_CLASS:
            continue
        hwnd = int(w.get("hwnd") or 0)
        if _find_login_button(hwnd) is None:
            continue
        if _password_state(hwnd)["password_edit_found"]:
            return hwnd
    return None


def _password_state(login_hwnd: int) -> dict[str, Any]:
    """检测「密码框是否非空」：ES_PASSWORD 样式定位密码框（兜底最后一个编辑框），
    只用 WM_GETTEXTLENGTH 取长度位，绝不取文本、不写文本。"""
    edits = [
        child
        for child in _children(login_hwnd)
        if _looks_like_edit_class(_class_name(child)) and _is_visible(child)
    ]
    password = next(
        (h for h in edits if int(_window_style(h)) & ES_PASSWORD), None
    )
    selector = "es_password"
    if password is None and len(edits) >= 2:
        password = edits[-1]  # 约定：登录框密码框在账号框下方（最后一个编辑框）
        selector = "last_edit"
    if password is None:
        return {"password_edit_found": False, "password_nonempty": False,
                "selector": "none"}
    length = _message_int(password, WM_GETTEXTLENGTH)
    return {"password_edit_found": True, "password_nonempty": int(length) > 0,
            "selector": selector}


def _click_login_and_wait(
    login_hwnd: int, button_hwnd: int, timeout: float,
) -> tuple[str, list[dict[str, Any]]]:
    """命中验证后真实点击一次；不重复提交登录。"""
    clicks: list[dict[str, Any]] = []
    _click_window(button_hwnd)
    clicks.append({"method": "coordinate_click", "button_hwnd": button_hwnd})

    deadline = time.monotonic() + timeout
    last_state = "dialog"
    while True:
        last_state, wins, _main = _poll_state()
        # 登录后壳窗可能先于登录框消失；任何可见对话框都不能作为成功。
        if last_state == "main" and not wins:
            return "logged_in_after_click", clicks
        now = time.monotonic()
        if now >= deadline:
            break
        time.sleep(0.5)
    raise ThsXiadanError(
        f"login click did not reach a main window "
        f"(last_state={last_state}, clicks={len(clicks)})"
    )


def _reach_login_state(*, window_wait: float, login_timeout: float,
                       dry_run: bool) -> dict[str, Any]:
    state, windows, main = _wait_any_window(window_wait)
    if state == "none":
        raise ThsXiadanError(
            "no visible xiadan top-level window after launch (NO_XIADAN_WINDOW)"
        )
    # R51：登录框可见 = 未登录（标题壳窗不算数，2026-11-13 19:5x 实盘根因）
    visible_login = _find_visible_login_dialog(windows)
    if state == "main" and not windows:
        return {"login_state": "logged_in", "windows": windows,
                "main_window": main, "clicks": [], "password_remembered": None}
    if state == "main" and visible_login is None:
        raise ThsXiadanError("visible unrecognized dialog blocks login verification")
    if visible_login is not None and len(windows) != 1:
        raise ThsXiadanError("additional window blocks login")
    if visible_login is None:
        # state == dialog 但无登录框形状（遗留/错误弹窗）——维持原契约：
        # 取首个可见对话框按登录框流程做形状校验（形状不明 fail-closed）
        login_hwnd = windows[0]["hwnd"]
    else:
        login_hwnd = visible_login
    button = _find_login_button(login_hwnd)
    password = _password_state(login_hwnd)
    detail: dict[str, Any] = {
        "login_state": "login_box",
        "windows": windows,
        "clicks": [],
        "login_hwnd": login_hwnd,
        "password_remembered": password["password_nonempty"],
        "password_edit_selector": password["selector"],
    }
    # 形态不明的 #32770 绝不点击（可能是错误弹窗而非登录框）
    if password["selector"] == "none" or button is None:
        raise ThsXiadanError(
            f"login dialog shape not recognized: "
            f"selector={password['selector']} login_button={button}"
        )
    if not password["password_nonempty"]:
        detail["login_state"] = "need_manual_login"
        return detail
    if dry_run:
        detail["would_click_login"] = True
        return detail
    detail["login_state"], detail["clicks"] = _click_login_and_wait(
        login_hwnd, button, login_timeout
    )
    return detail


def _read_default_account_label(hwnd: int) -> str:
    """只读交易窗口账号切换器的 Value；不读取登录框或凭证。"""
    try:
        from pywinauto import Application
        window = Application(backend="uia").connect(handle=hwnd).window(handle=hwnd)
        combos = window.descendants(auto_id="2322", control_type="ComboBox")
        if len(combos) != 1:
            return ""
        return str(combos[0].iface_value.CurrentValue or "").strip()
    except Exception:
        return ""


def _verify_default_account(timeout: float) -> str:
    """窗口和当前账户下拉框共同确认目标账户；仅返回内部代号。"""
    labels = {"广发证券-曾*": "acct1", "广发证券-王*美": "acct2"}
    def observe() -> Optional[str]:
        state, windows, main = _poll_state()
        if state != "main" or windows or not main:
            return None
        label = _read_default_account_label(main["hwnd"])
        return labels.get("".join(str(label or "").split()))
    account = _wait_for(observe, timeout, 0.5)
    if not account:
        raise ThsXiadanError("default account identity could not be verified")
    return account


# ── 导出子进程 ──

def _run_export(python_exe: Path, export_script: Path, timeout: float) -> dict[str, Any]:
    for label, path in (("python_exe", python_exe), ("export_script", export_script)):
        if not path.is_file():
            raise ThsXiadanError(f"export {label} not found: {path}")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)        # cron 环境 win32api DLL 污染事故：剥离
    env["PYTHONIOENCODING"] = "utf-8"  # 子进程管道编码（exporter 输出中文 JSON）
    # 经统一入口导出（旧链：ths_fills_export.py --once；wrapper 仍兼容）
    cmd = [str(python_exe), str(export_script), "export-fills", "--once"]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=str(export_script.parent), env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {"cmd": cmd, "returncode": None, "timed_out": True,
                "duration_sec": round(time.monotonic() - started, 1),
                "stdout": str(exc.stdout or "")[-20000:],
                "stderr": str(exc.stderr or "")[-4000:],
                "pythonpath_stripped": True}
    return {"cmd": cmd, "returncode": proc.returncode, "timed_out": False,
            "duration_sec": round(time.monotonic() - started, 1),
            "stdout": (proc.stdout or "")[-20000:],
            "stderr": (proc.stderr or "")[-4000:],
            "pythonpath_stripped": True}


def run(
    *,
    hexin_exe: Path = HEXIN_EXE_DEFAULT,
    xiadan_exe: Path = XIADAN_EXE_DEFAULT,
    python_exe: Path = PYTHON_EXE_DEFAULT,
    export_script: Path = EXPORT_SCRIPT_DEFAULT,
    launch_wait: float = LAUNCH_WAIT_SEC,
    direct_wait: float = DIRECT_WAIT_SEC,
    window_wait: float = WINDOW_WAIT_SEC,
    login_timeout: float = LOGIN_WAIT_SEC,
    export_timeout: float = EXPORT_TIMEOUT_SEC,
    dry_run: bool = False,
    export: bool = False,
    report_root: Optional[Path] = None,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "command": "login",
        "status": "failure",
        "started_at": started.isoformat(),
        "dry_run": bool(dry_run),
        "export_requested": bool(export),
        "credentials_touched": False,
        "hexin_exe": str(hexin_exe),
        "xiadan_exe": str(xiadan_exe),
    }
    try:
        with bridge._ths_window_operation_lock(lock_name=bridge.HOTSWITCH_LOCK_NAME):
            pids, steps = _ensure_processes(hexin_exe, xiadan_exe, launch_wait, direct_wait)
            report["launch"] = steps
            report["xiadan_pids"] = pids
            detail = _reach_login_state(
                window_wait=window_wait, login_timeout=login_timeout, dry_run=dry_run,
            )
            if not dry_run and detail["login_state"] in {"logged_in", "logged_in_after_click"}:
                report["account"] = _verify_default_account(login_timeout)
        launched = any(
            s.get("step") in {"launched_hexin", "launched_xiadan_direct"} for s in steps
        )
        if detail["login_state"] == "logged_in" and launched:
            detail["login_state"] = "logged_in_after_launch"
        for key in ("login_state", "windows", "main_window", "clicks",
                    "password_remembered", "password_edit_selector", "login_hwnd"):
            if key in detail:
                report[key] = detail[key]

        if dry_run:
            report["status"] = "dry_run"
            report["would_click_login"] = bool(detail.get("would_click_login"))
            if export:
                report["export_skipped"] = "dry_run"
            return report

        if detail["login_state"] == "need_manual_login":
            report["status"] = "need_manual_login"
            report["reason"] = (
                "password box empty on login dialog; refusing to click (fail-closed)"
            )
            if export:
                report["export_skipped"] = "not_logged_in"
            return report

        if not export:
            report["status"] = "logged_in"
            return report

        result = _run_export(python_exe, export_script, export_timeout)
        report["export"] = result
        report["status"] = "success" if result.get("returncode") == 0 else "export_failed"
        return report
    except Exception as exc:  # noqa: BLE001 - fail-closed：异常如实入报告
        report["status"] = "failure"
        report["reason"] = f"{type(exc).__name__}: {exc}"
        steps = getattr(exc, "steps", None)
        if steps and "launch" not in report:
            report["launch"] = steps  # 失败路径也保留启动链证据
        return report
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_report(report, report_root)

# ══════════════════════════════════════════════════════════════════════════════
# export-fills 段（原 scripts/ths_fills_export.py 全部逻辑：R37b/R47/R48/R51
# 多账号当日成交导出——身份门禁多锚、热切换、unavailable 标记、幂等）。
# 原文件已改为薄 wrapper（hermes cron ths_fills_daily.py 15:05 经它转发）。
# ══════════════════════════════════════════════════════════════════════════════

SCHEMA_FILLS = "ths_fills_export/1"             # fills 文件协议 schema（不变）
SCHEMA_FILLS_MULTI = "ths_fills_export_multi/1"  # 多账号汇总 schema（不变）
SCHEMA_UNAVAILABLE = "ths_fills_export_unavailable/1"
POSTCLOSE_SKIP_AFTER = 15 * 60  # 盘后幂等线：当日 15:00 后已有 ok 文件则跳过
DEFAULT_HISTORY_WINDOW_DAYS = 10
SOURCE_TODAY = "ths_today_trades_ocr"
SOURCE_HISTORY = "ths_history_trades_ocr"

# 当日成交视图表头特征（hexin 2026.9.22.14 实拍：无「成交日期」列，时间为 HH:MM:SS）
_TODAY_HEADER_TOKENS = ("成交时间", "证券代码")
_TIME_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})$")
_CODE_RE = re.compile(r"^\d{6}$")
_DECIMAL_RE = re.compile(r"\d+\.\d+")
_BUY_TOKENS = ("买", "买入", "证券买入", "buy")
_SELL_TOKENS = ("卖", "卖出", "证券卖出", "sell")


def _parse_number(value: Any) -> Optional[float]:
    text = str(value or "").replace(",", "").strip()
    if not text or text in {"--", "-"}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _normalize_time_cell(cell: str) -> Optional[str]:
    text = str(cell or "").strip()
    match = _TIME_RE.match(text)
    if match:
        return f"{match.group(1)}:{match.group(2)}:{match.group(3)}"
    return None  # 不收 6 位紧凑形态：会把证券代码 002015 误判成时间


def _parse_side(cell: str) -> str:
    text = str(cell or "").strip().lower()
    if any(token in text for token in _BUY_TOKENS):
        return "BUY"
    if any(token in text for token in _SELL_TOKENS):
        return "SELL"
    return ""


def parse_today_trades_lines(lines: list[str], for_date: date) -> dict[str, Any]:
    """解析当日成交视图 OCR 行（`_ocr_items_to_lines` 产物）→ fills 协议行。

    行特征（R32 实拍）：左侧栏噪声词开头（撤单[F3]/自选股/三…），随后
    HH:MM:SS | 6位代码 | 名称(可 OCR 误读，原样保留) | 证券买/卖出 | 数量(整)
    | 均价(小数) | 金额(小数) | 合同编号(整) | 成交编号…。

    数量取第一个整数形态数字，均价/金额取前两个小数形态数字（合同/成交编号为
    整数形态不会误占）；金额缺失或缺额>1% 时按 均价×数量 补齐（amount_source
    标记 computed），价格缺失则整行 fail-closed 计入 skipped。
    """
    header_seen = any(
        all(token in line for token in _TODAY_HEADER_TOKENS) for line in lines
    )
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped: list[str] = []

    for line in lines:
        cells = [c.strip() for c in str(line or "").split("\t") if c.strip()]
        time_idx = next((i for i, c in enumerate(cells) if _normalize_time_cell(c)), None)
        if time_idx is None:
            continue  # 非数据行（表头/侧栏/资金行等）
        time_norm = _normalize_time_cell(cells[time_idx])
        rest = cells[time_idx + 1:]
        code_idx = next((i for i, c in enumerate(rest) if _CODE_RE.match(c)), None)
        if code_idx is None:
            skipped.append(f"code_missing@{time_norm}")
            continue
        code = rest[code_idx]
        tail = rest[code_idx + 1:]
        side_idx = next((i for i, c in enumerate(tail) if _parse_side(c)), None)
        if side_idx is None:
            skipped.append(f"side_missing@{time_norm}")
            continue
        side = _parse_side(tail[side_idx])
        name = next((c for i, c in enumerate(tail)
                     if i != side_idx and not _parse_number(c) and not _parse_side(c)), "")

        ints: list[float] = []
        decimals: list[float] = []
        for i, c in enumerate(tail):
            if i == side_idx:
                continue
            value = _parse_number(c)
            if value is None:
                continue
            (decimals if _DECIMAL_RE.search(c) else ints).append(value)
        shares = next((v for v in ints if v > 0), None)
        price = next((v for v in decimals if v > 0), None)
        if shares is None or price is None:
            skipped.append(f"price_missing@{time_norm}" if price is None
                           else f"shares_missing@{time_norm}")
            continue
        amount = next((v for v in decimals if v != price), None)
        amount_source = "ocr"
        if amount is None or abs(amount - price * shares) > max(1.0, 0.01 * price * shares):
            amount = round(price * shares, 2)
            amount_source = "computed"

        row = {
            "trade_time": f"{for_date.isoformat()} {time_norm}",
            "side": side,
            "code": code,
            "name": name,
            "shares": int(shares),
            "price": round(price, 3),
            "amount": round(amount, 2),
            "source": SOURCE_TODAY,
        }
        if amount_source == "computed":
            row["amount_source"] = "computed"
        sig = "|".join([row["trade_time"], row["side"], row["code"],
                        str(row["shares"]), str(row["price"])])
        if sig in seen:
            continue
        seen.add(sig)
        rows.append(row)

    rows.sort(key=lambda r: r["trade_time"])
    return {"header_seen": header_seen, "rows": rows, "parsed": len(rows),
            "skipped": skipped, "skipped_count": len(skipped)}


def merge_fills(today_rows: list[dict[str, Any]], history_rows: list[dict[str, Any]],
                for_date: date, window_days: int = DEFAULT_HISTORY_WINDOW_DAYS
                ) -> list[dict[str, Any]]:
    """当日行 + 历史行（近 window_days 日，严格早于 for_date）→ fills 协议合并。

    历史行来自 bridge `_parse_trades_from_ocr_items`（含多余键），归一为协议子集；
    按 (trade_time, side, code, shares, price) 去重，当日视图优先，时间升序。
    """
    lo = (for_date - timedelta(days=window_days)).isoformat()
    day = for_date.isoformat()
    merged: dict[str, dict[str, Any]] = {}

    def _key(r: dict[str, Any]) -> str:
        return "|".join([str(r.get("trade_time") or ""), str(r.get("side") or ""),
                         str(r.get("code") or ""), str(r.get("shares")),
                         str(r.get("price"))])

    for row in history_rows:
        ts = str(row.get("trade_time") or "")
        if not lo <= ts[:10] < day:
            continue  # 窗口外（过旧/当日/未来）不参与配对
        merged.setdefault(_key(row), {
            "trade_time": ts, "side": str(row.get("side") or "").upper(),
            "code": str(row.get("code") or "").split(".")[0],
            "name": str(row.get("name") or ""),
            "shares": int(row.get("shares") or 0),
            "price": float(row.get("price") or 0.0),
            "amount": round(float(row.get("amount") or 0.0)
                            or round(float(row.get("price") or 0) * int(row.get("shares") or 0), 2), 2),
            "source": SOURCE_HISTORY,
        })
    for row in today_rows:  # 当日视图权威，同键覆盖历史
        merged[_key(row)] = dict(row)
    return [merged[k] for k in sorted(merged, key=lambda k: merged[k]["trade_time"])]


def build_payload(rows: list[dict[str, Any]], *, status: str, trade_date: date,
                  captured_at: str, history_rows: int = 0,
                  account: str = "acct1") -> dict[str, Any]:
    return {
        "schema": SCHEMA_FILLS,
        "status": status,  # ok | zero_trades
        "account": account,  # R47：行级 account 字段同步冗余在顶层
        "trade_date": trade_date.isoformat(),
        "captured_at": captured_at,
        "sources": {"today": SOURCE_TODAY, "history": SOURCE_HISTORY},
        "history_window_days": DEFAULT_HISTORY_WINDOW_DAYS,
        "history_rows_merged": history_rows,
        "fills": rows,
    }


def should_skip_existing(existing: Optional[dict[str, Any]], for_date: date,
                         now: datetime) -> bool:
    """幂等：当日 15:00 后已有 ok/zero_trades 文件 → 跳过（重复 cron 安全）。"""
    if not isinstance(existing, dict):
        return False
    if existing.get("status") not in {"ok", "zero_trades"}:
        return False
    if existing.get("trade_date") != for_date.isoformat():
        return False
    captured = str(existing.get("captured_at") or "")
    try:
        cap_dt = datetime.fromisoformat(captured)
    except ValueError:
        return False
    return cap_dt.date() == for_date and (
        cap_dt.hour * 60 + cap_dt.minute) >= POSTCLOSE_SKIP_AFTER


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _write_unavailable(root: Path, for_date: date, now: datetime,
                       reason: str, detail: Any = None,
                       account: str = "acct1") -> dict[str, Any]:
    stamp = for_date.strftime("%Y%m%d")
    suffix = "" if account == "acct1" else f"_{account}"
    marker = {
        "schema": SCHEMA_UNAVAILABLE, "status": "unavailable",
        "account": account,
        "trade_date": for_date.isoformat(), "captured_at": now.isoformat(sep=" ", timespec="seconds"),
        "reason": reason, "detail": detail,
    }
    _atomic_write_json(root / "fills" / f"{stamp}{suffix}.unavailable.json", marker)
    return marker


def _fills_paths(root: Path, stamp: str, account: str) -> dict[str, Path]:
    """账号 → fills 文件名。acct1=legacy 无后缀（既有消费者零改动），
    其他账号=YYYYMMDD_acctN.json（R47 协议）。"""
    suffix = "" if account == "acct1" else f"_{account}"
    fills_file = root / "fills" / f"{stamp}{suffix}.json"
    return {
        "fills": fills_file,
        "evidence": root / "fills" / f"evidence_{stamp}{suffix}",
        "acct_copy": (root / "fills" / f"{stamp}_acct1.json") if account == "acct1" else fills_file,
    }


def run_export(*, for_date: date, now: datetime, out_root: Optional[Path] = None,
               force: bool = False, account: str = "acct1",
               capture: Callable[[Optional[Path]], dict[str, Any]]) -> dict[str, Any]:
    """单账号导出（--once）。capture 注入真机采集（测试注入合成视图）。

    R47：account="acct1" 写 legacy YYYYMMDD.json + YYYYMMDD_acct1.json 镜像；
    其他账号写 YYYYMMDD_<account>.json。
    """
    root = _tracker_root(out_root)
    stamp = for_date.strftime("%Y%m%d")
    paths = _fills_paths(root, stamp, account)
    fills_file = paths["fills"]
    evidence_dir = paths["evidence"]

    if not force and fills_file.is_file():
        try:
            existing = json.loads(fills_file.read_text(encoding="utf-8"))
        except Exception:
            existing = None
        if should_skip_existing(existing, for_date, now):
            return {"action": "skip", "status": "skip", "account": account,
                    "existing_captured_at":
                    (existing or {}).get("captured_at"), "fills_file": str(fills_file)}

    try:
        captured = capture(evidence_dir)
    except Exception as exc:  # noqa: BLE001 - THS 不可达如实标记，不造数
        marker = _write_unavailable(root, for_date, now, f"{type(exc).__name__}: {exc}",
                                    account=account)
        return {"action": "unavailable", "status": "unavailable", "account": account,
                "reason": marker["reason"], "marker": marker["captured_at"]}

    today = parse_today_trades_lines(list(captured.get("today_lines") or []), for_date)
    if not today["header_seen"]:
        marker = _write_unavailable(root, for_date, now, "today_view_header_not_found",
                                    {"preview": list(captured.get("today_lines") or [])[:16]},
                                    account=account)
        return {"action": "unavailable", "status": "unavailable", "account": account,
                "reason": "today_view_header_not_found",
                "marker": marker["captured_at"]}
    if today["skipped_count"]:
        marker = _write_unavailable(root, for_date, now,
                                    f"partial_today_rows: {today['skipped_count']}",
                                    {"skipped": today["skipped"]}, account=account)
        return {"action": "unavailable", "status": "unavailable", "account": account,
                "reason": f"partial_today_rows: {today['skipped_count']}",
                "marker": marker["captured_at"]}
    if not captured.get("history_header_seen"):
        marker = _write_unavailable(root, for_date, now, "history_view_header_not_found",
                                    account=account)
        return {"action": "unavailable", "status": "unavailable", "account": account,
                "reason": "history_view_header_not_found", "marker": marker["captured_at"]}

    history_rows = list(captured.get("history_rows") or [])
    merged = merge_fills(today["rows"], history_rows, for_date)
    for row in merged:
        row.setdefault("account", account)
    prior = sum(1 for r in merged if r["trade_time"][:10] < for_date.isoformat())
    status = "ok" if today["rows"] else "zero_trades"
    payload = build_payload(merged, status=status, trade_date=for_date,
                            captured_at=now.isoformat(sep=" ", timespec="seconds"),
                            history_rows=prior, account=account)
    _atomic_write_json(fills_file, payload)
    if account == "acct1":
        _atomic_write_json(paths["acct_copy"], payload)  # legacy 名镜像已写，_acct1 副本同步
    return {"action": "write", "status": status, "account": account,
            "fills_file": str(fills_file),
            "fills_total": len(merged), "fills_today": len(today["rows"]),
            "prior_day_fills": prior, "window_state": captured.get("window_state") or {}}


def assert_panel_identity(today_lines: list[str], identity_hint: Optional[str],
                          account: str = "acct1") -> Optional[str]:
    """R48 身份门禁（纯函数可测）：hint 未配置 → None 放行；配置且顶栏
    「券商-姓名」OCR 标签不含 hint → 抛 PANEL_IDENTITY_MISMATCH。"""
    if not identity_hint:
        return None
    from services.ths_account_bridge import identity_matches, parse_identity_label
    label = parse_identity_label(today_lines)
    if identity_matches(label, identity_hint) is False:
        raise RuntimeError(f"PANEL_IDENTITY_MISMATCH: label={label!r} "
                           f"expect~{identity_hint!r} account={account}")
    return label


def gate_identity_decision(ocr_label: str, combo_text: str,
                           identity_hint: Optional[str], *,
                           allow_combo_fallback: bool) -> tuple[bool, str, str]:
    """R51 门禁决策（纯函数可测）：(ok, reason, effective_label)。

    输入双锚读到的身份证据：ocr_label=顶栏 PrintWindow OCR「券商-姓名」
    主锚；combo_text=账号切换器 ComboBox(aid=2322) 当前项文本备用锚。
      * hint 未配置 → (True, "no_hint", "")
      * 主锚可读：含 hint → ok；不含 → mismatch（明确他人身份，任何降级不救）
      * 主锚空（渲染缺层/OCR 波动）：备用锚可读且 allow_combo_fallback
        （单账号采集）且含 hint → ok（reason=combo_fallback）；备用锚读到
        他人身份 → mismatch；其余（双账号 fail-closed / 双锚皆空）→
        unreadable fail-closed
    2026-11-13 实盘注：登录框态两锚皆空——决策为 unreadable，但上游
    detect_xiadan_login_dialog 会先一步报 PANEL_LOGIN_DIALOG 真原因。
    """
    if not identity_hint:
        return True, "no_hint", ""
    if ocr_label:
        if identity_hint in ocr_label:
            return True, "ocr", ocr_label
        return False, "mismatch", ocr_label
    if combo_text:
        if identity_hint in combo_text and allow_combo_fallback:
            return True, "combo_fallback", combo_text
        return False, "mismatch" if identity_hint not in combo_text else "dual_account_fail_closed", combo_text
    return False, "unreadable", ""


def assert_panel_identity_robust(ident: dict[str, Any], identity_hint: Optional[str],
                                 account: str = "acct1", *,
                                 allow_combo_fallback: bool = False) -> str:
    """R51 门禁（多锚版）：输入 read_window_identity_robust 产物，决策见
    gate_identity_decision。不通过抛 PANEL_IDENTITY_MISMATCH（携带双锚证据）。
    hint 未配置 → 空串放行。"""
    if not identity_hint:
        return ""
    ocr_label = str((ident or {}).get("ocr_label") or "")
    combo_text = str((ident or {}).get("combo_text") or "")
    ok, reason, label = gate_identity_decision(
        ocr_label, combo_text, identity_hint,
        allow_combo_fallback=allow_combo_fallback)
    if not ok:
        raise RuntimeError(
            f"PANEL_IDENTITY_MISMATCH: label={ocr_label!r} combo={combo_text!r} "
            f"reason={reason} expect~{identity_hint!r} account={account}")
    return label


def _capture_ths_views(evidence_dir: Optional[Path] = None,
                       panel: Optional[dict[str, Any]] = None,
                       account: str = "acct1",
                       identity_hint: Optional[str] = None,
                       hotswitch: Optional[dict[str, Any]] = None,
                       combo_identity_fallback: bool = False) -> dict[str, Any]:
    """真机采集（仅 host / F:\\python\\python3.10）：锁内依次导出当日/历史成交视图。

    R47：panel 指定目标面板（panel_scope 内 _find_xiadan_window 连到该面板 pid）。
    R49c hotswitch：目标账号≠当前面板账号 → 账号切换器 ComboBox(aid=2322)
    SelectionItem 选择 + OCR 身份自证（选不到/读不到目标令牌=fail-closed
    unavailable）；组末位读毕切回常态位。热切换账号统一串行在共享锁
    ths_window.lock（单物理面板），lock_name 显式覆盖。
    只触 `_click_query_menu_item` 查询子项（查询菜单），绝不打开下单界面；
    窗口若原为最小化，采集后还原为最小化。两视图截图与 OCR 行归档 evidence_dir。
    R48 身份门禁：首个视图 OCR 后校验顶栏「券商-姓名」含 identity_hint；
    不符（窗口换登他人账号）→ 抛 PANEL_IDENTITY_MISMATCH → run_export 落
    unavailable 标记（fail-closed，绝不把账号2成交写成 acct1）。
    R51 门禁加固（2026-11-13 实盘根因：盘后重启后面板停在登录框，壳窗
    骨架渲染、双锚读身份恒空 → 误报 MISMATCH label=''）：
      ① 导出前先还原/前台化面板再读标签（渲染让位给真实 paint）；
      ② 身份读取多锚：PrintWindow OCR 主锚 + ComboBox(2322) 当前项备用锚，
         门禁前置到点菜单之前（失败零交互零采集）；
      ③ 降级安全：主锚空且单账号采集（combo_identity_fallback=True）时用
         账号下拉当前项文本验证身份；双账号采集保持 fail-closed；
      ④ 登录框在（detect_xiadan_login_dialog）→ PANEL_LOGIN_DIALOG 真原因
         fail-closed（未登录≠身份不符，绝不写 fills）。
    """
    import shutil

    from services.ths_account_bridge import (  # noqa: WPS433
        _click_query_menu_item,
        _find_xiadan_window,
        _ocr_xiadan_window,
        _parse_trades_from_ocr_items,
        _ths_window_operation_lock,
        detect_xiadan_login_dialog,
        panel_scope,
    )

    def _keep(shot: str, name: str) -> Optional[str]:
        if not evidence_dir or not shot:
            return None
        try:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            target = evidence_dir / name
            shutil.copy2(shot, target)
            return str(target)
        except Exception:
            return None

    inner_error: Optional[Exception] = None
    switchback: dict[str, Any] = {}
    window: Any = None
    was_minimized = False
    today_lines: list[str] = []
    history_lines: list[str] = []
    history_rows: list[dict[str, Any]] = []
    kept_today = kept_history = None
    gate_detail: dict[str, Any] = {}
    with panel_scope(panel), _ths_window_operation_lock(
            panel=account, lock_name=(hotswitch or {}).get("lock_name")):
        on_target = False
        try:
            window = _find_xiadan_window()
            try:
                was_minimized = bool(window.is_minimized())
            except Exception:
                was_minimized = False
            # R51 ①：先还原/前台化再读身份（最小化窗 PrintWindow 渲染空白）
            from services.ths_hotswitch import _ensure_restored
            _ensure_restored(window)
            # R51 ④：登录框在 = 面板未登录（壳窗骨架、双锚恒空）→ 真原因
            login = detect_xiadan_login_dialog(window)
            if login is not None:
                raise RuntimeError(
                    f"PANEL_LOGIN_DIALOG: 面板停在登录框（未登录） "
                    f"dlg_hwnd={login.get('dialog_hwnd')} "
                    f"account_field={login.get('account')!r} "
                    f"password_remembered={login.get('password_nonempty')} "
                    f"expect~{identity_hint!r} target_account={account}")
            # R51 ②③：门禁前置（多锚读取 + 纯函数决策），点菜单之前零交互
            if identity_hint:
                from services.ths_hotswitch import read_window_identity_robust
                ident = read_window_identity_robust(
                    window, save_png=(evidence_dir / "identity_gate.png")
                    if evidence_dir else None)
                gate_detail = {"ocr_label": ident["ocr_label"],
                               "combo_text": ident["combo_text"]}
                assert_panel_identity_robust(
                    ident, identity_hint, account,
                    allow_combo_fallback=combo_identity_fallback)
            if hotswitch:
                from services import ths_hotswitch as hs
                pre = hs.ensure_target_account(window, hotswitch)
                if pre is not None:
                    raise RuntimeError(
                        f"PANEL_{str(pre.get('status', 'switch_failed')).upper()}: "
                        f"label={pre.get('identity_label')!r} "
                        f"expect~{hotswitch.get('target_hint')!r} "
                        f"account={account}")
            on_target = True
            _click_query_menu_item("today_trades")
            _today_items, today_lines, shot_today = _ocr_xiadan_window()
            kept_today = _keep(shot_today, "today_view.png")
            if identity_hint:  # R48 身份门禁（换登 fail-closed）
                assert_panel_identity(today_lines, identity_hint, account)
            _click_query_menu_item("history_trades")
            history_items, history_lines, shot_history = _ocr_xiadan_window()
            kept_history = _keep(shot_history, "history_view.png")
            history_rows = _parse_trades_from_ocr_items(history_items)
        except Exception as exc:  # noqa: BLE001 - R48：块内捕获，不依赖
            # generator CM 的 throw 传播（2026-11-11 14:46 换登扰动期实证：
            # 经 contextlib 传播会变形为 "generator didn't stop after throw()"）
            inner_error = exc
        finally:
            # R49：组末位读毕切回常态位（导出成败均须恢复；best-effort）
            if on_target and hotswitch and hotswitch.get("restore_after"):
                from services import ths_hotswitch as hs
                try:
                    rb = hs.restore_home_account(window, hotswitch)
                except Exception as exc:  # noqa: BLE001
                    rb = {"status": "switchback_failed",
                          "error": f"{type(exc).__name__}: {exc}"}
                if rb is not None:
                    switchback = {"switchback_failed": True, "detail": rb}
            # R51：原为最小化则采集成败均复最小化（此前仅成功路径恢复，
            # 门禁 fail-closed 会把面板留在还原态）。登录框模态期
            # pywinauto minimize 可能被吞，用 ShowWindow 直发更可靠。
            if was_minimized and window is not None:
                try:
                    import ctypes
                    ctypes.windll.user32.ShowWindow(
                        int(getattr(window, "handle", 0) or 0), 6)  # SW_MINIMIZE
                except Exception:
                    pass
    if inner_error is not None:
        raise inner_error  # run_export 落 unavailable 标记（真实原因）
    if evidence_dir:
        try:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            (evidence_dir / "today_ocr_lines.json").write_text(
                json.dumps(today_lines, ensure_ascii=False, indent=1), encoding="utf-8")
            (evidence_dir / "history_rows.json").write_text(
                json.dumps(history_rows, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
    history_header_seen = any(
        ("成交日期" in line or "成交时间" in line) and "代码" in line
        for line in history_lines
    )
    window_state: dict[str, Any] = {"minimized_before": was_minimized,
                                    "restored_minimized": was_minimized}
    if gate_detail:
        window_state["identity_gate"] = gate_detail
    if switchback:
        window_state.update(switchback)
    return {
        "today_lines": today_lines,
        "history_rows": history_rows,
        "history_header_seen": history_header_seen,
        "window_state": window_state,
        "screenshots": {"today": kept_today, "history": kept_history},
    }


def _parse_for_date(text: Optional[str]) -> date:
    if not text:
        return datetime.now().date()
    return date.fromisoformat(text)


# ── R47 多账号：采集计划 + 账号循环 ───────────────────────────

KNOWN_ACCOUNTS = ("acct1", "acct2")


def build_capture_plan(live_panels: list[dict[str, Any]],
                       registry: Any,
                       account_filter: Optional[str] = None) -> dict[str, Any]:
    """live 面板 × 注册表规则 → 采集计划 {account: {panel, identity_hint,
    hotswitch?, capture 由 main 装配}}。

    acct1 无绑定面板时仍入计划（panel=None → 默认 connect 路径，legacy 行为）；
    其他账号仅有绑定面板才入计划（fail-closed：未绑定面板绝采集）。
    R49c：热切换组共享物理面板——home 在前、组末位
    restore_after=True（读毕切回常态位），锁统一串行在共享 ths_window.lock；
    切换机制=ComboBox aid=2322 SelectionItem（注册表 hotkey 仅作绑定标记）。
    """
    resolved = registry.resolve(live_panels)
    from services.ths_hotswitch import build_hotswitch_plan

    plan = build_hotswitch_plan(registry, live_panels,
                                account_filter=account_filter)
    plan["_resolution"] = {  # 上报面（不参与采集）
        "unbound_panels": resolved.unbound,
        "ambiguous_accounts": resolved.ambiguous_accounts,
        "hotswitch_groups": {str(k): v for k, v
                             in (resolved.hotswitch_groups or {}).items()},
    }
    return plan


def run_export_multi(*, for_date: date, now: datetime, out_root: Optional[Path] = None,
                     plan: dict[str, Any], force: bool = False,
                     known_accounts: Optional[list[str]] = None) -> dict[str, Any]:
    """多账号导出：逐账号 run_export（锁/文件名按账号隔离）。

    rc 语义：在场账号 unavailable → rc=2（acct1 沿用 PM watcher 语义）；
    已配置但无面板的账号 → panel_absent 跳过（非失败，当前 acct2 实况）。
    """
    resolution = plan.pop("_resolution", {}) or {}
    accounts: dict[str, Any] = {}
    rc = 0
    for account in KNOWN_ACCOUNTS:
        entry = plan.get(account)
        if entry is None:
            if known_accounts and account in known_accounts:
                accounts[account] = {"status": "panel_absent"}
            continue
        result = run_export(for_date=for_date, now=now, out_root=out_root,
                            force=force, account=account, capture=entry["capture"])
        accounts[account] = result
        if result.get("status") == "unavailable":
            rc = 2
    plan["_resolution"] = resolution  # 幂等：不消费调用方的 plan
    return {"schema": SCHEMA_FILLS_MULTI, "trade_date": for_date.isoformat(),
            "accounts": accounts, "resolution": resolution, "rc": rc}

# ══════════════════════════════════════════════════════════════════════════════
# poll-holdings 段（原 scripts/ths_holdings_poll.py CLI 逻辑：schtasks 每 5min
# --once 单轮巡读 / --probe-capture / --replay-morning）。轮次/快照/diff 服务
# 在 services 层。原文件已改为薄 wrapper（schtask AiStock_THSHoldingsPoll 经
# 它转发，任务现禁用中）。
# ══════════════════════════════════════════════════════════════════════════════

def _parse_now(text: Optional[str]) -> datetime:
    if not text:
        return datetime.now()
    fmt = "%H:%M:%S" if len(text.split(":")) == 3 else "%H:%M"
    return datetime.combine(date.today(), datetime.strptime(text, fmt).time())


def _build_plan(account_filter: Optional[str]) -> dict[str, dict[str, Any]]:
    """bridge 面板枚举 × 注册表 → 有序计划 {account: {panel, identity_hint,
    hotswitch?}}（R49c：热切换组 home 在前、末位 restore_after；切换机制
    =ComboBox aid=2322 SelectionItem，注册表 hotkey 仅作绑定标记）。

    fills 导出器同语义：acct1 无绑定面板也入计划走默认 connect；未绑定其他
    账号绝采集。identity_hint 取自规则（默认 acct1=「曾」，R47 取证锚）。"""
    from services.ths_account_bridge import load_panel_registry

    try:
        from services.ths_account_bridge import enumerate_trade_panels
        # include_minimized：最小化面板也入解析（capture 仍 skipped_minimized
        # 让位零交互铁律；状态可辨 panel vs absent）
        live_panels = enumerate_trade_panels(include_minimized=True)
    except Exception:  # 非 host 环境 → 空面板表
        live_panels = []
    return build_hotswitch_plan(load_panel_registry(), live_panels,
                                account_filter=account_filter)


def cmd_poll_once(args: argparse.Namespace) -> int:
    now = _parse_now(args.now)
    ok, reason = poll.should_poll(now)
    if not ok:
        print(json.dumps({"schema": SCHEMA, "command": "poll-holdings",
                          "status": "out_of_window", "reason": reason,
                          "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                          "credentials_touched": False}, ensure_ascii=False))
        return 0
    if not args.no_stagger:
        wait = poll.stagger_sleep_seconds(datetime.now())
        if wait > 0:
            time.sleep(min(wait, 60.0))
    root = Path(args.out_dir) if args.out_dir else _tracker_root()
    plan = _build_plan(args.account)
    report = poll.run_poll_round(
        root=root, now=datetime.now(), plan=plan,
        capture=lambda account, panel, hint=None: poll.capture_panel_zero_touch(
            account, panel, evidence_dir=poll.snapshots_dir(root, now.date()),
            identity_hint=hint,
            hotswitch=(plan.get(account) or {}).get("hotswitch")))
    statuses = [r.get("status") for r in report.get("accounts", {}).values()]
    aggregate = "error" if statuses and all(s == "error" for s in statuses) else "ok"
    print(json.dumps({"schema": SCHEMA, "command": "poll-holdings",
                      "status": aggregate, "credentials_touched": False,
                      "poll": report}, ensure_ascii=False, default=str))
    return 1 if aggregate == "error" else 0


def cmd_probe(args: argparse.Namespace) -> int:
    """单面板一次零交互采集（任何时候安全：PrintWindow+UIA 只读，不吃焦点）。

    R49：--account acct2 时经热切换读目标账号（Alt+N 身份自证，读毕切回常态位）。"""
    root = Path(args.out_dir) if args.out_dir else _tracker_root()
    plan = _build_plan(args.account or "acct1")
    account, entry = next(iter(plan.items()))
    cap = poll.capture_panel_zero_touch(
        account, entry.get("panel"),
        evidence_dir=poll.snapshots_dir(root, date.today()),
        identity_hint=entry.get("identity_hint"),
        hotswitch=entry.get("hotswitch"))
    safe = {k: cap.get(k) for k in ("status", "note", "error", "ocr_line_count",
                                    "screenshot", "identity_label",
                                    "identity_hint", "switchback_failed")}
    if cap.get("status") == "ok":
        safe["holdings"] = [{k: h.get(k) for k in
                             ("code", "name", "shares", "available_shares",
                              "cost_price", "current_price", "holding_pnl")}
                            for h in cap.get("holdings") or []]
        safe["capital"] = cap.get("capital")
    safe["ocr_lines_preview"] = (cap.get("ocr_lines") or [])[:12]
    print(json.dumps({"schema": SCHEMA, "command": "poll-holdings",
                      "mode": "probe-capture", "status": cap.get("status"),
                      "credentials_touched": False, "probe": safe},
                     ensure_ascii=False, default=str))
    return 0 if cap.get("status") == "ok" else 2


def cmd_replay_morning(args: argparse.Namespace) -> int:
    """Owner 上午两笔 inferred 回放对拍（证据生成器，写 replay/ 隔离目录）。

    输入：--fills（R37/R47 fills 协议文件，真实成交）+ --anchor CODE=QTY,...
    （当日开盘前持仓，默认取 broker_state holdings）。
    重建：锚点 → 逐笔 fill 后 +2min 合成快照（price_ref=成交价，标 synthetic）
    → diff_holdings → inferred 行 → override_inferred_with_fills（±10min）
    → 对拍报告（逐笔：inferred vs fill 匹配/价差）。绝写生产 trades_inferred。
    """
    from services import t_trade_coaching as coach

    fills_payload = json.loads(Path(args.fills).read_text(encoding="utf-8"))
    fills_rows = fills_payload.get("fills") if isinstance(fills_payload, dict) \
        else fills_payload
    trade_date = date.fromisoformat(str(fills_payload.get("trade_date")
                                        if isinstance(fills_payload, dict)
                                        else date.today().isoformat()))
    account = str(args.account or "acct1")
    anchor: dict[str, int] = dict(_anchor_holdings())

    day_fills = [f for f in fills_rows
                 if str(f.get("trade_time") or "").startswith(trade_date.isoformat())
                 and (f.get("account") in (None, "", account))]
    day_fills.sort(key=lambda f: str(f.get("trade_time")))

    # 合成快照序列：锚点(09:34) + 每笔后 +2min。
    # T+1 语义（与实盘一致）：买入→shares 增、available 不变；卖出→两者同减。
    anchor_shares = dict(anchor)
    events: list[dict[str, Any]] = [{
        "ts": datetime.combine(trade_date, dtime(9, 34)),
        "shares": dict(anchor_shares), "available": dict(anchor_shares)}]
    shares, available = dict(anchor_shares), dict(anchor_shares)
    from datetime import timedelta
    for f in day_fills:
        ts = datetime.strptime(str(f["trade_time"]), "%Y-%m-%d %H:%M:%S")
        d = int(f["shares"])
        code = str(f["code"])
        if str(f.get("side")).upper() == "BUY":
            shares[code] = int(shares.get(code, 0)) + d
        else:
            shares[code] = int(shares.get(code, 0)) - d
            available[code] = int(available.get(code, 0)) - d
        events.append({"ts": ts + timedelta(minutes=2),
                       "shares": dict(shares), "available": dict(available),
                       "fill": f})

    rows_seq: list[list[dict[str, Any]]] = []
    inferred: list[dict[str, Any]] = []
    for i, ev in enumerate(events):
        rows_seq.append([
            {"code": c, "name": "", "shares": q,
             "available_shares": ev["available"].get(c, q),
             "current_price": None}
            for c, q in ev["shares"].items() if q > 0])
        if i == 0:
            continue
        prev_rows, cur_rows = rows_seq[i - 1], rows_seq[i]
        # price_ref 用触发本快照那笔 fill 的真实成交价（标 synthetic）
        fill = ev.get("fill")
        if fill:
            for r in cur_rows:
                if r["code"] == str(fill["code"]):
                    r["current_price"] = float(fill["price"])
        changes = poll.diff_holdings(prev_rows, cur_rows)
        inferred.extend(poll.build_inferred_rows(
            account, ev["ts"], changes,
            f"synthetic_{i - 1:02d}", f"synthetic_{i:02d}"))
    for row in inferred:
        row["source"] = "snapshot_diff_replay(synthetic)"

    overrides = coach.override_inferred_with_fills(inferred, fills_rows)

    out_dir = Path(args.out_dir) if args.out_dir else (
        _tracker_root() / "replay" / f"r48_morning_{trade_date.strftime('%Y%m%d')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "schema": "ths_holdings_poll_replay/1", "trade_date": trade_date.isoformat(),
        "fills_used": len(day_fills), "anchor": anchor,
        "inferred_rows": [{k: r.get(k) for k in
                           ("ts", "code", "side", "delta_qty", "price_ref",
                            "avail_delta")} for r in inferred],
        "overrides": [{k: r.get(k) for k in
                       ("ts", "code", "side", "delta_qty", "price_ref",
                        "fill_override")} for r in overrides],
        "all_matched": all(
            (r.get("fill_override") or {}).get("status") == "matched"
            for r in overrides),
    }
    out_file = out_dir / "replay_report.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    (out_dir / "inferred.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n"
                for r in overrides), encoding="utf-8")
    print(json.dumps({"schema": SCHEMA, "command": "poll-holdings",
                      "mode": "replay-morning",
                      "status": "all_matched" if report["all_matched"] else "mismatch",
                      "credentials_touched": False,
                      "replay": {"all_matched": report["all_matched"],
                                 "fills_used": report["fills_used"],
                                 "report": str(out_file)}}, ensure_ascii=False))
    return 0 if report["all_matched"] else 3


def _anchor_holdings() -> list[tuple[str, int]]:
    """当日开盘前持仓锚点：broker_state.json holdings（权威=15:15 同步产物）。"""
    try:
        from utils.paths import runtime_path
        broker_file = Path(runtime_path("gen3_state_alpha")) / "broker_state.json"
    except Exception:
        broker_file = Path(r"F:\AiStock\data\data\runtime\gen3_state_alpha\broker_state.json")
    payload = json.loads(broker_file.read_text(encoding="utf-8"))
    out = []
    for h in payload.get("holdings") or []:
        code = str(h.get("code") or "").split(".")[0]
        qty = int(h.get("qty") or h.get("shares") or 0)
        if code and qty:
            out.append((code, qty))
    return out

# ══════════════════════════════════════════════════════════════════════════════
# status 段：聚合一览（全只读，绝不触窗口内容）——进程 / 面板 / 登录态 /
# 各账号身份锁定（面板注册表解析）/ 最近 fills 产物 / 快照产物与轮次状态 /
# 最近告警（broker_state 同步告警 + 巡读 poller_state + 最近 login 报告）。
# ══════════════════════════════════════════════════════════════════════════════


def _tail_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    rows = poll.read_jsonl(path)  # services 层已有 tolerant reader
    return rows[-limit:] if limit else rows


def _latest_login_report() -> Optional[dict[str, Any]]:
    root = _runtime_dir("ths_xiadan_client")
    files = sorted(root.glob("report_*.json"), reverse=True)
    if not files:
        return None
    try:
        payload = json.loads(files[0].read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"file": str(files[0]), "error": f"{type(exc).__name__}: {exc}"}
    return {"file": str(files[0]), "status": payload.get("status"),
            "login_state": payload.get("login_state"),
            "finished_at": payload.get("finished_at"),
            "reason": payload.get("reason")}


def _latest_fills_for_account(root: Path, account: str) -> Optional[dict[str, Any]]:
    """fills 目录内该账号最新产物（fills 文件与 unavailable 标记都算）。"""
    fills_dir = root / "fills"
    if not fills_dir.is_dir():
        return None
    # acct1=legacy 无后缀 + _acct1 镜像；其他账号=YYYYMMDD_<account>[.unavailable]
    esc = "(?:_acct1)?" if account == "acct1" else re.escape(f"_{account}")
    pat = re.compile(r"^\d{8}" + esc + r"(?:\.unavailable)?\.json$")
    files = sorted((p for p in fills_dir.iterdir()
                    if p.is_file() and pat.match(p.name)), reverse=True)
    if not files:
        return None
    path = files[0]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"file": str(path), "error": f"{type(exc).__name__}: {exc}"}
    return {"file": str(path), "status": payload.get("status"),
            "captured_at": payload.get("captured_at"),
            "trade_date": payload.get("trade_date"),
            "fills_total": len(payload.get("fills") or []),
            "reason": payload.get("reason")}


def _snapshots_summary(root: Path, recent_days: int = 2) -> dict[str, Any]:
    base = root / "holdings_snapshots"
    out: dict[str, Any] = {"root": str(base)}
    if not base.is_dir():
        out["note"] = "no_snapshots_dir"
        return out
    days_out: list[dict[str, Any]] = []
    for day_dir in sorted((d for d in base.iterdir() if d.is_dir()),
                          reverse=True)[:recent_days]:
        entry: dict[str, Any] = {"date": day_dir.name}
        state_file = day_dir / "poller_state.json"
        if state_file.is_file():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                entry["poller_state"] = {
                    a: {"rounds": s.get("rounds"),
                        "consecutive_failures": s.get("consecutive_failures"),
                        "alert": s.get("alert")}
                    for a, s in (state.get("accounts") or {}).items()}
            except Exception as exc:  # noqa: BLE001
                entry["poller_state"] = {"error": f"{type(exc).__name__}: {exc}"}
        latest: dict[str, str] = {}
        for snap in sorted(day_dir.glob("*_acct*.json")):
            latest[snap.stem.split("_")[-1]] = str(snap)  # 升序遍历，留最新
        entry["latest_snapshots"] = latest
        days_out.append(entry)
    out["days"] = days_out
    return out


def collect_status(*, fills_root: Optional[Path] = None,
                   recent: int = 5) -> dict[str, Any]:
    """聚合一览（全只读）。单段失败只降级该段（{"error": ...}），绝不弹窗。"""

    def guard(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - status 聚合不因单段失败而失败
            return {"error": f"{type(exc).__name__}: {exc}"}

    root = Path(fills_root) if fills_root is not None else _tracker_root()
    out: dict[str, Any] = {"t_tracker_root": str(root)}
    out["processes"] = guard(lambda: {
        HEXIN_PROCESS: _pids_of(HEXIN_PROCESS),
        XIADAN_PROCESS: _pids_of(XIADAN_PROCESS),
    })
    live_panels = guard(lambda: bridge.enumerate_trade_panels(
        include_minimized=True))
    out["panels"] = live_panels
    state = guard(lambda: _poll_state())
    if isinstance(state, tuple) and len(state) == 3:
        s, windows, main = state
        out["login_state"] = {"state": s, "main_window": main,
                              "visible_window_count": len(windows)}
    else:
        out["login_state"] = state

    accounts: dict[str, Any] = {}
    rules: dict[str, dict[str, Any]] = {}
    resolution: Any = None
    try:
        registry = bridge.load_panel_registry()
        resolution = registry.resolve(live_panels if isinstance(live_panels, list)
                                      else [])
        rules = {str(r.get("account")): r for r in (resolution.rules or [])}
    except Exception as exc:  # noqa: BLE001
        accounts["_registry_error"] = f"{type(exc).__name__}: {exc}"
    if resolution is not None:
        out["panel_resolution"] = {
            "unbound_panels": resolution.unbound,
            "ambiguous_accounts": resolution.ambiguous_accounts,
            "hotswitch_groups": {str(k): v for k, v
                                 in (resolution.hotswitch_groups or {}).items()},
        }
    for account in KNOWN_ACCOUNTS:
        info: dict[str, Any] = {
            "identity_hint": (rules.get(account) or {}).get("identity_hint"),
        }
        bound = ((resolution.by_account or {}).get(account)
                 if resolution is not None else None)
        info["panel_bound"] = bool(bound)
        if isinstance(bound, dict):
            info["bound_panel"] = bound.get("panel")
        info["latest_fills"] = _latest_fills_for_account(root, account)
        accounts[account] = info
    out["accounts"] = accounts

    out["snapshots"] = guard(lambda: _snapshots_summary(root))
    alerts = guard(lambda: _tail_jsonl(
        root / "broker_state_sync_alerts.jsonl", recent))
    out["recent_alerts"] = {
        "broker_state_sync": alerts,
        "latest_login_report": guard(_latest_login_report),
    }
    return out


def cmd_status(args: argparse.Namespace) -> int:
    detail = collect_status(
        fills_root=Path(args.fills_root) if args.fills_root else None,
        recent=args.recent)
    envelope = {"schema": SCHEMA, "command": "status", "status": "ok",
                "credentials_touched": False,
                "collected_at": datetime.now().isoformat(sep=" ",
                                                         timespec="seconds"),
                "detail": detail}
    print(json.dumps(envelope, ensure_ascii=False, indent=2, default=str))
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# daily 段：15:05 cron 全链 = login 检查 → export-fills → 汇总报告。
# login 未就绪（need_manual_login/失败）→ 绝不导出（fail-closed）。
# ══════════════════════════════════════════════════════════════════════════════


def _execute_export_fills(*, for_date: date, now: datetime,
                          out_root: Optional[Path] = None, force: bool = False,
                          account: Optional[str] = None) -> dict[str, Any]:
    """多账号导出执行体（export-fills 子命令与 daily 共用；原 ths_fills_export.main）。

    include_minimized：15:05 面板常最小化——解析须见窗（导出既有语义会
    恢复→导出→复最小化），否则 acct2 永远 panel_absent。
    R51 ③：单账号采集（--account 唯一或计划仅一账号）才允许 ComboBox 备用锚
    降级验证身份；双账号（热切换共享面板）保持 fail-closed。
    """
    try:
        live_panels = bridge.enumerate_trade_panels(include_minimized=True)
    except Exception:  # 非 host/无窗口环境 → 空面板表，acct1 走默认 connect
        live_panels = []
    plan = build_capture_plan(live_panels, bridge.load_panel_registry(),
                              account_filter=account)
    resolution = plan.pop("_resolution", {}) or {}
    to_capture = [a for a in plan if not account or a == account]
    combo_fallback = len(to_capture) == 1
    for account_id, entry in plan.items():
        if account and account_id != account:
            continue
        panel = entry.get("panel")
        entry["capture"] = (lambda ev, p=panel, a=account_id,
                            h=entry.get("identity_hint"),
                            sw=entry.get("hotswitch"),
                            cf=combo_fallback:
                            _capture_ths_views(ev, panel=p, account=a,
                                               identity_hint=h,
                                               hotswitch=sw,
                                               combo_identity_fallback=cf))
    plan["_resolution"] = resolution
    result = run_export_multi(for_date=for_date, now=now, out_root=out_root,
                              plan=plan, force=force,
                              known_accounts=[account] if account
                              else list(KNOWN_ACCOUNTS))
    result["trade_date"] = for_date.isoformat()
    return result


def run_daily(*, for_date: Optional[date] = None, now: Optional[datetime] = None,
              out_root: Optional[Path] = None, force: bool = False,
              account: Optional[str] = None, report_root: Optional[Path] = None,
              hexin_exe: Optional[Path] = None, xiadan_exe: Optional[Path] = None,
              launch_wait: float = LAUNCH_WAIT_SEC,
              direct_wait: float = DIRECT_WAIT_SEC,
              window_wait: float = WINDOW_WAIT_SEC,
              login_timeout: float = LOGIN_WAIT_SEC) -> tuple[dict[str, Any], int]:
    """全链：login 检查 → export-fills → 汇总（给 15:05 cron，替代 ths_fills_daily）。

    rc：0=导出 ok/skip；1=login 未就绪或链路异常；2=在场账号 unavailable。
    报告原子写 runtime/ths_client/daily_<时间戳>.json；credentials_touched 恒 false。
    """
    now = now or datetime.now()
    for_date = for_date or now.date()
    kwargs: dict[str, Any] = {}
    if hexin_exe is not None:
        kwargs["hexin_exe"] = hexin_exe
    if xiadan_exe is not None:
        kwargs["xiadan_exe"] = xiadan_exe
    daily: dict[str, Any] = {
        "schema": SCHEMA, "command": "daily", "status": "failure",
        "credentials_touched": False,
        "trade_date": for_date.isoformat(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "steps": [],
    }
    rc = 1
    try:
        login_report = run(launch_wait=launch_wait, direct_wait=direct_wait,
                           window_wait=window_wait, login_timeout=login_timeout,
                           export=False, report_root=report_root, **kwargs)
        daily["login"] = {k: login_report.get(k) for k in
                          ("status", "login_state", "xiadan_pids",
                           "report_path", "reason")}
        daily["steps"].append({"step": "login",
                               "status": login_report.get("status")})
        if login_report.get("status") != "logged_in":
            daily["status"] = f"login_{login_report.get('status')}"
            daily["export_skipped"] = "not_logged_in"  # fail-closed：未登录绝不采集
        else:
            result = _execute_export_fills(for_date=for_date, now=now,
                                           out_root=out_root, force=force,
                                           account=account)
            rc = int(result.get("rc", 0))
            daily["fills_export"] = result
            daily["summary"] = {
                acct: {k: r.get(k) for k in
                       ("status", "fills_total", "fills_today", "fills_file")}
                for acct, r in (result.get("accounts") or {}).items()}
            daily["status"] = "unavailable" if rc == 2 else "ok"
            daily["steps"].append({"step": "export_fills", "rc": rc})
    except Exception as exc:  # noqa: BLE001 - fail-closed：异常如实入报告
        daily["status"] = "failure"
        daily["reason"] = f"{type(exc).__name__}: {exc}"
        rc = 1
    finally:
        daily["finished_at"] = datetime.now(timezone.utc).isoformat()
        daily["rc"] = rc
        try:
            _write_report(daily,
                          report_root if report_root is not None
                          else _runtime_dir("ths_client"), prefix="daily")
        except Exception:  # noqa: BLE001 - 报告写失败不影响返回值语义
            pass
    return daily, rc


def cmd_daily(args: argparse.Namespace) -> int:
    daily, rc = run_daily(
        for_date=_parse_for_date(args.date),
        out_root=Path(args.out_dir) if args.out_dir else None,
        force=args.force, account=args.account,
        hexin_exe=args.hexin_exe, xiadan_exe=args.xiadan_exe,
        launch_wait=args.launch_wait, direct_wait=args.direct_wait,
        window_wait=args.window_wait, login_timeout=args.login_timeout)
    print(json.dumps(daily, ensure_ascii=False, indent=2, default=str))
    return rc


# ══════════════════════════════════════════════════════════════════════════════
# 统一 dispatcher：六子命令路由 + 统一 JSON envelope（schema ths_client/1）。
# ══════════════════════════════════════════════════════════════════════════════


def cmd_login(args: argparse.Namespace) -> int:
    report = run(
        hexin_exe=args.hexin_exe, xiadan_exe=args.xiadan_exe,
        python_exe=args.python_exe, export_script=args.export_script,
        launch_wait=args.launch_wait, direct_wait=args.direct_wait,
        window_wait=args.window_wait, login_timeout=args.login_timeout,
        export_timeout=args.export_timeout, dry_run=args.dry_run,
        export=args.export)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["status"] in {"logged_in", "success", "dry_run"} else 1


def cmd_export_fills(args: argparse.Namespace) -> int:
    for_date = _parse_for_date(args.date)
    result = _execute_export_fills(
        for_date=for_date, now=datetime.now(),
        out_root=Path(args.out_dir) if args.out_dir else None,
        force=args.force, account=args.account)
    rc = int(result.get("rc", 0))
    statuses = [r.get("status") for r in (result.get("accounts") or {}).values()]
    status = ("unavailable" if rc == 2
              else "skip" if statuses and all(s == "skip" for s in statuses)
              else "ok")
    envelope = {"schema": SCHEMA, "command": "export-fills", "status": status,
                "credentials_touched": False,
                "trade_date": for_date.isoformat(),
                "fills_export": result, "rc": rc}
    print(json.dumps(envelope, ensure_ascii=False, default=str))
    return rc


def cmd_poll_holdings(args: argparse.Namespace) -> int:
    if getattr(args, "replay_morning", False):
        if not getattr(args, "fills", None):
            print(json.dumps({"schema": SCHEMA, "command": "poll-holdings",
                              "mode": "replay-morning", "status": "error",
                              "reason": "--fills required for replay-morning",
                              "credentials_touched": False},
                             ensure_ascii=False))
            return 2
        return cmd_replay_morning(args)
    if getattr(args, "probe_capture", False):
        return cmd_probe(args)
    # 默认单轮巡读（--once 兼容旧 schtask 语义，不传亦同）
    return cmd_poll_once(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ths_client",
        description="同花顺统一客户端（login/export-fills/poll-holdings/survey/"
                    "status/daily；fail-closed，凭证零接触，绝不触碰委托下单界面）")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser("login", help="启动链+登录态判定+凭证记住时点登录"
                                     "（原 ths_xiadan_client）")
    p.add_argument("--hexin-exe", type=Path, default=HEXIN_EXE_DEFAULT)
    p.add_argument("--xiadan-exe", type=Path, default=XIADAN_EXE_DEFAULT)
    p.add_argument("--python-exe", type=Path, default=PYTHON_EXE_DEFAULT)
    p.add_argument("--export-script", type=Path, default=EXPORT_SCRIPT_DEFAULT)
    p.add_argument("--launch-wait", type=float, default=LAUNCH_WAIT_SEC)
    p.add_argument("--direct-wait", type=float, default=DIRECT_WAIT_SEC)
    p.add_argument("--window-wait", type=float, default=WINDOW_WAIT_SEC)
    p.add_argument("--login-timeout", type=float, default=LOGIN_WAIT_SEC)
    p.add_argument("--export-timeout", type=float, default=EXPORT_TIMEOUT_SEC)
    p.add_argument("--export", action="store_true",
                   help="就绪后运行 export-fills 子命令（PYTHONPATH 已剥离）")
    p.add_argument("--dry-run", action="store_true",
                   help="只做启动链 + 登录态判定：不点登录、不导出")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("export-fills", help="多账号当日成交导出"
                                            "（原 ths_fills_export，15:05）")
    p.add_argument("--once", action="store_true",
                   help="兼容旧 CLI：默认即单次导出（幂等）")
    p.add_argument("--date", help="YYYY-MM-DD（默认今天）")
    p.add_argument("--force", action="store_true", help="忽略幂等跳过，强制重导")
    p.add_argument("--out-dir", help="覆盖 t_tracker 根目录（测试用）")
    p.add_argument("--history-window-days", type=int,
                   default=DEFAULT_HISTORY_WINDOW_DAYS,
                   help="历史成交配对窗口天数（默认 10）")
    p.add_argument("--account", help="只导出指定账号（默认全部绑定账号循环）")
    p.set_defaults(func=cmd_export_fills)

    p = sub.add_parser("poll-holdings", help="盘中零交互持仓巡读"
                                             "（原 ths_holdings_poll，默认单轮）")
    p.add_argument("--once", action="store_true",
                   help="单轮巡读（默认模式：窗口守卫+秒错峰）")
    p.add_argument("--probe-capture", action="store_true",
                   help="单面板一次零交互采集（验证 PrintWindow/OCR 链）")
    p.add_argument("--replay-morning", action="store_true",
                   help="回放对拍：fills 重建 inferred→覆盖匹配（写 replay/）")
    p.add_argument("--account")
    p.add_argument("--now", help="HH:MM[:SS]（当日时刻，测试用）")
    p.add_argument("--out-dir")
    p.add_argument("--no-stagger", action="store_true")
    p.add_argument("--fills", help="replay-morning 用：fills 协议文件路径")
    p.set_defaults(func=cmd_poll_holdings)

    p = sub.add_parser("survey", help="L2 面板 UIA 零风险勘察/绑定取证"
                                      "（原 l2_panel_survey）")
    p.add_argument("--account", default="acct1", help="目标账号（默认 acct1 曾*）")
    p.add_argument("--allow-hotswitch", action="store_true",
                   help="面板在另一已知账号时允许经账号切换器 ComboBox "
                        "SelectionItem 切到目标（默认零交互）")
    p.add_argument("--walker", choices=["control", "raw"], default="control")
    p.add_argument("--max-nodes", type=int, default=MAX_NODES)
    p.set_defaults(func=cmd_survey)

    p = sub.add_parser("status", help="聚合一览：进程/面板/登录态/身份锁定/"
                                      "最近 fills/快照/告警（全只读）")
    p.add_argument("--fills-root", help="覆盖 t_tracker 根目录（默认 runtime/t_tracker）")
    p.add_argument("--recent", type=int, default=5, help="最近告警条数")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("daily", help="全链：login 检查→export-fills→汇总"
                                     "（15:05 cron 用）")
    p.add_argument("--date", help="YYYY-MM-DD（默认今天）")
    p.add_argument("--force", action="store_true", help="忽略幂等跳过，强制重导")
    p.add_argument("--out-dir", help="覆盖 t_tracker 根目录（测试用）")
    p.add_argument("--account")
    p.add_argument("--hexin-exe", type=Path, default=HEXIN_EXE_DEFAULT)
    p.add_argument("--xiadan-exe", type=Path, default=XIADAN_EXE_DEFAULT)
    p.add_argument("--launch-wait", type=float, default=LAUNCH_WAIT_SEC)
    p.add_argument("--direct-wait", type=float, default=DIRECT_WAIT_SEC)
    p.add_argument("--window-wait", type=float, default=WINDOW_WAIT_SEC)
    p.add_argument("--login-timeout", type=float, default=LOGIN_WAIT_SEC)
    p.set_defaults(func=cmd_daily)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


# ══════════════════════════════════════════════════════════════════════════════
# survey 段（原 scripts/l2_panel_survey.py：R50 Phase1 L2 面板 UIA 零风险勘察）。
# 【自审计边界】_self_audit 自 _SURVEY_SECTION_MARKER 起扫描至文件尾——本段
# 之后禁止出现任何输入/激活原语；新增非勘察代码请插到标记之前，勘察代码
# 永远放文件最后。原文件已改为薄 wrapper 转发至此。
# ══════════════════════════════════════════════════════════════════════════════
_SURVEY_SECTION_MARKER = "# ==== SURVEY SECTION (self-audit scans from here to EOF) ===="

OUT_DIR = Path(r"F:\Aistock\data\data\runtime\l2_r50")
PANEL_MAP = OUT_DIR / "panel_map.json"
JOURNAL = OUT_DIR / "journal_r50_phase1.md"
PANELS_JSON = Path(r"F:\Aistock\data\data\runtime\gen3_state_alpha\ths_panels.json")
PANEL_MAP_SCHEMA = "l2_panel_map/1"

# 提交/撤单类按钮名令牌（命中即入 submit_buttons，Phase1 绝不 Invoke）
SUBMIT_TOKENS = ("买入", "卖出", "买入委托", "卖出委托", "委托下单", "下单",
                 "提交", "撤单", "撤单", "全撤", "一键", "清空")
MAX_NODES = 5000
MAX_DEPTH = 24


def _ts() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def journal(text: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as fh:
        fh.write(f"- `{_ts()}` {text}\n")


def _write_map(payload: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PANEL_MAP.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(PANEL_MAP)


# ── 源码级自审计：本文件不得含任何输入/激活原语 ──────────────────
# 令牌用相邻字面量拼接，避免审计串出现在本文件源码中造成自命中。
_FORBIDDEN_TOKENS = (
    ".invoke" "(", "invoke_pattern" ".Invoke", ".set_" "value(", ".set_" "focus(",
    "click_" "input", ".click" "(", "type_" "keys", "send_" "keys",
    "Set" "Value", "Set" "Focus", "Mouse" "Click", "keybd_" "event",
    "Send" "Input", "Post" "MessageW", "Send" "MessageW",
)


def _self_audit() -> None:
    """源码扫描：本文件一旦出现任何输入/激活原语立即拒绝运行。"""
    src = Path(__file__).read_text(encoding="utf-8")
    start = src.find(_SURVEY_SECTION_MARKER)
    if start < 0:   # pragma: no cover - 防勘察段标记被误删
        raise RuntimeError("L2 survey self-audit FAILED: survey marker missing")
    # 只审计勘察段（标记之后至文件尾）：login 段点「登录」按钮 / fills 段点
    # 查询菜单是各自任务书获准的交互面，不在勘察禁令内；勘察代码永远放最后。
    src = src[start:]
    hits = [t for t in _FORBIDDEN_TOKENS if t in src]
    if hits:   # pragma: no cover - 防未来改动引入危险调用
        raise RuntimeError(f"L2 survey self-audit FAILED: {hits}")


# ── UIA 只读枚举（comtypes 原生，零交互：只 GetCurrent*） ─────────

class UiaReader:
    """comtypes UIA 封装（只读）。属性经 GetCurrentPropertyValue/Current* 读取。"""

    def __init__(self) -> None:
        from comtypes.client import CreateObject, GetModule
        GetModule("UIAutomationCore.dll")
        import comtypes.gen.UIAutomationClient as UIAC  # noqa: F401
        self.UIAC = UIAC
        self.uia = CreateObject(UIAC.CUIAutomation,
                                interface=UIAC.IUIAutomation)

    def element_from_handle(self, hwnd: int):
        return self.uia.ElementFromHandle(hwnd)

    def walker(self, raw: bool):
        return self.uia.RawViewWalker if raw else self.uia.ControlViewWalker

    @staticmethod
    def _safe_call(fn, *args):
        """易失控件（tooltip/动画/已销毁）上 walker 调用会 E_POINTER——
        读失败按"无"处理（只读勘察不重试不打扰，如实记录可读部分）。"""
        try:
            return fn(*args)
        except Exception:  # noqa: BLE001 - COMError E_POINTER 等
            return None

    def _prop(self, elem, name: str, default: Any = None) -> Any:
        try:
            return getattr(elem, name)
        except Exception:  # noqa: BLE001 - 单属性失败不阻断整树
            return default

    def read_node(self, elem) -> dict:
        UIAC = self.UIAC
        rect = self._prop(elem, "CurrentBoundingRectangle")
        l = t = r = b = None
        if rect is not None:
            l = getattr(rect, "left", getattr(rect, "l", None))
            t = getattr(rect, "top", getattr(rect, "t", None))
            r = getattr(rect, "right", getattr(rect, "r", None))
            b = getattr(rect, "bottom", getattr(rect, "b", None))
        node: dict[str, Any] = {
            "automation_id": self._prop(elem, "CurrentAutomationId", "") or "",
            "name": (self._prop(elem, "CurrentName", "") or "")[:80],
            "class_name": self._prop(elem, "CurrentClassName", "") or "",
            "control_type": int(self._prop(elem, "CurrentControlType", 0) or 0),
            "localized_type": self._prop(elem, "CurrentLocalizedControlType", "") or "",
            "rect": [int(l) if l is not None else None,
                     int(t) if t is not None else None,
                     int(r) if r is not None else None,
                     int(b) if b is not None else None],
            "enabled": bool(self._prop(elem, "CurrentIsEnabled", False)),
            "offscreen": bool(self._prop(elem, "CurrentIsOffscreen", True)),
            "keyboard_focusable": bool(self._prop(elem, "CurrentIsKeyboardFocusable", False)),
            "runtime_id": [int(x) for x in
                           (self._prop(elem, "CurrentRuntimeId") or [])][:6],
            "patterns": {},
        }
        is_password = bool(self._prop(elem, "CurrentIsPassword", False))
        node["is_password"] = is_password

        def avail(pid) -> bool:
            try:
                return bool(elem.GetCurrentPropertyValue(pid))
            except Exception:  # noqa: BLE001
                return False

        # 可读写性标注（ValuePattern 写入能力=available 且非只读；今晚绝不写）
        try:
            if avail(UIAC.UIA_IsValuePatternAvailablePropertyId):
                unk = elem.GetCurrentPattern(UIAC.UIA_ValuePatternId)
                if unk:
                    vp = unk.QueryInterface(UIAC.IUIAutomationValuePattern)
                    node["patterns"]["value"] = {
                        "read_only": bool(vp.CurrentIsReadOnly),
                        "writable_phase2": not bool(vp.CurrentIsReadOnly),
                        "value": "" if is_password else str(vp.CurrentValue)[:60],
                    }
        except Exception as exc:  # noqa: BLE001
            node["patterns"]["value_error"] = f"{type(exc).__name__}"
        if avail(UIAC.UIA_IsInvokePatternAvailablePropertyId):
            node["patterns"]["invoke"] = {"available": True,
                                          "invoke_forbidden_phase1": True}
        if avail(UIAC.UIA_IsRangeValuePatternAvailablePropertyId):
            node["patterns"]["range_value"] = {"available": True}
        if avail(UIAC.UIA_IsTogglePatternAvailablePropertyId):
            try:
                unk = elem.GetCurrentPattern(UIAC.UIA_TogglePatternId)
                if unk:
                    tp = unk.QueryInterface(UIAC.IUIAutomationTogglePattern)
                    node["patterns"]["toggle"] = {
                        "state": str(tp.CurrentToggleState)}
            except Exception:  # noqa: BLE001
                node["patterns"]["toggle"] = {"available": True}
        if avail(UIAC.UIA_IsSelectionItemPatternAvailablePropertyId):
            try:
                unk = elem.GetCurrentPattern(UIAC.UIA_SelectionItemPatternId)
                if unk:
                    sp = unk.QueryInterface(UIAC.IUIAutomationSelectionItemPattern)
                    node["patterns"]["selection_item"] = {
                        "selected": bool(sp.CurrentIsSelected)}
            except Exception:  # noqa: BLE001
                node["patterns"]["selection_item"] = {"available": True}
        return node

    def walk_tree(self, root_elem, raw: bool = False,
                  max_nodes: int = MAX_NODES, max_depth: int = MAX_DEPTH,
                  progress_cb=None) -> tuple[dict, int]:
        """迭代式深度优先（ControlView 默认），返回 (根节点树, 总节点数)。"""
        walker = self.walker(raw)
        total = 0
        truncated = False

        def build(elem, depth: int) -> dict:
            nonlocal total, truncated
            total += 1
            if total > max_nodes:
                truncated = True
                return {"_truncated": True}
            node = self.read_node(elem)
            if depth < max_depth:
                children = []
                child = self._safe_call(walker.GetFirstChildElement, elem)
                guard = 0
                while child is not None and guard < max_nodes:
                    guard += 1
                    children.append(build(child, depth + 1))
                    nxt = self._safe_call(walker.GetNextSiblingElement, child)
                    child = nxt
                node["children"] = children
            else:
                node["children"] = []
                node["_depth_truncated"] = True
            if progress_cb and total % 200 == 0:
                progress_cb(total)
            return node

        tree = build(root_elem, 0)
        return tree, total


# ── 弹窗记录（同 pid 其他可见顶层窗口，只记录零处理） ─────────────

def enumerate_sibling_dialogs(pid: int, main_hwnd: int) -> list[dict]:
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    out: list[dict] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd: int, _l: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or int(hwnd) == int(main_hwnd):
            return True
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value != pid:
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 2)
        user32.GetWindowTextW(hwnd, buf, n + 2)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        out.append({"hwnd": hex(hwnd), "title": buf.value,
                    "class": cls.value,
                    "rect": [rect.left, rect.top, rect.right, rect.bottom],
                    "action": "recorded_only_no_confirm"})
        return True

    user32.EnumWindows(_cb, 0)
    return out


def classify_submit_buttons(nodes_iter) -> list[dict]:
    """按名称令牌识别提交/撤单类按钮——今晚绝不 Invoke。"""
    submits = []
    for node, path in nodes_iter:
        name = str(node.get("name") or "")
        if not any(tok in name for tok in SUBMIT_TOKENS):
            continue
        submits.append({
            "path": path,
            "automation_id": node.get("automation_id"),
            "name": name,
            "class_name": node.get("class_name"),
            "control_type": node.get("localized_type"),
            "rect": node.get("rect"),
            "invoke_available": bool((node.get("patterns") or {}).get("invoke")),
            "enabled": node.get("enabled"),
            "invoke_forbidden_phase1": True,
            "note": "Phase1 勘察绝不 Invoke；Phase2+ 须 Owner 批准",
        })
    return submits


def flatten(tree: dict):
    """深度优先展开 (node, path)，path 形如 '0/3/1'（测绘定位用）。"""
    def rec(node: dict, path: str):
        yield node, path
        for i, ch in enumerate(node.get("children") or []):
            if isinstance(ch, dict) and not ch.get("_truncated"):
                yield from rec(ch, f"{path}/{i}")
    yield from rec(tree, "")


def load_panel_rules() -> list[dict]:
    try:
        data = json.loads(PANELS_JSON.read_text(encoding="utf-8"))
        return list(data.get("rules") or [])
    except Exception:  # noqa: BLE001
        return []


def _show_window(hwnd: int, cmd: int) -> None:
    ctypes.windll.user32.ShowWindow(hwnd, cmd)


def _is_iconic(hwnd: int) -> bool:
    return bool(ctypes.windll.user32.IsIconic(hwnd))


class _WinHandle:
    """hotswitch/poll 需要的 window 对象最小面（仅 handle）。"""

    def __init__(self, hwnd: int) -> None:
        self.handle = hwnd


def _survey_main(args: argparse.Namespace) -> int:
    """勘察主体（参数解析上收到统一 dispatcher 的 survey 子命令，行为不变）。"""

    _self_audit()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    journal(f"survey start account={args.account} walker={args.walker} "
            f"hotswitch={'on' if args.allow_hotswitch else 'off'} "
            f"(iron_law: 零输入零Invoke零点击零交易)")

    # 1) 面板枚举（include_minimized=True：R49b 最小化面板可解析）
    panels = bridge.enumerate_trade_panels(include_minimized=True)
    journal(f"panels={len(panels)} " +
            "; ".join(f"{p['process']}:{p['title'][:24]}@{p['hwnd']}"
                      for p in panels))
    if not panels:
        payload = {"schema": PANEL_MAP_SCHEMA, "status": "panel_absent",
                   "surveyed_at": _ts(),
                   "note": "全系统无交易面板（EnumWindows 零命中）。需 Owner 登录"
                           "交易客户端后重跑本脚本。fail-closed：绝不自启客户端。"}
        _write_map(payload)
        journal("panel_absent → fail-closed 退出（rc=2），绝不自启交易客户端")
        return 2
    if len(panels) > 1:
        payload = {"schema": PANEL_MAP_SCHEMA, "status": "ambiguous_panels",
                   "surveyed_at": _ts(), "panels": panels,
                   "note": "多面板并存，测绘对象不唯一，fail-closed 待 PM 裁决"}
        _write_map(payload)
        journal(f"ambiguous_panels={len(panels)} → rc=6 fail-closed")
        return 6

    panel = panels[0]
    hwnd = int(str(panel["hwnd"]), 16)
    rules = load_panel_rules()
    rule_by_acct = {str(r.get("account")): r for r in rules}
    target_rule = rule_by_acct.get(args.account) or {}
    target_hint = str(target_rule.get("identity_hint") or "")
    known_hints = [(str(r.get("identity_hint")), str(r.get("account")))
                   for r in rules if r.get("identity_hint")]

    # 2) 窗口锁内完成全部面板侧操作
    try:
        lock_cm = bridge._ths_window_operation_lock(
            timeout_seconds=180, lock_name=bridge.HOTSWITCH_LOCK_NAME)
    except Exception as exc:  # noqa: BLE001
        journal(f"lock_setup_error {exc} → rc=5")
        return 5
    with lock_cm:
        if poll._is_foreground(hwnd):
            payload = {"schema": PANEL_MAP_SCHEMA, "status": "skipped_foreground",
                       "surveyed_at": _ts(), "panel": panel,
                       "note": "面板在前台（疑有人使用）→ 让位，绝不抢焦点"}
            _write_map(payload)
            journal("skipped_foreground → rc=3（零触碰）")
            return 3

        was_minimized = _is_iconic(hwnd)
        if was_minimized:
            _show_window(hwnd, 9)  # SW_RESTORE（PrintWindow 对 iconic 窗空白）
            time.sleep(0.5)
            journal("panel was minimized → restored for survey")

        # 3) 身份 fail-closed（顶栏 OCR 自证）
        try:
            label, ident_png = hotswitch.read_window_identity(
                _WinHandle(hwnd),
                save_png=OUT_DIR / "identity_check.png")
        except Exception as exc:  # noqa: BLE001
            label, ident_png = "", None
            journal(f"identity_read_error {type(exc).__name__}: {exc}")
        journal(f"identity_label={label!r} target_hint={target_hint!r}")
        status_identity = None
        if not target_hint:
            status_identity = "no_target_rule"
        elif target_hint not in label:
            hit = hotswitch._match_known(label, known_hints)
            if hit is None:
                status_identity = "identity_mismatch"
            elif args.allow_hotswitch and rule_by_acct.get(hit, {}).get("hotkey"):
                cfg = {"target_hint": target_hint,
                       "known_hints": known_hints}
                try:
                    # 生产窗口契约（fills_export/poll 同源）：pywinauto uia 包装
                    # 窗满足 hotswitch 服务的完整调用面（最小化/恢复）；
                    # 切换交互只出自受控 hotswitch 模块（R49c ComboBox
                    # SelectionItem），本文件自审计仍零输入原语。
                    win = bridge._find_window_for_panel(panel)
                    res = hotswitch.ensure_target_account(win, cfg)
                except Exception as exc:  # noqa: BLE001 - 窗口消失/连接失败
                    journal(f"hotswitch_window_error {type(exc).__name__}: {exc}")
                    res = {"status": "switch_failed",
                           "note": "窗口连接/热切换异常，fail-closed 零采集"}
                if res is not None:
                    status_identity = res.get("status", "switch_failed")
                else:
                    label2, _ = hotswitch.read_window_identity(
                        _WinHandle(hwnd), save_png=OUT_DIR / "identity_after_switch.png")
                    journal(f"hotswitch ok → identity={label2!r}")
                    label = label2
            else:
                status_identity = "not_target_account_no_hotswitch"

        if status_identity is not None:
            if was_minimized:
                _show_window(hwnd, 6)  # SW_MINIMIZE 复原
                journal("restored to minimized (identity fail-closed)")
            payload = {"schema": PANEL_MAP_SCHEMA, "status": status_identity,
                       "surveyed_at": _ts(), "panel": panel,
                       "identity_label": label, "target_hint": target_hint,
                       "note": "身份 fail-closed：零 UIA 采集零触碰"}
            _write_map(payload)
            journal(f"{status_identity} → rc=4（零触碰，身份标签 {label!r}）")
            return 4

        # 4) PrintWindow 截图证据
        shot = OUT_DIR / f"panel_full_{datetime.now().strftime('%H%M%S')}.png"
        try:
            poll.print_window_image(hwnd).save(shot)
            journal(f"screenshot {shot.name}")
        except Exception as exc:  # noqa: BLE001
            shot = None
            journal(f"screenshot_error {type(exc).__name__}: {exc}")

        # 5) UIA 全树测绘（零交互：只 GetCurrent*）
        try:
            reader = UiaReader()
            root = reader.element_from_handle(hwnd)
            tree, total = reader.walk_tree(
                root, raw=(args.walker == "raw"), max_nodes=args.max_nodes,
                progress_cb=lambda n: journal(f"walk progress {n} nodes"))
        except Exception as exc:  # noqa: BLE001
            if was_minimized:
                _show_window(hwnd, 6)
            payload = {"schema": PANEL_MAP_SCHEMA, "status": "uia_walk_error",
                       "surveyed_at": _ts(), "panel": panel,
                       "error": f"{type(exc).__name__}: {exc}"}
            _write_map(payload)
            journal(f"uia_walk_error {type(exc).__name__}: {exc} → rc=5")
            return 5

        # 6) 提交类按钮清单 + 弹窗记录
        submits = classify_submit_buttons(flatten(tree))
        dialogs = enumerate_sibling_dialogs(int(panel["pid"]), hwnd)
        if dialogs:
            journal(f"sibling_dialogs recorded (no confirm): "
                    + "; ".join(d["title"][:20] for d in dialogs))

        # 7) 复原最小化
        restored_min = False
        if was_minimized:
            _show_window(hwnd, 6)  # SW_MINIMIZE
            restored_min = True
            time.sleep(0.2)
            journal("restored to minimized after survey")

        payload = {
            "schema": PANEL_MAP_SCHEMA, "status": "ok",
            "surveyed_at": _ts(),
            "panel": panel,
            "identity_label": label, "account": args.account,
            "walker": args.walker, "node_count": total,
            "was_minimized": was_minimized, "restored_to_minimized": restored_min,
            "iron_law": {"zero_input": True, "zero_invoke": True,
                         "zero_click": True, "zero_trade": True,
                         "submit_buttons_never_invoke_phase1": True},
            "tree": tree,
            "submit_buttons": submits,
            "sibling_dialogs": dialogs,
            "screenshots": [shot.name] if shot else [],
            "next_phase_note": "Phase2 填单演练须 Owner 在场另派任务书；"
                               "本测绘绝不构成任何填单/提交授权",
        }
        _write_map(payload)
        journal(f"survey ok nodes={total} submits={len(submits)} "
                f"dialogs={len(dialogs)} → panel_map.json")
        return 0


def cmd_survey(args: argparse.Namespace) -> int:
    """survey 子命令：勘察主体 + 统一 envelope（panel_map.json 内容不变）。"""
    rc = _survey_main(args)
    try:
        payload = json.loads(PANEL_MAP.read_text(encoding="utf-8"))
        brief = {k: payload.get(k) for k in
                 ("status", "identity_label", "account", "node_count",
                  "was_minimized", "surveyed_at")}
        brief["submit_buttons"] = len(payload.get("submit_buttons") or [])
    except Exception as exc:  # noqa: BLE001
        brief = {"status": "map_unreadable", "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps({"schema": SCHEMA, "command": "survey",
                      "status": brief.get("status"),
                      "credentials_touched": False,
                      "panel_map": str(PANEL_MAP), "survey": brief, "rc": rc},
                     ensure_ascii=False, default=str))
    return rc


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
