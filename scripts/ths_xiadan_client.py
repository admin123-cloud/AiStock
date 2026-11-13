# -*- coding: utf-8 -*-
"""同花顺下单(xiadan) 自动登录 + 就绪客户端 —— fail-closed，零凭证接触。

仿 scripts/tdx_postclose_client.py 的失败即停风格。凭证铁律：绝不读取/
写入/记录任何密码框内容；只允许检测「密码框是否非空」（WM_GETTEXTLENGTH
长度位 / 控件样式位，绝不取文本）与点击「登录」按钮。

流程：
1. 启动链：hexin.exe 未运行 → Popen(['hexin.exe'], cwd='D:/同花顺')（必须带
   cwd，否则 hexinhelper/xiadan 不会拉起——实盘事实）；≤60s 等 xiadan.exe
   自动出现；未自动拉起 → 再 Popen D:/同花顺/xiadan.exe 兜底。
2. 登录态判定：枚举 xiadan PID 的可见顶层窗口：
   - 只有 #32770（空标题登录框，实盘事实）→ 校验形态（Edit 子控件 + 「登录」
     按钮）→ 密码框长度检测：
       非空（凭证已记住）→ BM_CLICK 点「登录」→ 8s 无过渡则真鼠标坐标点击
       兜底一次 → 等非 #32770 主窗口；
       空 → fail-closed status=need_manual_login（不点、不导出）。
   - 出现非 #32770 可见主窗口 → status=logged_in。
   窗口/控件缺失即停，绝不盲点。
3. --export：就绪后 subprocess 调
   F:/python/python3.10/python.exe scripts/ths_fills_export.py --once
   （子进程 env 剥离 PYTHONPATH——cron 环境 win32api DLL 污染事故），
   stdout 透传进本报告。
4. 报告原子写 runtime/ths_xiadan_client/report_<时间戳>.json
   （schema ths_xiadan_client/1，credentials_touched 恒为 false）。
5. --dry-run：只做启动链 + 登录态判定，不点登录、不导出。
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

IS_WINDOWS = os.name == "nt"


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
EXPORT_SCRIPT_DEFAULT = SCRIPTS_DIR / "ths_fills_export.py"
SCHEMA = "ths_xiadan_client/1"

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
    if not hit or _root(hit) != _root(hwnd):
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

    主窗口判定（实盘 2026-11-13 19:48 取证：盘后面板最小化/隐藏时
    IsWindowVisible=False，但窗口仍在、标题为「网上股票交易系统5.0」）：
      a) 可见且非 #32770（任务规范）；或
      b) 非 #32770 且标题含交易系统锚点（隐藏也代表已登录——fills 导出器
         自会恢复窗口）。
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
            if is_visible or _is_trade_window_title(title):
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
    if password is None and edits:
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
    """BM_CLICK 点「登录」；8s 无过渡则真鼠标坐标点击兜底一次；仍无主窗口即停。"""
    clicks: list[dict[str, Any]] = []
    _bring_to_front(login_hwnd)
    _message_int(button_hwnd, BM_CLICK)
    clicks.append({"method": "bm_click", "button_hwnd": button_hwnd})

    deadline = time.monotonic() + timeout
    fallback_at = time.monotonic() + min(8.0, timeout)
    used_fallback = False
    last_state = "dialog"
    while True:
        last_state, _wins, _main = _poll_state()
        if last_state == "main":
            return "logged_in_after_click", clicks
        now = time.monotonic()
        if now >= deadline:
            break
        if not used_fallback and last_state == "dialog" and now >= fallback_at:
            _click_window(button_hwnd)  # 覆盖校验 + 真鼠标点击，一次为限
            clicks.append({"method": "coordinate_click", "button_hwnd": button_hwnd})
            used_fallback = True
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
    if state == "main" and visible_login is None:
        return {"login_state": "logged_in", "windows": windows,
                "main_window": main, "clicks": [], "password_remembered": None}
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


# ── 导出子进程 ──

def _run_export(python_exe: Path, export_script: Path, timeout: float) -> dict[str, Any]:
    for label, path in (("python_exe", python_exe), ("export_script", export_script)):
        if not path.is_file():
            raise ThsXiadanError(f"export {label} not found: {path}")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)        # cron 环境 win32api DLL 污染事故：剥离
    env["PYTHONIOENCODING"] = "utf-8"  # 子进程管道编码（exporter 输出中文 JSON）
    cmd = [str(python_exe), str(export_script), "--once"]
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


# ── 报告 ──

def _write_report(report: dict[str, Any], root: Optional[Path] = None) -> Path:
    # 运行期产物一律落仓库外 runtime 目录（utils.paths）
    if root is None:
        try:
            from utils.paths import runtime_path

            root = runtime_path("ths_xiadan_client")
        except Exception:
            base = Path(os.environ.get("AISTOCK_DATA_ROOT", r"F:\AiStock\data\data"))
            root = base / "runtime" / "ths_xiadan_client"
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    path = root / f"report_{stamp}.json"
    report["report_path"] = str(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


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
        "status": "failure",
        "started_at": started.isoformat(),
        "dry_run": bool(dry_run),
        "export_requested": bool(export),
        "credentials_touched": False,
        "hexin_exe": str(hexin_exe),
        "xiadan_exe": str(xiadan_exe),
    }
    try:
        pids, steps = _ensure_processes(hexin_exe, xiadan_exe, launch_wait, direct_wait)
        report["launch"] = steps
        report["xiadan_pids"] = pids

        detail = _reach_login_state(
            window_wait=window_wait, login_timeout=login_timeout, dry_run=dry_run,
        )
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="同花顺下单(xiadan) 自动登录+就绪客户端（fail-closed，零凭证接触）",
    )
    parser.add_argument("--hexin-exe", type=Path, default=HEXIN_EXE_DEFAULT)
    parser.add_argument("--xiadan-exe", type=Path, default=XIADAN_EXE_DEFAULT)
    parser.add_argument("--python-exe", type=Path, default=PYTHON_EXE_DEFAULT)
    parser.add_argument("--export-script", type=Path, default=EXPORT_SCRIPT_DEFAULT)
    parser.add_argument("--launch-wait", type=float, default=LAUNCH_WAIT_SEC)
    parser.add_argument("--direct-wait", type=float, default=DIRECT_WAIT_SEC)
    parser.add_argument("--window-wait", type=float, default=WINDOW_WAIT_SEC)
    parser.add_argument("--login-timeout", type=float, default=LOGIN_WAIT_SEC)
    parser.add_argument("--export-timeout", type=float, default=EXPORT_TIMEOUT_SEC)
    parser.add_argument("--export", action="store_true",
                        help="就绪后运行 ths_fills_export.py --once（PYTHONPATH 已剥离）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只做启动链 + 登录态判定：不点登录、不导出")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    report = run(
        hexin_exe=args.hexin_exe,
        xiadan_exe=args.xiadan_exe,
        python_exe=args.python_exe,
        export_script=args.export_script,
        launch_wait=args.launch_wait,
        direct_wait=args.direct_wait,
        window_wait=args.window_wait,
        login_timeout=args.login_timeout,
        export_timeout=args.export_timeout,
        dry_run=args.dry_run,
        export=args.export,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["status"] in {"logged_in", "success", "dry_run"} else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
