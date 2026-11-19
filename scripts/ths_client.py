# -*- coding: utf-8 -*-
"""同花顺（THS）统一客户端 scripts/ths_client.py —— 单脚本收拢全部 THS 功能。

Owner 2026-11-13 要求：同花顺全部功能收拢到单一脚本。Owner 2026-11-14 裁决：
同花顺全部能力代码汇聚到本文件，开源组件可用逻辑以 vendored 方式并入（标注
来源与 MIT 许可），运行时不依赖外部包。八个子命令：

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
  read-account    远航版（D:/同花顺远航，bin/happ.exe）单账号只读链：
                  资金/持仓 + --fills 历史逐笔成交；GUI 串行锁全程覆盖；
                  采集器+纯决策两级裁决，证据不足如实 partial/unavailable；
                  **绝不切号**（活动≠目标 fail-closed rc4；切号走
                  switch-account；旧自动切换能力保留在
                  run_read_account_switchable 供双账号阶段复用）
  switch-account  远航版账号切换：主程序「交易」菜单真实坐标点击（坐标可
                  配置化 + 视觉辅助定位），Owner 活跃守卫 + 切换后验证
                  fail-closed（--dry-run 只定位不点击）

实现约束：
  * 复用 services/ 层（ths_account_bridge / ths_hotswitch / ths_holdings_poll），
    服务层单一事实源，本文件不复制其逻辑；远航版段（read-account/
    switch-account）例外：Owner 2026-11-14 裁决自包含，EasyTHS 技法 vendored
    并入并标注 MIT 来源，不经 services 经典面板链；
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
  python scripts/ths_client.py read-account --account acct1   # 远航版资金/持仓
  python scripts/ths_client.py switch-account --to acct2      # 远航版切账号
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

# 服务层复用（不复制逻辑）。2026-11-15 起改惰性加载：模块初始化零
# services 依赖（read-account 只读链不触 services；旧命令首次使用时才
# 导入，导入失败在首次访问点如实抛出）：
#   bridge   = services.ths_account_bridge（窗口锁/查询菜单/OCR/面板注册表/登录框检测）
#   poll     = services.ths_holdings_poll（巡读轮次/快照/diff/inferred 服务）
#   hotswitch= services.ths_hotswitch（R49c 账号切换器受控路径/身份多锚读取）
class _YhLazyLoader:
    """按名惰性加载 services 模块属性（旧调用点字节不变）。"""

    def __init__(self, loader):
        self._loader = loader

    def __getattr__(self, name):
        return getattr(self._loader(), name)


class _YhLazyCallable:
    """惰性函数代理：首调导入真实函数再转发（旧调用点字节不变）。"""

    def __init__(self, loader):
        self._loader = loader

    def __call__(self, *args, **kwargs):
        return self._loader()(*args, **kwargs)


def _yh_load_bridge():
    from services import ths_account_bridge as bridge_mod

    return bridge_mod


def _yh_load_poll():
    from services import ths_holdings_poll as poll_mod

    return poll_mod


def _yh_load_hotswitch():
    from services import ths_hotswitch as hotswitch_mod

    return hotswitch_mod


def _yh_load_build_hotswitch_plan():
    from services.ths_hotswitch import build_hotswitch_plan

    return build_hotswitch_plan


bridge = _YhLazyLoader(_yh_load_bridge)
poll = _YhLazyLoader(_yh_load_poll)
hotswitch = _YhLazyLoader(_yh_load_hotswitch)
build_hotswitch_plan = _YhLazyCallable(_yh_load_build_hotswitch_plan)


def _runtime_dir(name: str) -> Path:
    """运行期产物目录（仓库外 runtime 树；utils.paths 可用时走它）。"""
    try:
        from utils.paths import runtime_path

        return Path(runtime_path(name))
    except Exception:
        base = Path(os.environ.get("AISTOCK_RUNTIME_ROOT", r"F:\Aistock\runtime"))
        return base / name


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


_TOAST_MAX_AREA = 80_000  # px²；小于此的可见 #32770=通知/提示条（2026-11-14 冷启动实测 265×29 屏幕底角条）
_WM_CLOSE = 0x0010


def _dismiss_tiny_toasts(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """关闭微小可见 #32770 通知条（WM_CLOSE 零交互）。

    安全护栏：带「登录」按钮的对话框（真登录框）绝不关闭；登录框尺寸
    ≥300×200（60,000px² 边界重叠），护栏一 _find_login_button 双保险。
    返回关闭记录；调用方需重新 _poll_state。
    """
    closed: list[dict[str, Any]] = []
    for w in windows or []:
        if w.get("class") != DIALOG_CLASS:
            continue
        hwnd = int(w.get("hwnd") or 0)
        if not hwnd or _find_login_button(hwnd) is not None:
            continue  # 真登录框：绝不关
        try:
            l, t, r, b = _rect(hwnd)
        except ThsXiadanError:
            continue
        if (r - l) * (b - t) < _TOAST_MAX_AREA:
            USER32.SendMessageW(hwnd, _WM_CLOSE, 0, 0)
            closed.append({"hwnd": hwnd, "rect": [l, t, r, b]})
    return closed


def _reach_login_state(*, window_wait: float, login_timeout: float,
                       dry_run: bool) -> dict[str, Any]:
    state, windows, main = _wait_any_window(window_wait)
    toasts_closed: list[dict[str, Any]] = []
    restored_hidden_panel = False
    # 判定前清理循环（2026-11-14 冷启动二轮实盘）：证书自动登录后 xiadan 处于
    # 隐藏主窗+通知条状态——通知条在轮询后才弹出，一次性清理覆盖不到。
    # 循环：关微小通知条 → 隐藏主窗先还原 → 重轮询，直到状态稳定（≤4 轮）。
    for _round in range(4):
        closed_now = _dismiss_tiny_toasts(windows)
        if closed_now:
            toasts_closed.extend(closed_now)
            time.sleep(1.0)
            state, windows, main = _poll_state()
        if (state == "main" and main is not None and not main.get("visible")
                and not windows):
            USER32.ShowWindow(int(main["hwnd"]), 9)  # SW_RESTORE
            time.sleep(1.5)
            state, windows, main = _poll_state()
            restored_hidden_panel = True
        # 稳定判定：无可见杂窗，或剩的就是登录框形状
        if not windows or _find_visible_login_dialog(windows) is not None:
            break
        if state == "main" and not windows:
            break
        time.sleep(1.0)
        state, windows, main = _poll_state()
    toast_note: dict[str, Any] = {}
    if toasts_closed:
        toast_note["toasts_closed"] = toasts_closed
    if restored_hidden_panel:
        toast_note["restored_hidden_panel"] = True
    if state == "none":
        raise ThsXiadanError(
            "no visible xiadan top-level window after launch (NO_XIADAN_WINDOW)"
        )
    # R51：登录框可见 = 未登录（标题壳窗不算数，2026-11-13 19:5x 实盘根因）
    visible_login = _find_visible_login_dialog(windows)
    if state == "main" and not windows:
        return {"login_state": "logged_in", "windows": windows,
                "main_window": main, "clicks": [], "password_remembered": None,
                **toast_note}
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
        **toast_note,
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
    text = re.sub(r"[-−﹣－‒–—]\s*(?=\d)", "-", text)
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
            # R52 ①：热切换先于身份门禁——面板停在另一账号时，门禁先行
            # mismatch 会让切换代码永不执行（2026-11-14 实证根因，当晚两次
            # unavailable 皆是此因）。切换自带 OCR 身份自证 + 未知身份
            # identity_mismatch fail-closed；门禁随后复核切换结果（双保险）。
            if hotswitch:
                from services import ths_hotswitch as hs
                pre = hs.ensure_target_account(window, hotswitch)
                if pre is not None:
                    raise RuntimeError(
                        f"PANEL_{str(pre.get('status', 'switch_failed')).upper()}: "
                        f"label={pre.get('identity_label')!r} "
                        f"expect~{hotswitch.get('target_hint')!r} "
                        f"account={account}")
            # R51 ②③：身份门禁（多锚读取 + 纯函数决策），点菜单之前零交互
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
        broker_file = Path(r"F:\Aistock\runtime\gen3_state_alpha\broker_state.json")
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


# ══════════════════════════════════════════════════════════════════════════════
# 远航版段（2026-11-14 Owner 裁决新增：read-account / switch-account 子命令）。
#
# 目标客户端：D:\同花顺远航\bin\happ.exe（主程序，CEF 页面）+ F12 拉起的
# xiadan.exe 交易面板。与经典版（D:\同花顺）并存、链路独立：本段自包含（不经
# services 经典面板链）。开源可用逻辑以 vendored 方式并入并标注来源与 MIT
# 许可，运行时零外部包依赖（EasyTHS 只读源码改写技法，绝不 import）。
#
# 已实机验证事实（2026-11-14）：
#   * 登录页 UIA 37 控件（资金帐户 Edit aid=1001 / 交易密码 Edit aid=1012 /
#     登录 Button aid=1006）；已登录面板 134 控件；F12=交易面板热键；
#   * 资金汇总区 UIA Text 直读可用（面板相对区域 y150-320 x150-950）；
#   * 持仓明细自绘/虚拟化：UIA 无行数据 → PrintWindow 离屏截图
#     （PW_RENDERFULLCONTENT=2，RDP 最小化可用）+ 视觉模型读行；
#   * 面板顶栏脱敏账号自绘（A5****1851=曾 / A6****5646=王），UIA 读不到文本；
#   * 账号切换走主程序「交易」菜单（CEF，UIA 不可点）→ 真实鼠标坐标点击；
#   * 面板句柄随重渲染变化 → 每次重新枚举（xiadan PID+可见+宽>300），
#     绝不缓存句柄。
#
# 安全铁律（不放松）：凭证零接触（本段不触任何密码控件，登录页探测只读
# ControlType/AutomationId）；绝不触碰委托下单界面（只读资金/持仓查询页 +
# 交易菜单账号项）；身份门禁 fail-closed（脱敏栏读不到不归属任何数据）；
# Owner 活跃守卫（THS 窗前台+输入空闲<90s → 绝不键击/点击）。
# ══════════════════════════════════════════════════════════════════════════════

YUANHANG_APP_DIR_DEFAULT = Path(r"D:\同花顺远航")
YUANHANG_EXE_DEFAULT = YUANHANG_APP_DIR_DEFAULT / "bin" / "happ.exe"
YH_MAIN_PROCESS = "happ.exe"          # 远航版主程序（CEF 壳 + 交易菜单）
YH_PANEL_PROCESS = "xiadan.exe"       # F12 交易面板宿主进程

YH_ACCOUNTS = {                        # 账号注册表（顶栏脱敏 → 账号键）
    "acct1": {"masked_id": "A5****1851", "owner": "曾", "label": "广发证券-曾*"},
    "acct2": {"masked_id": "A6****5646", "owner": "王", "label": "广发证券-王*美"},
}
_YH_MASKED_RE = re.compile(r"([A-Z])\s*(\d{1,2})\s*(\*{2,10})\s*(\d{3,6})")

YH_FUNDS_REGION = (150.0, 110.0, 980.0, 330.0)  # 资金汇总区（面板相对；实测
# 2026-11-14 实拍：资金行 y125-176，UIA 六项命中于 y150-320；下限放宽到
# y110 兼容 OCR 行高差，x≥150 排除左侧买卖/撤单按钮列）
YH_FUNDS_ROW_TOLERANCE = 16.0          # 标签↔数值同行判定（px）
YH_HOLDINGS_MIN_CODE_X = 200.0         # 持仓代码列最小 x（排除左侧导航树列）
# 资金标签 → 规范键（实测文案 + 常见变体；长词优先匹配）
YH_FUNDS_LABEL_ALIASES = {
    "资金余额": "balance", "余额": "balance",
    "可用金额": "available", "可用": "available",
    "可取金额": "withdrawable", "可取": "withdrawable",
    "冻结金额": "frozen", "冻结": "frozen",
    "股票市值": "market_value", "证券市值": "market_value",
    "持仓盈亏": "holding_profit", "浮动盈亏": "holding_profit",
    "总资产": "total_assets", "总资金": "total_assets",
    "当日盈亏": "daily_profit", "今日盈亏": "daily_profit",
    "当日盈亏比": "daily_profit_ratio", "持仓盈亏比": "holding_profit_ratio",
}
# [vendored] EasyTHS operations/funds_query.py（MIT License, (c) noimank，
# github noimank/easyths 2.0.8）：资金字段控件 automation_id → 字段名映射，
# 改写并入（远航版面板若复用同 auto_id 直接命中；否则走标签配对路径）。
YH_EASYTHS_FUNDS_FIELD_MAP = {
    "1012": "balance", "1013": "frozen_amount", "1014": "market_value",
    "1015": "total_assets", "1016": "available_amount",
    "1017": "withdrawable_amount", "1027": "holding_profit",
    "1026": "daily_profit", "1029": "daily_profit_ratio",
}
_YH_EASYTHS_FUNDS_TO_CANON = {
    "balance": "balance", "frozen_amount": "frozen",
    "market_value": "market_value", "total_assets": "total_assets",
    "available_amount": "available", "withdrawable_amount": "withdrawable",
    "holding_profit": "holding_profit", "daily_profit": "daily_profit",
    "daily_profit_ratio": "daily_profit_ratio",
}

# [vendored] EasyTHS operations/holding_query.py parse_holding_row（MIT
# License, (c) noimank）：实盘/模拟两套列名 → 规范字段适配表改写并入
# （「持仓数量/股票余额」「参考成本价/成本价」「当前价/市价」「浮动盈亏/
# 盈亏」等差异，键经归一化后精确匹配优先、包含匹配兜底）。
YH_HOLDING_COLUMN_ALIASES = {
    "证券代码": "code", "股票代码": "code", "code": "code", "stock_code": "code",
    "证券名称": "name", "股票名称": "name", "name": "name", "stock_name": "name",
    "持仓数量": "quantity", "股票余额": "quantity", "数量": "quantity",
    "quantity": "quantity",
    "可用数量": "available_quantity", "可用余额": "available_quantity",
    "可用": "available_quantity", "available_quantity": "available_quantity",
    "冻结数量": "frozen_quantity", "frozen_quantity": "frozen_quantity",
    "参考成本价": "cost_price", "成本价": "cost_price", "成本": "cost_price",
    "cost_price": "cost_price",
    "当前价": "current_price", "市价": "current_price", "最新价": "current_price",
    "现价": "current_price", "current_price": "current_price",
    "浮动盈亏": "floating_profit", "盈亏": "floating_profit",
    "floating_profit": "floating_profit",
    "盈亏比例": "profit_ratio", "盈亏比": "profit_ratio",
    "盈利比例": "profit_ratio", "profit_ratio": "profit_ratio",
    "最新市值": "market_value", "市值": "market_value",
    "market_value": "market_value",
    "当日盈亏": "daily_profit", "daily_profit": "daily_profit",
    "当日盈亏比": "daily_profit_ratio", "daily_profit_ratio": "daily_profit_ratio",
    "仓位占比": "position_ratio", "position_ratio": "position_ratio",
    "当日买入": "daily_bought", "daily_bought": "daily_bought",
    "当日卖出": "daily_sold", "daily_sold": "daily_sold",
    "交易市场": "market", "市场": "market", "market": "market",
}
_YH_HOLDING_CANON_FIELDS = (
    "code", "name", "quantity", "available_quantity", "frozen_quantity",
    "cost_price", "current_price", "floating_profit", "profit_ratio",
    "market_value", "daily_profit", "daily_profit_ratio", "position_ratio",
    "daily_bought", "daily_sold", "market",
)
_YH_HOLDING_NUMERIC_FIELDS = (
    "quantity", "available_quantity", "frozen_quantity", "cost_price",
    "current_price", "floating_profit", "profit_ratio", "market_value",
    "daily_profit", "daily_profit_ratio", "position_ratio",
)
# 无表头时的兜底列序（任务口径：代码/名称/数量/成本/市价/盈亏）
YH_HOLDING_FIELD_ORDER = ("quantity", "cost_price", "current_price",
                          "floating_profit")

YH_VISION_ENV = "AISTOCK_THS_VISION_CMD"    # 视觉模型钩子（<cmd> <png>→JSON）
YH_MENU_CONFIG_ENV = "AISTOCK_THS_YUANHANG_MENU_JSON"  # 菜单坐标配置 JSON
# 主程序顶栏搜索区（实拍 2026-11-14：主窗 1853px 宽，账号下拉
# 「广发证券－王*美▼」在 (790,26)；无「交易」独立菜单，点当前账号下拉展开
# 账号列表——候选优先级：交易 > 广发证券 > 脱敏账号 > Owner 字）
YH_MENU_REGION_DEFAULT = (0.0, 0.0, 1280.0, 140.0)
YH_OWNER_ACTIVE_IDLE_S = 90.0   # [同源] services/ths_hotswitch OWNER_ACTIVE_IDLE_S
YH_FUNDS_JUMP_EPSILON = 0.005   # 总资产跳变判定阈值（元）

VK_F12 = 0x7B
VK_ESCAPE = 0x1B
KEYEVENTF_KEYUP = 0x0002

# [vendored] EasyTHS core/account_state.py AccountState（MIT License,
# (c) noimank）：进程内可用账户/当前使用账户可信副本；切换/身份读取经验证
# 后才刷新，绝不提前写目标账号。
_YH_ACCOUNT_STATE: dict[str, Any] = {
    "available_accounts": [
        {"account": k, "masked_id": v["masked_id"], "owner": v["owner"]}
        for k, v in YH_ACCOUNTS.items()
    ],
    "current_used_account": None,
}


# ── 远航版窗口原语 ─────────────────────────────────────────────────

def _yh_runtime_dir() -> Path:
    """远航版产物目录（runtime/ths_client/：截图/菜单坐标配置/报告）。"""
    return _runtime_dir("ths_client")


def _yh_stamped(prefix: str, suffix: str = ".png") -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    return f"{prefix}_{stamp}{suffix}"


def _yh_ensure_main(happ_exe: Path = YUANHANG_EXE_DEFAULT,
                    main_wait: float = 20.0) -> dict[str, Any]:
    """确保 happ 主程序运行：未运行 Popen 带 cwd=安装目录（远航版契约，
    与经典版 _launch 同源）。"""
    _require_windows()
    pids = _pids_of(YH_MAIN_PROCESS)
    launched = False
    if not pids:
        exe = Path(happ_exe)
        if not exe.is_file():
            raise ThsXiadanError(f"happ.exe not found: {exe}")
        _launch(exe)
        launched = True
    pids = _wait_for(lambda: _pids_of(YH_MAIN_PROCESS),
                     main_wait if launched else 3.0, 0.5)
    if not pids:
        raise ThsXiadanError("happ.exe not running after launch wait")
    return {"pids": list(pids), "launched": launched}


def _yh_main_window() -> Optional[dict[str, Any]]:
    """happ 主程序可见顶层窗（最大面积；无则 None）。"""
    _require_windows()
    pids = set(_pids_of(YH_MAIN_PROCESS))
    best: Optional[dict[str, Any]] = None
    for hwnd in _top_level_windows():
        if _process_id(hwnd) not in pids or not _is_visible(hwnd):
            continue
        left, top, right, bottom = _rect(hwnd)
        area = max(0, right - left) * max(0, bottom - top)
        if area <= 0:
            continue
        cand = {"hwnd": int(hwnd), "pid": int(_process_id(hwnd)),
                "rect": [left, top, right, bottom], "area": area,
                "title": _window_text(hwnd)}
        if best is None or area > best["area"]:
            best = cand
    return best


def _yh_enum_panels() -> list[dict[str, Any]]:
    """F12 交易面板枚举（xiadan PID 过滤 + IsWindowVisible + 宽>300）。
    面板句柄随重渲染变化 → 每次调用重新枚举，绝不缓存。"""
    _require_windows()
    pids = set(_pids_of(YH_PANEL_PROCESS))
    out: list[dict[str, Any]] = []
    for hwnd in _top_level_windows():
        if _process_id(hwnd) not in pids or not _is_visible(hwnd):
            continue
        left, top, right, bottom = _rect(hwnd)
        width, height = right - left, bottom - top
        if width <= 300 or height <= 0:
            continue
        out.append({"hwnd": int(hwnd), "pid": int(_process_id(hwnd)),
                    "rect": [left, top, right, bottom], "width": width,
                    "height": height, "title": _window_text(hwnd)})
    out.sort(key=lambda w: w["width"] * w["height"], reverse=True)
    return out


def _yh_set_foreground(hwnd: int) -> dict[str, Any]:
    """前台化（AttachThreadInput 跨线程授权；同源 services/ths_hotswitch）。
    SetForegroundWindow 被前台锁挡（RDP 常见）→ SwitchToThisWindow 兜底
    （2026-11-14 实机：前者 ok=false 后者成功）。"""
    _require_windows()
    fg = int(USER32.GetForegroundWindow() or 0)
    if fg == int(hwnd):
        return {"ok": True, "attached": False, "via": "already"}
    fg_thread = int(USER32.GetWindowThreadProcessId(fg, None)) if fg else 0
    target_thread = int(USER32.GetWindowThreadProcessId(int(hwnd), None))
    attached = bool(fg_thread and target_thread and fg_thread != target_thread)
    if attached:
        USER32.AttachThreadInput(fg_thread, target_thread, True)
    USER32.ShowWindow(int(hwnd), SW_RESTORE)
    USER32.SetWindowPos(int(hwnd), HWND_TOP, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    USER32.BringWindowToTop(int(hwnd))
    USER32.SetForegroundWindow(int(hwnd))
    USER32.SetFocus(int(hwnd))
    if attached:
        USER32.AttachThreadInput(fg_thread, target_thread, False)
    time.sleep(0.15)
    ok = int(USER32.GetForegroundWindow() or 0) == int(hwnd)
    via = "set_foreground_window"
    if not ok:  # 前台锁兜底：SwitchToThisWindow（实机验证有效）
        try:
            USER32.SwitchToThisWindow(int(hwnd), True)
            time.sleep(0.2)
            ok = int(USER32.GetForegroundWindow() or 0) == int(hwnd)
            via = "switch_to_this_window"
        except Exception:  # noqa: BLE001
            pass
    return {"ok": ok, "attached": attached, "via": via}


def _yh_send_f12(main_hwnd: int) -> dict[str, Any]:
    """前台化主程序 + keybd_event F12（远航版交易面板热键，人类同源路径）。
    不用 PostMessage：CEF 页可能把 F12 当 DevTools 开关（误开调试器）。"""
    _require_windows()
    fg = _yh_set_foreground(int(main_hwnd))
    time.sleep(0.2)
    USER32.keybd_event(VK_F12, 0, 0, 0)
    time.sleep(0.06)
    USER32.keybd_event(VK_F12, 0, KEYEVENTF_KEYUP, 0)
    _yh_note_self_injection()
    return {"foreground_ok": bool(fg.get("ok")), "via": "keybd_event"}


def _yh_ensure_panel(main_hwnd: Optional[int], f12_wait: float = 8.0,
                     allow_f12: bool = True,
                    ) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    """枚举已开面板；没有则 F12 拉起（allow_f12=False 时绝不键击）。"""
    steps: list[dict[str, Any]] = []
    panels = _yh_enum_panels()
    if panels:
        steps.append({"step": "panel_enumerated", "hwnd": panels[0]["hwnd"]})
        return panels[0], steps
    if main_hwnd and allow_f12:
        steps.append({"step": "f12_send", "main_hwnd": int(main_hwnd),
                      **_yh_send_f12(int(main_hwnd))})
        panels = _wait_for(lambda: _yh_enum_panels(), f12_wait, 0.5) or []
        steps.append({"step": "panel_after_f12", "found": bool(panels)})
        if panels:
            return panels[0], steps
    steps.append({"step": "panel_absent"})
    return None, steps


def _yh_click_screen(sx: int, sy: int) -> dict[str, Any]:
    """屏幕绝对坐标真实鼠标左键单击（SetCursorPos + mouse_event）。"""
    _require_windows()
    if not USER32.SetCursorPos(int(sx), int(sy)):
        raise ThsXiadanError(f"SetCursorPos failed: {sx},{sy}")
    time.sleep(0.06)
    USER32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    USER32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    _yh_note_self_injection()
    return {"clicked_screen": [int(sx), int(sy)]}


def _yh_click_window(hwnd: int, xy: Iterable[float]) -> dict[str, Any]:
    """窗口坐标（=PrintWindow PNG 像素空间，含非客户区）→ 屏幕 → 真实点击。
    点击前校验光标点下的顶层窗确属目标；被遮挡 → 主窗口临时 TOPMOST 抬升
    后复验再点（点毕还原 NOTOPMOST）；仍遮挡 → fail-closed 拒点（绝不把
    点击打进别的窗口）。"""
    coords = list(xy)[:2]
    if len(coords) != 2:
        raise ThsXiadanError(f"click xy must be [x, y], got {xy!r}")
    left, top, _right, _bottom = _rect(int(hwnd))
    sx, sy = int(left + float(coords[0])), int(top + float(coords[1]))
    def _same_app_window(h_hit: int, h_target: int) -> bool:
        if not h_hit:
            return True
        if _root(h_hit) == _root(h_target):
            return True
        # 远航版 CEF 顶栏菜单/下拉层可能为同进程独立顶层子窗；同 PID 不视为外部遮挡
        try:
            p_hit = int(_process_id(h_hit) or 0)
            p_tgt = int(_process_id(h_target) or 0)
            return p_hit > 0 and p_hit == p_tgt
        except Exception:  # noqa: BLE001
            return False

    hit = _window_from_point(sx, sy)
    topmost_used = False
    if hit and not _same_app_window(hit, int(hwnd)):
        # 遮挡兜底：临时 TOPMOST 抬升目标窗（点毕还原），复验通过才点
        USER32.SetWindowPos(int(hwnd), 0xFFFFFFFF, 0, 0, 0, 0,  # HWND_TOPMOST
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        time.sleep(0.15)
        topmost_used = True
        hit = _window_from_point(sx, sy)
        if hit and not _same_app_window(hit, int(hwnd)):
            USER32.SetWindowPos(int(hwnd), 0xFFFFFFFE, 0, 0, 0, 0,  # NOTOPMOST
                                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            hit_pid = 0
            try:
                hit_pid = int(_process_id(hit) or 0)
            except Exception:  # noqa: BLE001
                pass
            raise ThsXiadanError(
                f"click_target_covered: target={int(hwnd)} hit={hit} hit_pid={hit_pid} "
                f"at screen=({sx},{sy})（TOPMOST 抬升后仍被外部窗口遮挡，"
                "fail-closed 拒点）")
    try:
        result = {"window_xy": [float(coords[0]), float(coords[1])],
                  "hwnd": int(hwnd),
                  "topmost_used": topmost_used,
                  **_yh_click_screen(sx, sy)}
    finally:
        if topmost_used:
            USER32.SetWindowPos(int(hwnd), 0xFFFFFFFE, 0, 0, 0, 0,  # NOTOPMOST
                                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    return result


# ── Owner 活跃守卫（同源 services/ths_hotswitch 改写） ────────────

def _yh_input_idle_seconds() -> float:
    """GetLastInputInfo → 距上次输入秒数（失败 → -1：宁可不切）。"""
    try:
        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(lii)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return -1.0
        now = ctypes.windll.kernel32.GetTickCount()
        last = lii.dwTime
        if (now.__class__ is not int or last.__class__ is not int
                or not 0 < last <= now <= 0xFFFFFFFF):
            return -1.0
        self_tick = _YH_LAST_SELF_INJECT_TICK
        if (self_tick.__class__ not in (int, float)
                or not 0 <= self_tick <= now or self_tick != int(self_tick)):
            return -1.0
        if self_tick:
            # 第二次采样只校验记账和时钟，不能据此推断输入来源。
            later = ctypes.windll.kernel32.GetTickCount()
            if (later.__class__ is not int
                    or not now <= later <= 0xFFFFFFFF):
                return -1.0
        return (now - last) / 1000.0
    except Exception:  # noqa: BLE001 - 守卫失效方向 = fail-safe 让位 Owner
        return -1.0


_YH_LAST_SELF_INJECT_TICK = 0.0   # GetTickCount ms；0 = 本进程尚无自注入


def _yh_note_self_injection() -> None:
    """标记最近一次输入是本自动化注入的（点击/键击后调用）——Owner 活跃
    判定剔除自家输入，不把自家点击误判成 Owner 手动操作。"""
    global _YH_LAST_SELF_INJECT_TICK
    try:
        _YH_LAST_SELF_INJECT_TICK = float(ctypes.windll.kernel32.GetTickCount())
    except Exception:  # noqa: BLE001
        pass


def yh_owner_active_decision(idle_s: float, since_self_inject_s: float,
                             threshold: float = YH_OWNER_ACTIVE_IDLE_S) -> bool:
    """纯函数（可测）：本次输入是否 Owner 真实操作。idle≥阈值 → False；
    本进程从未注入 → 空闲即 Owner；最后输入≈自家注入 → 不算 Owner。"""
    if idle_s >= threshold:
        return False
    if since_self_inject_s <= 0 or since_self_inject_s == float("inf"):
        return True
    return idle_s < since_self_inject_s - 0.3


def _yh_owner_idle_seconds() -> float:
    """所有输入均计为活跃；不能以时间接近度证明输入来自自动化。"""
    return _yh_input_idle_seconds()


def _yh_owner_using(hwnd: Optional[int]) -> bool:
    """Owner 活跃守卫：目标窗在前台 且 Owner 输入空闲 <90s → True（绝不
    键击/点击）。判不了 → True（fail-safe 让位）。"""
    try:
        if hwnd is None or hwnd.__class__ is not int or hwnd <= 0:
            return True
        foreground = USER32.GetForegroundWindow()
        if foreground.__class__ is not int or foreground <= 0:
            return True
        if foreground != hwnd:
            return False
        idle = _yh_owner_idle_seconds()
        if not 0 <= idle < float("inf"):
            return True
        return idle < YH_OWNER_ACTIVE_IDLE_S
    except Exception:  # noqa: BLE001
        return True


# ── 截图与视觉/OCR 读取层 ──────────────────────────────────────────

def _yh_capture_window(hwnd: int, prefix: str) -> dict[str, Any]:
    """PrintWindow 离屏截图存 runtime/ths_client/<prefix>_时间戳.png
    （PW_RENDERFULLCONTENT=2，不吃焦点，RDP 最小化可用；实现复用
    services.ths_holdings_poll.print_window_image 同源，不复制）。"""
    img = poll.print_window_image(int(hwnd))
    root = _yh_runtime_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / _yh_stamped(prefix)
    img.save(path)
    return {"path": str(path), "width": int(img.width), "height": int(img.height)}


def _yh_read_image(png_path: str) -> dict[str, Any]:
    """截图 → 文本项/持仓行读取。优先视觉模型钩子（env
    AISTOCK_THS_VISION_CMD：以 <png> 为唯一参数调起，stdout 取 JSON），
    回退本地 OCR（rapidocr_onnxruntime，与 services 桥接层同源；env
    AISTOCK_THS_DISABLE_OCR 关闭本地 OCR）。两者皆不可用 → reader=None
    （调用方如实标 holdings_source=ocr_unavailable，绝不造数）。
    钩子 JSON 契约：{"items":[{text,x,y,score}...]} 或 {"texts":[...]}
    或 {"rows":[{证券代码.../code,name,quantity...}]}（rows 优先采用）。
    """
    out: dict[str, Any] = {"items": [], "rows": [], "reader": None, "errors": []}

    def _norm_items(raw: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for it in raw or []:
            if not isinstance(it, dict):
                continue
            try:
                items.append({"text": str(it.get("text") or ""),
                              "score": float(it.get("score") or 1.0),
                              "x": float(it.get("x") or 0.0),
                              "y": float(it.get("y") or 0.0),
                              "aid": str(it.get("aid") or "")})
            except Exception:  # noqa: BLE001 - 单项坏数据不阻断
                continue
        return items

    cmd = str(os.environ.get(YH_VISION_ENV) or "").strip()
    if cmd:
        try:
            proc = subprocess.run([cmd, str(png_path)], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=180)
            if proc.returncode != 0:
                out["errors"].append(f"vision_cmd_rc_{proc.returncode}")
            elif str(proc.stdout or "").strip():
                data = json.loads(proc.stdout.strip())
                if isinstance(data, list):
                    data = {"items": data}
                if not isinstance(data, dict):
                    raise ValueError("vision hook JSON must be object/list")
                items = data.get("items")
                if items is None and data.get("texts") is not None:
                    items = [{"text": t, "x": 0.0, "y": float(i) * 18.0,
                              "score": 1.0}
                             for i, t in enumerate(data.get("texts") or [])]
                out["items"] = _norm_items(items)
                out["rows"] = [r for r in (data.get("rows") or [])
                               if isinstance(r, dict)]
                out["reader"] = "vision_model"
                return out
        except Exception as exc:  # noqa: BLE001 - 钩子失败回退本地 OCR
            out["errors"].append(f"{type(exc).__name__}: {exc}")
    if str(os.environ.get("AISTOCK_THS_DISABLE_OCR") or "").strip().lower() \
            not in {"1", "true", "yes", "on"}:
        try:
            from rapidocr_onnxruntime import RapidOCR  # 桥接层同源本地 OCR

            result, _ = RapidOCR()(str(png_path))
            items = []
            for row in result or []:
                if len(row) < 3:
                    continue
                box, text, score = row[0], str(row[1] or "").strip(), float(row[2] or 0.0)
                if not text:
                    continue
                items.append({"text": text, "score": score,
                              "x": sum(float(p[0]) for p in box) / len(box),
                              "y": sum(float(p[1]) for p in box) / len(box)})
            out["items"] = items
            out["reader"] = "ocr_local" if items else None
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"{type(exc).__name__}: {exc}")
    return out


# ── 纯函数解析层（身份/资金/持仓，可单测） ─────────────────────────

def yh_normalize_masked(text: str) -> Optional[str]:
    """文本中提取脱敏账号（如 A5****1851；OCR 空隙容忍：原文不中再试去空格）
    → 规范连写形。"""
    raw = str(text or "")
    match = _YH_MASKED_RE.search(raw)
    if match is None:
        squeezed = re.sub(r"\s+", "", raw)
        if squeezed != raw:
            match = _YH_MASKED_RE.search(squeezed)
    if match is None:
        return None
    return "".join(match.groups())


def yh_masked_signature(masked: str) -> Optional[tuple[str, str, str]]:
    """脱敏账号签名 (字母, 前数字, 尾数字)；星数漂移不影响比较。"""
    match = _YH_MASKED_RE.fullmatch(re.sub(r"\s+", "", str(masked or "")))
    if match is None:
        return None
    return match.group(1), match.group(2), match.group(4)


def yh_account_from_masked(masked: Optional[str]) -> Optional[str]:
    """脱敏账号 → 注册表账号键（acct1/acct2）；未知/读不到 → None。"""
    sig = yh_masked_signature(masked or "")
    if sig is None:
        return None
    for key, meta in YH_ACCOUNTS.items():
        if yh_masked_signature(meta["masked_id"]) == sig:
            return key
    return None


def _yh_match_funds_label(text: str) -> tuple[Optional[str], Optional[str]]:
    """资金标签匹配（长词优先，包含匹配——OCR「值+标签」合并文本如实机
    「4076.82可取金额」也能命中）：(canon, alias)，不中 (None, None)。"""
    norm = re.sub(r"[\s:：]+", "", str(text or ""))
    if not norm:
        return None, None
    for alias in sorted(YH_FUNDS_LABEL_ALIASES, key=len, reverse=True):
        if alias in norm:
            return YH_FUNDS_LABEL_ALIASES[alias], alias
    return None, None


def yh_parse_funds_texts(items: list[dict[str, Any]],
                         region: Optional[tuple[float, float, float, float]] = YH_FUNDS_REGION,
                         strict: bool = False) -> dict[str, Any]:
    """资金汇总解析（纯函数）。items = 面板相对坐标文本项 [{text,x,y,aid}]
    （UIA Text 与 OCR item 同形同坐标系）。
      ① [vendored EasyTHS funds_query] automation_id 命中字段表直映；
      ② 标签 ↔ 同行数值配对（|Δy|≤16 且 x 在标签右侧最近）；
      ③ 「标签+数值」合并文本余量抽数。
    region 限定筛选区（默认实测区 y150-320 x150-950）；strict=True 仅整
    文本等值命中（全面板兜底防误配）。返回 {"funds","labels","matched"}。"""
    def in_region(it: dict[str, Any]) -> bool:
        if region is None:
            return True
        x1, y1, x2, y2 = region
        x, y = float(it.get("x") or 0.0), float(it.get("y") or 0.0)
        return x1 <= x <= x2 and y1 <= y <= y2

    scoped = [it for it in (items or []) if in_region(it)]
    funds: dict[str, float] = {}
    labels: dict[str, str] = {}
    # ① auto_id 直映（EasyTHS vendored）
    for it in scoped:
        canon = _YH_EASYTHS_FUNDS_TO_CANON.get(
            YH_EASYTHS_FUNDS_FIELD_MAP.get(str(it.get("aid") or ""), ""))
        if canon:
            value = _parse_number(it.get("text"))
            if value is not None and canon not in funds:
                funds[canon] = value
                labels[canon] = str(it.get("text") or "")
    numerics: list[tuple[dict[str, Any], float]] = []
    for it in scoped:
        value = _parse_number(it.get("text"))
        if value is not None:
            numerics.append((it, value))
    for it in scoped:
        text = str(it.get("text") or "").strip()
        if not text:
            continue
        canon, alias = _yh_match_funds_label(text)
        if canon is None or canon in funds:
            continue
        if strict and re.sub(r"[\s:：]+", "", text) != alias:
            continue
        x0, y0 = float(it.get("x") or 0.0), float(it.get("y") or 0.0)
        spaced = re.sub(r"[\s:：]+", " ", text)
        # 合并文本两种形态：「值+标签」（OCR 常见，值属左邻标签，本标签的值
        # 取同行右侧数值）与「标签+值」（值属本标签，取余量抽数）。
        alias_at = spaced.find(alias) if alias else -1
        head, tail = spaced[:alias_at], spaced[alias_at + len(alias or ""):]
        prefix_number = _parse_number(head)
        value = _parse_number(tail)
        if value is None and prefix_number is not None:
            value = None                      # 前缀数不归属本标签（防误配）
        source_label = text
        if value is None:
            # ② 同行最近右侧数值
            best: Optional[tuple[dict[str, Any], float]] = None
            for nit, nval in numerics:
                nx, ny = float(nit.get("x") or 0.0), float(nit.get("y") or 0.0)
                if abs(ny - y0) > YH_FUNDS_ROW_TOLERANCE or nx <= x0:
                    continue
                if best is None or nx < float(best[0].get("x") or 0.0):
                    best = (nit, nval)
            if best is not None:
                value = best[1]
                source_label = str(best[0].get("text") or "")
        if value is not None:
            funds[canon] = value
            labels[canon] = source_label
    return {"funds": funds, "labels": labels, "matched": len(funds)}


def yh_uia_texts_to_items(texts: list[dict[str, Any]],
                          panel_rect: Optional[list[int]]) -> list[dict[str, Any]]:
    """UIA Text（屏幕矩形）→ 面板相对坐标文本项（与截图/资金区同坐标系）。"""
    out: list[dict[str, Any]] = []
    if not panel_rect or len(panel_rect) != 4:
        return out
    pl, pt = float(panel_rect[0]), float(panel_rect[1])
    for it in texts or []:
        rect = it.get("rect") or [0, 0, 0, 0]
        try:
            left, top = float(rect[0]), float(rect[1])
            right, bottom = float(rect[2]), float(rect[3])
        except Exception:  # noqa: BLE001
            continue
        if right <= left or bottom <= top:
            continue
        out.append({"text": str(it.get("text") or ""),
                    "aid": str(it.get("aid") or ""),
                    "x": (left + right) / 2.0 - pl,
                    "y": (top + bottom) / 2.0 - pt, "score": 1.0})
    return out


def _yh_norm_colkey(key: str) -> str:
    return re.sub(r"[\s()%（）:：*／/]+", "", str(key or "")).lower()


def yh_normalize_holding_record(record: dict[str, Any]) -> dict[str, Any]:
    """[vendored+改写] EasyTHS holding_query.parse_holding_row：列名差异适配
    （实盘「持仓数量/参考成本价/当前价/浮动盈亏」≡模拟「股票余额/成本价/
    市价/盈亏」→ 规范字段；数值统一 _parse_number；缺列 → None）。"""
    row: dict[str, Any] = {}
    aliases_exact = {_yh_norm_colkey(a): c
                     for a, c in YH_HOLDING_COLUMN_ALIASES.items()}
    aliases_sub = sorted(aliases_exact.items(), key=lambda p: len(p[0]),
                         reverse=True)
    for key, value in (record or {}).items():
        nk = _yh_norm_colkey(key)
        if not nk:
            continue
        canon = aliases_exact.get(nk)
        if canon is None:  # 包含匹配兜底（「股票市值」→市值 等长尾差异）
            for an, c in aliases_sub:
                if len(an) >= 2 and (an in nk or nk in an):
                    canon = c
                    break
        if canon and canon not in row:
            row[canon] = value
    for field in _YH_HOLDING_NUMERIC_FIELDS:
        if field in row:
            text = str(row[field])
            # OCR 数字内空格修复：「33. 993」→「33.993」（仅动 数字.空格.数字）
            text = re.sub(r"(\d)\.\s+(\d)", r"\1.\2", text)
            row[field] = _parse_number(text)
    for field in ("code", "name", "market"):
        if field in row:
            row[field] = str(row[field] or "").strip() or None
    for field in _YH_HOLDING_CANON_FIELDS:
        row.setdefault(field, None)
    return row


def yh_rows_from_vision(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """视觉模型直接返回的行记录 → 规范持仓行（列名适配后取有代码的行）。"""
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        norm = yh_normalize_holding_record(row)
        if norm.get("code"):
            out.append(norm)
    return out


def yh_group_rows(items: list[dict[str, Any]],
                  row_delta: float = 14.0) -> list[list[dict[str, Any]]]:
    """y 质心行分组（技法同源 services.ths_account_bridge._ocr_group_rows）。"""
    rows: list[list[dict[str, Any]]] = []
    for it in sorted(items or [], key=lambda o: (float(o.get("y") or 0.0),
                                                 float(o.get("x") or 0.0))):
        y = float(it.get("y") or 0.0)
        if rows and abs(y - sum(float(o.get("y") or 0.0) for o in rows[-1])
                        / len(rows[-1])) <= row_delta:
            rows[-1].append(it)
        else:
            rows.append([it])
    return rows


def yh_holdings_from_items(items: list[dict[str, Any]],
                           row_delta: float = 14.0,
                           min_code_x: float = YH_HOLDINGS_MIN_CODE_X,
                           ) -> list[dict[str, Any]]:
    """截图文本项 → 持仓行。表头列名映射优先（EasyTHS 列名适配技法），
    无表头按 代码/名称/数量/成本/市价/盈亏 列序兜底；x<min_code_x 的项
    （远航版面板左侧导航树列）不参与解析，无代码/名称<2 字的行一律跳过。"""
    rows_out: list[dict[str, Any]] = []
    header_map: dict[str, float] = {}
    aliases = {_yh_norm_colkey(a): c
               for a, c in YH_HOLDING_COLUMN_ALIASES.items()}
    for row in yh_group_rows(items, row_delta=row_delta):
        cells = sorted((o for o in row
                        if float(o.get("x") or 0.0) >= min_code_x),
                       key=lambda o: float(o.get("x") or 0.0))
        texts = [str(o.get("text") or "").strip() for o in cells]
        if not any(texts):
            continue
        code = next((tx for tx, o in zip(texts, cells)
                     if re.fullmatch(r"\d{6}", tx)), None)
        if code is None:
            if not header_map:  # 表头行候选：≥2 列名命中且尚无表头
                hits: list[tuple[str, float]] = []
                for tx, obj in zip(texts, cells):
                    canon = YH_HOLDING_COLUMN_ALIASES.get(tx) \
                        or aliases.get(_yh_norm_colkey(tx))
                    if canon:
                        hits.append((canon, float(obj.get("x") or 0.0)))
                if len(hits) >= 2:
                    for canon, x in hits:
                        header_map.setdefault(canon, x)
            continue
        record: dict[str, Any] = {"code": code}
        name: Optional[str] = None
        nums: list[tuple[float, str]] = []
        for tx, obj in zip(texts, cells):
            if tx == code:
                continue
            x = float(obj.get("x") or 0.0)
            if _parse_number(tx) is None:
                if (name is None and len(tx) >= 2
                        and not re.fullmatch(r"[\d.,%+\-—\-]+", tx)):
                    name = tx          # 首个非数值文本（≥2 字）= 证券名称
                continue               # 图标/「--」等杂项忽略
            nums.append((x, tx))
        if name is not None:
            record["name"] = name
        if header_map:
            used: set[str] = set()
            for x, tx in nums:
                best: Optional[tuple[float, str]] = None
                for canon, hx in header_map.items():
                    if canon in used:
                        continue
                    dist = abs(x - hx)
                    if best is None or dist < best[0]:
                        best = (dist, canon)
                if best is not None and best[0] <= 60.0:
                    used.add(best[1])
                    record.setdefault(best[1], tx)
        else:
            for (_x, tx), canon in zip(nums, YH_HOLDING_FIELD_ORDER):
                record.setdefault(canon, tx)
        norm = yh_normalize_holding_record(record)
        if norm.get("code"):
            rows_out.append(norm)
    return rows_out


def yh_locate_text(items: list[dict[str, Any]],
                   candidates: Iterable[str],
                   region: Optional[tuple[float, float, float, float]] = None,
                   ) -> Optional[dict[str, Any]]:
    """视觉辅助定位：文本项中找候选词命中（去空格+横线变体归一（－—–―→-）
    后包含匹配，大小写不敏感），region 限定 (x1,y1,x2,y2)。返回
    {"x","y","text","matched"}（元素中心，窗口/PNG 像素坐标）；取最上
    （再最左）命中保证确定性。"""

    def norm(text: str) -> str:
        squeezed = re.sub(r"\s+", "", str(text or ""))
        return re.sub(r"[－—–―]", "-", squeezed)

    cands: list[tuple[str, str]] = []
    for c in candidates or []:
        cn = norm(c)
        if cn and cn.lower() not in {x[0].lower() for x in cands}:
            cands.append((cn, str(c)))
    if not cands:
        return None
    best: Optional[dict[str, Any]] = None
    for it in items or []:
        text = str(it.get("text") or "")
        target = norm(text)
        if not target:
            continue
        x, y = float(it.get("x") or 0.0), float(it.get("y") or 0.0)
        if region and not (region[0] <= x <= region[2]
                           and region[1] <= y <= region[3]):
            continue
        hit = next((orig for cn, orig in cands if cn.lower() in target.lower()),
                   None)
        if hit is None:
            continue
        cand = {"x": x, "y": y, "text": text, "matched": hit}
        if best is None or (y, x) < (best["y"], best["x"]):
            best = cand
    return best


def yh_switch_verdict(masked_before: Optional[str],
                      masked_after: Optional[str], target_masked: str, *,
                      funds_before: Optional[float] = None,
                      funds_after: Optional[float] = None,
                      ) -> tuple[str, str]:
    """切换后验证裁决（纯函数）：
      verified              脱敏账号栏 == 目标（主锚）
      unverified_funds_jump 脱敏栏读不到但总资产跳变（弱锚：盘中市值自然波动
                            可致假阳性 → fail-closed rc2 交人工/视觉复核）
      failed                其余（含「切到了别的身份」）
    """
    target_sig = yh_masked_signature(target_masked)
    after_sig = yh_masked_signature(masked_after or "")
    if after_sig is not None:
        if target_sig is not None and after_sig == target_sig:
            return "verified", "masked_bar_matched"
        return "failed", f"masked_bar_not_target {masked_after!r}"
    if (funds_before is not None and funds_after is not None
            and abs(funds_after - funds_before) > YH_FUNDS_JUMP_EPSILON):
        return "unverified_funds_jump", (
            f"total_assets {funds_before}->{funds_after} "
            f"masked_after={masked_after!r}")
    return "failed", (f"no_evidence masked_before={masked_before!r} "
                      f"masked_after={masked_after!r}")


def yh_account_state_snapshot() -> dict[str, Any]:
    """[vendored EasyTHS account_state] 账号状态可信副本快照（报告用）。"""
    return {"available_accounts": list(_YH_ACCOUNT_STATE["available_accounts"]),
            "current_used_account": _YH_ACCOUNT_STATE["current_used_account"]}


# ── 菜单坐标配置（坐标定位可配置化） ───────────────────────────────

def yh_menu_config_path(explicit: Optional[Path] = None) -> Optional[Path]:
    """交易菜单坐标配置路径：--menu-config > env
    AISTOCK_THS_YUANHANG_MENU_JSON > runtime/ths_client/yuanhang_menu.json。"""
    if explicit is not None:
        return Path(explicit)
    env = str(os.environ.get(YH_MENU_CONFIG_ENV) or "").strip()
    if env:
        return Path(env)
    return _yh_runtime_dir() / "yuanhang_menu.json"


def load_yh_menu_config(explicit: Optional[Path] = None) -> dict[str, Any]:
    """读菜单坐标配置；缺失/坏文件 → {}（走视觉辅助定位，绝不盲点）。
    结构（坐标=主程序窗口像素，PrintWindow PNG 同空间）：
      {"trade_menu_xy": [x, y],                       # 「交易」菜单按钮
       "accounts": {"acct1": [x, y], "acct2": [x, y]},  # 菜单内账号项
       "account_labels": {"acct1": "曾", "acct2": "王"},  # 视觉定位候选词
       "trade_menu_region": [x1, y1, x2, y2],          # 「交易」搜索区
       "menu_wait_sec": 1.5}                           # 菜单展开缓冲
    """
    path = yh_menu_config_path(explicit)
    if not path or not Path(path).is_file():
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - 坏配置等同无配置（fail-safe 视觉定位）
        return {}


# ── UIA 只读层（comtypes 原生；技法同 survey.UiaReader，只 GetCurrent*） ──

def _yh_uia_texts(hwnd: int) -> list[dict[str, Any]]:
    """UIA Text 元素清单（FindAll Text 控件）。仅 comtypes 不可导入（能力
    缺失）如实降级 []；GetModule/CreateObject/枚举/属性读取异常一律传播。"""
    try:
        from comtypes.client import CreateObject, GetModule
    except ImportError:  # comtypes 缺席=能力缺失（唯一降级分支）
        return []
    GetModule("UIAutomationCore.dll")
    import comtypes.gen.UIAutomationClient as UIAC
    uia = CreateObject(UIAC.CUIAutomation, interface=UIAC.IUIAutomation)
    cond = uia.CreatePropertyCondition(
        UIAC.UIA_ControlTypePropertyId,
        getattr(UIAC, "UIA_TextControlTypeId", 50020))
    root = uia.ElementFromHandle(int(hwnd))
    found = root.FindAll(getattr(UIAC, "TreeScope_Descendants", 4), cond)
    out: list[dict[str, Any]] = []
    for i in range(found.Length):
        el = found.GetElement(i)
        rect = getattr(el, "CurrentBoundingRectangle", None)
        out.append({
            "text": str(getattr(el, "CurrentName", "") or ""),
            "aid": str(getattr(el, "CurrentAutomationId", "") or ""),
            "rect": [int(getattr(rect, "left", 0) or 0),
                     int(getattr(rect, "top", 0) or 0),
                     int(getattr(rect, "right", 0) or 0),
                     int(getattr(rect, "bottom", 0) or 0)],
        })
    return out


def _yh_uia_login_page(hwnd: int) -> bool:
    """登录页探测（实机事实：资金帐户 Edit aid=1001 / 交易密码 Edit aid=1012
    / 登录 Button aid=1006）。只读 ControlType/AutomationId，凭证零接触；
    三锚中 ≥2 即判登录页（已登录面板不会同现 Edit1001+Edit1012）。"""
    try:
        from comtypes.client import CreateObject, GetModule

        GetModule("UIAutomationCore.dll")
        import comtypes.gen.UIAutomationClient as UIAC

        uia = CreateObject(UIAC.CUIAutomation, interface=UIAC.IUIAutomation)
        root = uia.ElementFromHandle(int(hwnd))
        found = root.FindAll(getattr(UIAC, "TreeScope_Descendants", 4),
                             uia.CreateTrueCondition())
        edit_account = edit_password = button_login = False
        ct_edit = getattr(UIAC, "UIA_EditControlTypeId", 50004)
        ct_button = getattr(UIAC, "UIA_ButtonControlTypeId", 50000)
        for i in range(min(found.Length, 400)):
            el = found.GetElement(i)
            aid = str(getattr(el, "CurrentAutomationId", "") or "")
            ct = int(getattr(el, "CurrentControlType", 0) or 0)
            if aid == "1001" and ct == ct_edit:
                edit_account = True
            elif aid == "1012" and ct == ct_edit:
                edit_password = True   # 只看类型与 aid，绝不取值（凭证铁律）
            elif aid == "1006" and ct == ct_button:
                button_login = True
        return sum([edit_account, edit_password, button_login]) >= 2
    except Exception:  # noqa: BLE001
        return False


def _yh_uia_select_tree_item(hwnd: int,
                             name_token: str = "资金股票") -> dict[str, Any]:
    """切资金股票页：UIA TreeItem（ControlType=50024）SelectionItem.Select
    优先（查询页导航，非下单界面）；不可选 → 真实点击元素中心兜底。"""
    try:
        from comtypes.client import CreateObject, GetModule

        GetModule("UIAutomationCore.dll")
        import comtypes.gen.UIAutomationClient as UIAC

        uia = CreateObject(UIAC.CUIAutomation, interface=UIAC.IUIAutomation)
        cond = uia.CreatePropertyCondition(
            UIAC.UIA_ControlTypePropertyId,
            getattr(UIAC, "UIA_TreeItemControlTypeId", 50024))
        root = uia.ElementFromHandle(int(hwnd))
        found = root.FindAll(getattr(UIAC, "TreeScope_Descendants", 4), cond)
        target = None
        for i in range(found.Length):
            el = found.GetElement(i)
            if name_token in str(getattr(el, "CurrentName", "") or ""):
                target = el
                break
        if target is None:
            return {"ok": False, "method": "not_found", "item": name_token}
        try:
            unk = target.GetCurrentPattern(UIAC.UIA_SelectionItemPatternId)
            if unk:
                sel = unk.QueryInterface(UIAC.IUIAutomationSelectionItemPattern)
                sel.Select()
                _yh_note_self_injection()
                return {"ok": True, "method": "uia_selection", "item": name_token}
        except Exception:  # noqa: BLE001 - Select 失败退鼠标点击
            pass
        rect = getattr(target, "CurrentBoundingRectangle", None)
        cx = (int(getattr(rect, "left", 0) or 0)
              + int(getattr(rect, "right", 0) or 0)) // 2
        cy = (int(getattr(rect, "top", 0) or 0)
              + int(getattr(rect, "bottom", 0) or 0)) // 2
        return {"ok": True, "method": "mouse_click", "item": name_token,
                **_yh_click_screen(cx, cy)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "method": "error", "item": name_token,
                "error": f"{type(exc).__name__}: {exc}"}


# ── 资金读取与身份门禁 ─────────────────────────────────────────────

def _yh_read_funds(panel: dict[str, Any],
                   image_items: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    """资金汇总区读取：UIA Text 直读（实测区 y150-320 x150-950）优先，
    空 → 全面板 strict 兜底 → 截图 OCR 同坐标空间兜底；UIA 读到部分字段时
    缺口由截图项补齐（不覆盖 UIA 值）。
    返回 (yh_parse_funds_texts 产物, source)。"""
    texts = _yh_uia_texts(panel["hwnd"])
    items = yh_uia_texts_to_items(texts, panel.get("rect"))
    parsed = yh_parse_funds_texts(items)
    source = "uia_text"
    if not parsed["funds"] and items:
        parsed = yh_parse_funds_texts(items, region=None, strict=True)
        source = "uia_text_full"
    if not parsed["funds"] and image_items:
        parsed = yh_parse_funds_texts(image_items)
        source = "image_items"
        if not parsed["funds"]:
            parsed = yh_parse_funds_texts(image_items, region=None, strict=True)
    if image_items and parsed["funds"]:
        img_parsed = yh_parse_funds_texts(image_items)
        if not img_parsed["funds"]:
            img_parsed = yh_parse_funds_texts(image_items, region=None,
                                              strict=True)
        filled = [k for k, v in img_parsed["funds"].items()
                  if k not in parsed["funds"]]
        for key in filled:
            parsed["funds"][key] = img_parsed["funds"][key]
            parsed["labels"][key] = img_parsed["labels"].get(key, "")
        parsed["matched"] = len(parsed["funds"])
        if filled:
            source = "uia_text+image_fill"
    if not parsed["funds"]:
        return {"funds": {}, "labels": {}, "matched": 0}, "unreadable"
    return parsed, source


def yh_identity_gate(masked: Optional[str],
                     funds_read_ok: bool) -> tuple[Optional[str], str]:
    """身份门禁（fail-closed，纯函数）：
      脱敏栏命中注册表        → (acct, "masked_bar")
      脱敏栏在但非注册账号    → (None, "unknown_masked_id")
      脱敏栏读不到+资金区可读 → (None, "unreadable_funds_alive")
      都读不到               → (None, "not_logged_in")
    canon=None 时调用方绝不归属任何数据（holdings_source=identity_gate_blocked）。"""
    if masked:
        active = yh_account_from_masked(masked)
        if active is not None:
            return active, "masked_bar"
        return None, "unknown_masked_id"
    if funds_read_ok:
        return None, "unreadable_funds_alive"
    return None, "not_logged_in"


def _yh_masked_from_read(read: dict[str, Any]) -> Optional[str]:
    """一次截图读取产物 → 脱敏账号（items 文本 + rows 序列化联合扫描）。"""
    haystack = " ".join([str(it.get("text") or "") for it in read["items"]]
                        + [json.dumps(r, ensure_ascii=False)
                           for r in read["rows"]])
    return yh_normalize_masked(haystack)


# ── 历史逐笔成交（fills）纯解析层 + 单账号快照裁决（2026-11-15 任务书）──
#
# 设计要点：日期/分页/计数全部取自查询页面实际证据（OCR/UIA 文本项），
# 绝不按当前日期猜测；空记录无「共0条」类零计数证据一律 unavailable
# （不得输出成功空结果）；真实重复成交原样保留绝不静默去重；历史委托
# 页（委托时间/申报价格表头且无成交表头）判 wrong_page 绝不冒充逐笔成交。

YH_FILLS_TREE_CANDIDATES = ("历史成交", "历史成交查询", "成交查询", "交割单")
YH_FILLS_COLUMN_ALIASES = {
    "成交日期": "date", "日期": "date", "发生日期": "date", "date": "date",
    "成交时间": "time", "时间": "time", "time": "time",
    "业务名称": "side_raw", "业务": "side_raw", "操作": "side_raw",
    "摘要": "side_raw", "side_raw": "side_raw",
    "证券代码": "code", "股票代码": "code", "code": "code",
    "证券名称": "name", "股票名称": "name", "name": "name",
    "成交数量": "quantity", "数量": "quantity", "成交股数": "quantity",
    "quantity": "quantity",
    "成交价格": "price", "价格": "price", "成交单价": "price",
    "成交均价": "price", "均价": "price", "price": "price",
    "成交金额": "amount", "金额": "amount", "amount": "amount",
    "手续费": "fee", "费用": "fee", "佣金": "fee", "fee": "fee",
    "印花税": "stamp_tax", "过户费": "transfer_fee",
    "stamp_tax": "stamp_tax", "transfer_fee": "transfer_fee",
    "合同号": "contract_no", "合同序号": "contract_no",
    "合同编号": "contract_no", "contract_no": "contract_no",
    "成交编号": "fill_no", "成交单号": "fill_no", "fill_no": "fill_no",
    "股东代码": "shareholder_code", "shareholder_code": "shareholder_code",
    "发生金额": "net_amount", "net_amount": "net_amount",
    "股票余额": "balance_after", "balance_after": "balance_after",
}
_YH_FILLS_NUMERIC_FIELDS = ("quantity", "price", "amount", "fee",
                            "stamp_tax", "transfer_fee", "net_amount",
                            "balance_after")
_YH_FILLS_CANON_FIELDS = ("date", "time", "side", "code", "name", "quantity",
                          "price", "amount", "fee", "stamp_tax",
                          "transfer_fee", "contract_no", "fill_no",
                          "shareholder_code", "net_amount", "balance_after")

_YH_DATE8_RE = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
_YH_DATE_SEP_RE = re.compile(
    r"(?<!\d)(20\d{2})\s*[./\-年]\s*(\d{1,2})\s*[./\-月]\s*(\d{1,2})\s*日?(?!\d)")
_YH_FILLS_COUNT_RE = re.compile(r"共\s*(\d+)\s*[条笔记录]")
_YH_FILLS_PAGE_RE = re.compile(r"第\s*(\d+)\s*页\s*/?\s*共\s*(\d+)\s*页")
_YH_FILLS_NEXT_TOKENS = ("下一页", "下页", "末页")
_YH_FILLS_ORDER_TOKENS = ("委托时间", "申报价格", "委托价格", "委托序号",
                          "合同序号", "委托日期")
_YH_FILLS_TRADE_TOKENS = ("成交日期", "成交价格", "成交数量", "成交金额",
                          "业务名称", "成交时间")
_YH_PAGINATION_TOKEN_RE = re.compile(r"共\s*\d+\s*[条笔记录]|下一页|第\s*\d+\s*页")


def yh_normalize_fill_record(record: dict[str, Any]) -> dict[str, Any]:
    """逐笔成交列名适配；严格解析数值并留存原始行，歧义值不取正数兜底。"""
    row: dict[str, Any] = {}
    aliases_exact = {_yh_norm_colkey(a): c
                     for a, c in YH_FILLS_COLUMN_ALIASES.items()}
    aliases_sub = sorted(aliases_exact.items(), key=lambda p: len(p[0]),
                         reverse=True)
    for key, value in (record or {}).items():
        nk = _yh_norm_colkey(key)
        if not nk:
            continue
        canon = aliases_exact.get(nk)
        if canon is None:
            for an, c in aliases_sub:
                if len(an) >= 2 and (an in nk or nk in an):
                    canon = c
                    break
        if canon and canon not in row:
            row[canon] = value
    for field in _YH_FILLS_NUMERIC_FIELDS:
        if field in row:
            value = row[field]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                number = value
            else:
                text = re.sub(r"(\d)\.\s+(\d)", r"\1.\2", str(value)).strip()
                text = re.sub(r"[-−﹣－‒–—]\s*(?=\d)", "-", text)
                number = (_parse_number(text) if re.fullmatch(
                    r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?",
                    text) else None)
            row[field] = (number if number is not None
                          and -float("inf") < number < float("inf") else None)
    side_raw = str(row.get("side_raw") or "").strip().lower()
    side = None
    if side_raw == "buy" or ("买" in side_raw and "卖" not in side_raw):
        side = "buy"
    elif side_raw == "sell" or ("卖" in side_raw and "买" not in side_raw):
        side = "sell"
    canonical_side = (record or {}).get("side")
    if not side_raw and canonical_side in ("buy", "sell"):
        side = canonical_side
    elif canonical_side is not None and canonical_side != side:
        side = None
    row["side"] = side
    row.pop("side_raw", None)
    for field in ("date", "time", "code", "name", "contract_no", "fill_no",
                  "shareholder_code"):
        if field in row:
            text = str(row[field] or "").strip()
            if field == "time":
                text = re.sub(r"\s*:\s*", ":", text)  # OCR「09 : 42:29」
            row[field] = text or None
    for field in _YH_FILLS_CANON_FIELDS:
        row.setdefault(field, None)
    raw_record = (record or {}).get("raw_record")
    row["raw_record"] = dict(raw_record if isinstance(raw_record, dict)
                             else (record or {}))
    return row


def yh_fills_rows_from_vision(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """视觉模型直接返回的行记录 → 规范逐笔成交行（须 6 位证券代码）。"""
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        norm = yh_normalize_fill_record(row)
        if norm.get("code") and re.fullmatch(r"\d{6}", str(norm["code"])):
            out.append(norm)
    return out


def _yh_fills_header_map(items: list[dict[str, Any]],
                         row_delta: float = 14.0,
                         min_code_x: float = YH_HOLDINGS_MIN_CODE_X,
                         ) -> dict[str, float]:
    """页面表头探测：首个 ≥2 列名命中的行 → {规范列键: x}（y 序最先）。"""
    aliases = {_yh_norm_colkey(a): c
               for a, c in YH_FILLS_COLUMN_ALIASES.items()}
    for row in yh_group_rows(items or [], row_delta=row_delta):
        cells = [o for o in row
                 if float(o.get("x") or 0.0) >= min_code_x]
        hits: list[tuple[str, float]] = []
        for o in cells:
            tx = str(o.get("text") or "").strip()
            canon = YH_FILLS_COLUMN_ALIASES.get(tx) \
                or aliases.get(_yh_norm_colkey(tx))
            if canon:
                hits.append((canon, float(o.get("x") or 0.0)))
        if len(set(c for c, _x in hits)) >= 2:
            out: dict[str, float] = {}
            for canon, x in hits:
                out.setdefault(canon, x)
            return out
    return {}


def yh_fills_from_items(items: list[dict[str, Any]],
                        row_delta: float = 14.0,
                        min_code_x: float = YH_HOLDINGS_MIN_CODE_X,
                        ) -> list[dict[str, Any]]:
    """截图文本项 → 逐笔成交行。表头列名映射优先（x 最近列 ≤60px），
    无表头按 日期/方向/数值列序 兜底；x<min_code_x（导航树列）不参与；
    无 6 位代码的行一律跳过；**同一行完全重复也原样保留（绝不去重）**。"""
    rows_out: list[dict[str, Any]] = []
    header_map: dict[str, float] = {}
    aliases = {_yh_norm_colkey(a): c
               for a, c in YH_FILLS_COLUMN_ALIASES.items()}
    for row in yh_group_rows(items or [], row_delta=row_delta):
        cells = sorted((o for o in row
                        if float(o.get("x") or 0.0) >= min_code_x),
                       key=lambda o: float(o.get("x") or 0.0))
        texts = [str(o.get("text") or "").strip() for o in cells]
        if not any(texts):
            continue
        code = next((tx for tx in texts if re.fullmatch(r"\d{6}", tx)), None)
        if code is None:
            if not header_map:
                header_map = _yh_fills_header_map(items or [],
                                                  row_delta=row_delta,
                                                  min_code_x=min_code_x)
            continue
        assigned: dict[str, Any] = {}
        if header_map:
            used: set[str] = set()
            for tx, obj in zip(texts, cells):
                if tx == code:
                    continue
                x = float(obj.get("x") or 0.0)
                best: Optional[tuple[float, str]] = None
                for canon, hx in header_map.items():
                    if canon in used:
                        continue
                    dist = abs(x - hx)
                    if best is None or dist < best[0]:
                        best = (dist, canon)
                if best is not None and best[0] <= 60.0:
                    used.add(best[1])
                    assigned[best[1]] = tx
        else:
            for tx in texts:
                if tx == code:
                    continue
                if re.fullmatch(r"20\d{6}", tx) and "date" not in assigned:
                    assigned["date"] = tx
                elif ("买" in tx or "卖" in tx) \
                        and "side_raw" not in assigned \
                        and _parse_number(tx) is None:
                    assigned["side_raw"] = tx
                elif _parse_number(tx) is not None:
                    for canon in ("quantity", "price", "amount", "fee"):
                        if canon not in assigned:
                            assigned[canon] = tx
                            break
                elif "name" not in assigned and len(tx) >= 2 \
                        and _parse_number(tx) is None:
                    assigned["name"] = tx
        record: dict[str, Any] = {"code": code}
        record.update(assigned)
        norm = yh_normalize_fill_record(record)
        if norm.get("code"):
            rows_out.append(norm)
    return rows_out


def yh_fills_date_tokens(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """页面文本项 → 日期令牌证据 [{token, iso}]（8 位数字与分隔符两形态，
    仅接受可构造的合法日期；六位证券代码/价格不会误中）。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for it in items or []:
        text = str(it.get("text") or "")
        for match in (list(_YH_DATE8_RE.finditer(text))
                      + list(_YH_DATE_SEP_RE.finditer(text))):
            y_, m_, d_ = (int(g) for g in match.groups())
            try:
                iso = date(y_, m_, d_).isoformat()
            except ValueError:
                continue
            if iso in seen:
                continue
            seen.add(iso)
            out.append({"token": match.group(0), "iso": iso})
    return out


def yh_fills_page_evidence(items: list[dict[str, Any]]) -> dict[str, Any]:
    """页面文本项 → 计数/分页/页面性质证据（共N条、第x/共y页、下一页、
    委托页特征）。orders_like_page=True 表示历史委托页特征且无任何成交
    表头 → 调用方判 wrong_page 绝不当逐笔成交采信。"""
    texts = [str(it.get("text") or "") for it in (items or [])]
    joined = " ".join(texts)
    out: dict[str, Any] = {"count_total": None, "page_current": None,
                           "page_total": None, "next_page_present": False,
                           "orders_like_page": False,
                           "trade_like_page": False}
    m = _YH_FILLS_COUNT_RE.search(joined)
    if m:
        out["count_total"] = int(m.group(1))
    pm = _YH_FILLS_PAGE_RE.search(joined)
    if pm:
        out["page_current"] = int(pm.group(1))
        out["page_total"] = int(pm.group(2))
    out["next_page_present"] = any(tok in t for t in texts
                                   for tok in _YH_FILLS_NEXT_TOKENS)
    orders = any(tok in joined for tok in _YH_FILLS_ORDER_TOKENS)
    trade = any(tok in joined for tok in _YH_FILLS_TRADE_TOKENS)
    out["orders_like_page"] = orders and not trade
    out["trade_like_page"] = trade
    return out


def yh_fills_summary_evidence(items: list[dict[str, Any]],
                              row_delta: float = 14.0,
                              min_code_x: float = YH_HOLDINGS_MIN_CODE_X,
                              ) -> dict[str, Any]:
    """页面「汇总」行证据：按表头 x 映射合计数值（数量/金额/印花税/
    发生金额等，实机 2026-11-15：远航版历史成交页无「共N条」，底部
    「汇总」行是客户端自带的合计=覆盖度对账证据源）。"""
    out: dict[str, Any] = {"present": False}
    header_map = _yh_fills_header_map(items, row_delta, min_code_x)
    if not header_map:
        return out
    for row in yh_group_rows(items or [], row_delta=row_delta):
        cells = [o for o in row
                 if float(o.get("x") or 0.0) >= min_code_x]
        texts = [str(o.get("text") or "").strip() for o in cells]
        if not any("汇总" in t for t in texts):
            continue
        mapped: dict[str, float] = {}
        used: set[str] = set()
        for tx, obj in zip(texts, cells):
            if "汇总" in tx:
                continue
            num = _parse_number(tx)
            if num is None:
                continue
            x = float(obj.get("x") or 0.0)
            best: Optional[tuple[float, str]] = None
            for canon, hx in header_map.items():
                if canon in used or canon not in _YH_FILLS_NUMERIC_FIELDS:
                    continue
                dist = abs(x - hx)
                if best is None or dist < best[0]:
                    best = (dist, canon)
            if best is not None and best[0] <= 60.0:
                used.add(best[1])
                mapped[best[1]] = num
        # 工具条「汇总」按钮行无数值 → 继续找真正的合计行
        if any(k in mapped for k in _YH_FILLS_NUMERIC_FIELDS):
            out.update(mapped)
            out["present"] = True
            return out
    return out


def yh_fills_summary_reconciled(fev: dict[str, Any],
                                tolerance: float = 0.5) -> bool:
    """逐项精确对账；金额用整数分，不舍入、不使用兼容参数 tolerance。

    数量保留旧约定：非负汇总可表示带号行数量的净额绝对值；负汇总只能
    与带号合计相等。发生金额等金额字段始终保留符号，不能 abs 对账。
    """
    summary = (fev or {}).get("summary") or {}
    rows = (fev or {}).get("rows") or []
    if not isinstance(summary, dict) or summary.get("present") is not True \
            or not isinstance(rows, (list, tuple)) or not rows \
            or any(not isinstance(r, dict) for r in rows):
        return False

    def decimal_parts(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not -float("inf") < value < float("inf"):
            return None
        match = re.fullmatch(
            r"([+-]?)([0-9]+)(?:\.([0-9]+))?(?:e([+-]?[0-9]+))?",
            str(value).lower())
        if match is None:
            return None
        sign, whole, fraction, exponent = match.groups()
        fraction = fraction or ""
        coefficient = int(whole + fraction)
        if sign == "-":
            coefficient = -coefficient
        return coefficient, int(exponent or "0") - len(fraction)

    # 均价和余额不是可加总的成交额；未证明其汇总语义时不能忽略它们。
    if summary.get("price") is not None or summary.get("balance_after") is not None:
        return False
    if summary.get("quantity") is None:
        return False
    money_compared = False
    for field in ("quantity", "amount", "stamp_tax", "net_amount", "fee",
                  "transfer_fee"):
        if summary.get(field) is None:
            continue
        parts = [decimal_parts(r.get(field)) for r in rows]
        parts.append(decimal_parts(summary[field]))
        if any(p is None for p in parts):
            return False
        if field == "quantity":
            exponent = min(p[1] for p in parts)
            units = [p[0] * 10 ** (p[1] - exponent) for p in parts]
        else:
            units = []
            for coefficient, exponent in parts:
                if exponent < -2:
                    divisor = 10 ** (-2 - exponent)
                    if coefficient % divisor:
                        return False  # 未证明的分以下数值不能靠容差或舍入吞掉
                    units.append(coefficient // divisor)
                else:
                    units.append(coefficient * 10 ** (exponent + 2))
            money_compared = True
        row_total, summary_total = sum(units[:-1]), units[-1]
        if field == "quantity":
            if summary_total != row_total and not (
                    summary_total >= 0 and summary_total == abs(row_total)):
                return False
        elif summary_total != row_total:
            return False
    return money_compared


def yh_fills_verdict(fev: dict[str, Any]) -> dict[str, Any]:
    """查询日期读回、真实表头、有效逐笔行及明确单页计数全部齐备才 ok。
    汇总仅用于交叉核对，绝不替代查询范围、计数或分页证据；不改动原行。
    显式 coverage_evidence 反证（rows/columns/pages_complete 任一 False）
    优先降级 partial：计数/日期/表头齐全也不得升级；键缺席=旧调用者语义。
    """
    out: dict[str, Any] = {"status": "unavailable", "reason": None,
                           "coverage_complete": False,
                           "date_range_proven": False, "row_count": 0}
    if not (fev or {}).get("attempted"):
        out["reason"] = (fev or {}).get("reason") or "fills_not_attempted"
        return out
    if fev.get("identity_reject"):
        out["reason"] = ("fills_identity_unverified"
                         if fev.get("identity_reject") == "unreadable"
                         else "fills_identity_changed")
        return out
    if fev.get("wrong_page"):
        out["reason"] = "wrong_page"
        return out
    rows = fev.get("rows") or []
    if not isinstance(rows, (list, tuple)):
        out["reason"] = "fills_rows_invalid"
        return out
    out["row_count"] = len(rows)
    query_range = fev.get("date_range")
    query_dates = []
    if fev.get("date_range_source") == "query_controls_readback" \
            and isinstance(query_range, dict):
        for key in ("start", "end"):
            text = query_range.get(key)
            if not isinstance(text, str) or not re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
                break
            try:
                query_dates.append(date(int(text[:4]), int(text[5:7]),
                                        int(text[8:10])))
            except ValueError:
                break
    date_proven = len(query_dates) == 2 and query_dates[0] <= query_dates[1]
    out["date_range_proven"] = date_proven
    for row in rows:
        valid = isinstance(row, dict)
        if valid:
            time_text = row.get("time")
            time_match = (re.fullmatch(r"([0-9]{2}):([0-9]{2}):([0-9]{2})",
                                      time_text)
                          if isinstance(time_text, str) else None)
            code = row.get("code")
            valid = bool(time_match and int(time_match.group(1)) < 24
                         and int(time_match.group(2)) < 60
                         and int(time_match.group(3)) < 60
                         and row.get("side") in ("buy", "sell")
                         and isinstance(code, str)
                         and re.fullmatch(r"[0-9]{6}", code))
            for field in _YH_FILLS_NUMERIC_FIELDS:
                value = row.get(field)
                if value is None and field not in ("quantity", "price", "amount"):
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) \
                        or not -float("inf") < value < float("inf"):
                    valid = False
                elif (field in ("quantity", "amount") and value == 0) \
                        or (field == "price" and value <= 0):
                    valid = False
            text = row.get("date")
            if not isinstance(text, str) or not re.fullmatch(
                    r"(?:[0-9]{8}|[0-9]{4}-[0-9]{2}-[0-9]{2})", text):
                valid = False
            else:
                text = text.replace("-", "")
                try:
                    row_date = date(int(text[:4]), int(text[4:6]), int(text[6:8]))
                    if date_proven and not query_dates[0] <= row_date <= query_dates[1]:
                        valid = False
                except ValueError:
                    valid = False
        if not valid:
            out.update(status="partial", reason="fills_row_invalid")
            return out
    columns = fev.get("header_columns")
    headers_proven = (isinstance(columns, (list, tuple, set, frozenset))
                      and all(key in columns for key in
                              ("date", "time", "side_raw", "code", "quantity",
                               "price", "amount")))
    count_total = fev.get("count_total")
    count_proven = (isinstance(count_total, int)
                    and not isinstance(count_total, bool) and count_total >= 0)
    single_page = all(isinstance(fev.get(key), int)
                      and not isinstance(fev.get(key), bool) and fev.get(key) == 1
                      for key in ("page_current", "page_total"))
    single_page = single_page and fev.get("next_page_present") is False
    if not rows and not (date_proven and headers_proven and count_proven
                         and count_total == 0 and single_page):
        out["reason"] = "empty_not_proven"
        return out
    if not date_proven:
        out.update(status="partial", reason="date_range_unproven")
        return out
    if not headers_proven:
        out.update(status="partial", reason="header_columns_unproven")
        return out
    if not count_proven:
        out.update(status="partial", reason="coverage_unproven")
        return out
    if count_total != len(rows) or not single_page:
        out.update(status="partial", reason="pagination_incomplete")
        return out
    count_read = fev.get("row_count_read")
    if count_read is not None and (not isinstance(count_read, int)
                                  or isinstance(count_read, bool)
                                  or count_read != len(rows)):
        out.update(status="partial", reason="row_count_conflict")
        return out
    summary = fev.get("summary")
    if summary is not None and (not isinstance(summary, dict)
                               or summary.get("present")
                               or any(summary.get(k) is not None
                                      for k in _YH_FILLS_NUMERIC_FIELDS)):
        if not yh_fills_summary_reconciled(fev):
            out.update(status="partial" if rows else "unavailable",
                       reason="summary_mismatch")
            return out
    counter = fev.get("coverage_evidence")
    if isinstance(counter, dict) and any(counter.get(key) is False
                                         for key in ("rows_complete",
                                                     "columns_complete",
                                                     "pages_complete")):
        # 显式覆盖反证优先：纵/横/分页任一未确认即不得升级 ok/complete。
        out.update(status="partial", reason="coverage_counter_evidence")
        return out
    out.update(status="ok", reason="ok" if rows else "empty_proven_by_zero_count",
               coverage_complete=True,
               coverage_evidence="count_match")
    return out


def yh_single_snapshot(evidence):
    """单账号快照裁决（纯函数，零副作用）。证据不充分时绝不返回成功或
    空仓：funds/holdings 一律 None（未知≠空仓）。tests 契约以受限 AST
    子集离线执行本函数（只允许 evidence.get、安全内建、纯比较与字面量），
    因此此处禁用 f-string、evidence.get 之外的属性访问、嵌套函数、增强
    赋值与下标写入——修改前先读 tests/test_ths_single_snapshot_contract。
    """
    expected = evidence.get("expected_account")
    before = evidence.get("identity_before")
    after = evidence.get("identity_after")
    funds = evidence.get("funds")
    holdings = evidence.get("holdings")
    headers = evidence.get("headers")
    ok = True
    reason = "ok"
    if expected is None:
        ok = False
        reason = "expected_account_unknown"
    elif before is None:
        ok = False
        if funds:
            reason = "identity_unreadable"
        else:
            reason = "not_logged_in"
    elif before != expected:
        ok = False
        reason = "wrong_account"
    elif after is None:
        ok = False
        reason = "identity_after_unverified"
    elif after != expected:
        ok = False
        reason = "identity_changed"
    elif evidence.get("stock_page_confirmed") is not True:
        ok = False
        reason = "stock_page_unconfirmed"
    elif evidence.get("ocr_available") is not True:
        ok = False
        reason = "ocr_unavailable"
    elif not isinstance(headers, list) or "code" not in headers or "quantity" not in headers:
        ok = False
        reason = "headers_incomplete"
    elif not isinstance(funds, dict) or "balance" not in funds or "total_assets" not in funds:
        ok = False
        reason = "funds_incomplete"
    elif evidence.get("coverage_complete") is not True:
        ok = False
        reason = "holdings_coverage_unproven"
    elif holdings is None:
        ok = False
        reason = "holdings_unknown"
    else:
        rows_bad = False
        digits = "0123456789"
        for row in holdings:
            if "code" not in row:
                rows_bad = True
            elif "quantity" not in row:
                rows_bad = True
            else:
                code = row["code"]
                qty = row["quantity"]
                code_ok = isinstance(code, str) and len(code) == 6 and all(ch in digits for ch in code)
                qty_ok = (isinstance(qty, int) or isinstance(qty, float)) and qty == int(qty) and qty >= 0
                if not code_ok:
                    rows_bad = True
                if not qty_ok:
                    rows_bad = True
        if rows_bad:
            ok = False
            reason = "holdings_rows_invalid"
    if ok and holdings == [] and evidence.get("empty_confirmed") is not True:
        ok = False
        reason = "holdings_empty_unconfirmed"
    if ok:
        return {"status": "ok", "rc": 0, "account": expected,
                "funds": funds, "holdings": holdings, "reason": reason}
    return {"status": "unavailable", "rc": 4, "account": expected,
            "funds": None, "holdings": None, "reason": reason}


def yh_holdings_header_canons(rows: list[dict[str, Any]],
                              items: list[dict[str, Any]]) -> list[str]:
    """持仓表头证据（规范列键）：视觉行键位优先；无行时从页面表头行
    列名命中提取（≥2 命中才认定表头）。"""
    heads = [f for f in _YH_HOLDING_CANON_FIELDS
             if any(f in (r or {}) for r in (rows or []))]
    if heads:
        return heads
    aliases = {_yh_norm_colkey(a): c
               for a, c in YH_HOLDING_COLUMN_ALIASES.items()}
    for row in yh_group_rows(items or []):
        hits: list[str] = []
        for o in row:
            tx = str(o.get("text") or "").strip()
            canon = YH_HOLDING_COLUMN_ALIASES.get(tx) \
                or aliases.get(_yh_norm_colkey(tx))
            if canon and canon not in hits:
                hits.append(canon)
        if len(hits) >= 2:
            return sorted(set(hits), key=_YH_HOLDING_CANON_FIELDS.index)
    return []


def _yh_holdings_no_pagination(items: list[dict[str, Any]]) -> bool:
    """持仓页无分页控件证据（有 共N条/下一页/第x页 令牌 → False）。"""
    return not any(_YH_PAGINATION_TOKEN_RE.search(str(it.get("text") or ""))
                   for it in (items or []))


# ── GUI 串行锁（单账号链：锁范围=身份核验+全部交互） ────────────────

YH_GUI_LOCK_NAME = "yuanhang_window.lock"


def _yh_injection_idle_ok(rep: Optional[dict[str, Any]] = None) -> bool:
    """全局输入空闲门禁（GetLastInputInfo，机器级）：空闲≥
    YH_OWNER_ACTIVE_IDLE_S 才允许任何键击/点击注入；读不到输入状态 →
    fail-closed 拒注入。探测值入 rep['input_idle_probes'] 留证。"""
    try:
        idle = float(_yh_input_idle_seconds())
    except Exception:  # noqa: BLE001 - 状态不可读 = 不注入
        idle = -1.0
    if rep is not None:
        rep.setdefault("input_idle_probes", []).append(round(idle, 1))
    return idle >= YH_OWNER_ACTIVE_IDLE_S


def _yh_gui_lock_path() -> Path:
    """GUI 串行锁路径（runtime/ths_client/yuanhang_window.lock）。"""
    return _yh_runtime_dir() / YH_GUI_LOCK_NAME


def _yh_acquire_gui_lock(timeout_seconds: float = 10.0,
                         lock_path: Optional[Path] = None) -> dict[str, Any]:
    """GUI 串行锁（msvcrt LK_NBLCK 轮询至超时，技法同源
    services.ths_account_bridge._ths_window_operation_lock；跨进程互斥
    远航版自动链）。未获锁 → acquired=False，调用方 fail-closed 绝不碰
    GUI。lock_path 显式覆盖（离线测试用 tmp 路径）。"""
    import msvcrt  # noqa: PLC0415 - Windows 专属（本段整体 Windows 守卫）

    path = Path(lock_path) if lock_path is not None else _yh_gui_lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = path.open("a+b")
    except OSError as exc:
        return {"acquired": False, "path": str(path), "fh": None,
                "error": f"{type(exc).__name__}: {exc}"}
    deadline = time.time() + max(0.0, float(timeout_seconds))
    while True:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return {"acquired": True, "path": str(path), "fh": fh}
        except OSError:
            if time.time() >= deadline:
                fh.close()
                return {"acquired": False, "path": str(path), "fh": None}
            time.sleep(0.2)


def _yh_release_gui_lock(lock: Optional[dict[str, Any]]) -> None:
    """释放 GUI 串行锁（幂等，fh 缺席时安全空操作）。"""
    fh = (lock or {}).get("fh")
    if fh is None:
        return
    try:
        import msvcrt  # noqa: PLC0415

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        try:
            fh.close()
        except OSError:
            pass


# ── switch-account 主体 ────────────────────────────────────────────

def run_switch_account(*, to: str, happ_exe: Path = YUANHANG_EXE_DEFAULT,
                       menu_config: Optional[Path] = None,
                       menu_wait_sec: float = 1.5, settle_sec: float = 3.0,
                       f12_wait: float = 8.0, main_wait: float = 20.0,
                       dry_run: bool = False,
                       report_root: Optional[Path] = None) -> dict[str, Any]:
    """远航版账号切换（主程序「交易」菜单真实坐标点击）。

    a 确保主程序运行（Popen 带 cwd）；
    b Owner 活跃守卫（THS 窗前台+输入空闲<90s → owner_active 绝不点击）；
    c 面板 F12 拉起/枚举 + 登录页探测（未登录 fail-closed rc4，绝不切）；
    d 活动账号=目标 → already_on_target 幂等不触 GUI
      [vendored EasyTHS account_switch 幂等语义]；
    e 交易菜单→目标账号项：坐标配置优先，缺失走视觉辅助定位
      （PrintWindow 主程序 + 文本项定位），定位不到 → menu_not_located
      fail-closed（绝不盲点）；
    f 切换后验证：脱敏账号栏==目标（主锚）/总资产跳变（弱锚 → unverified
      fail-closed rc2）；
    g 验证通过才刷新账号状态缓存 [vendored EasyTHS account_state 语义]。
    dry_run=True 执行到点击前为止（报 would_click 坐标，零真实点击）。
    rc：0 ok/幂等 · 1 失败 · 2 unverified · 3 owner_active · 4 fail-closed。
    """
    started = datetime.now(timezone.utc)
    target_meta = YH_ACCOUNTS.get(to) or {}
    report: dict[str, Any] = {
        "schema": SCHEMA, "command": "switch-account", "yuanhang": True,
        "status": "failure", "rc": 1, "to": to,
        "target_masked_id": target_meta.get("masked_id"),
        "masked_id_before": None, "masked_id_after": None,
        "from_account": None, "clicks": [], "would_click": [],
        "steps": [], "captures": [], "credentials_touched": False,
        "dry_run": bool(dry_run), "started_at": started.isoformat(),
    }
    try:
        try:
            report["steps"].append(
                {"step": "main_ensure",
                 **_yh_ensure_main(happ_exe, main_wait)})
        except ThsXiadanError as exc:
            report["status"] = "main_launch_failed"
            report["reason"] = f"{type(exc).__name__}: {exc}"
            return report
        main = _wait_for(_yh_main_window, 5.0, 0.5)
        if main is None:
            report["status"] = "main_window_not_found"
            report["reason"] = "happ 运行但无可见顶层窗"
            return report
        guard_hwnds = [main["hwnd"]] + [p["hwnd"] for p in _yh_enum_panels()]
        if any(_yh_owner_using(h) for h in guard_hwnds):
            report["status"] = "owner_active"
            report["rc"] = 3
            report["reason"] = ("Owner 在 THS 窗上（前台+输入空闲<90s），"
                                "绝不点击（Owner 活跃守卫）")
            return report
        # 真实点击前必须前台化主程序（否则点击会落在最顶层窗口上）
        fg = _yh_set_foreground(main["hwnd"])
        report["steps"].append({"step": "main_foreground", **fg})
        time.sleep(0.3)
        panel, steps = _yh_ensure_panel(main["hwnd"], f12_wait)
        report["steps"].extend(steps)
        if panel is None:
            report["status"] = "not_logged_in_fail_closed"
            report["rc"] = 4
            report["reason"] = ("F12 后仍无交易面板：最近会话不在（未登录），"
                                "fail-closed 绝不切账号")
            return report
        if _yh_uia_login_page(panel["hwnd"]):
            report["captures"].append(
                _yh_capture_window(panel["hwnd"], "switch_login_page"))
            report["status"] = "not_logged_in_fail_closed"
            report["rc"] = 4
            report["reason"] = ("面板停在登录页（UIA Edit1001/Edit1012/"
                                "Button1006）：会话未登录，fail-closed")
            return report
        cap_before = _yh_capture_window(panel["hwnd"], "switch_before")
        read_before = _yh_read_image(cap_before["path"])
        report["captures"].append(cap_before)
        masked_before = _yh_masked_from_read(read_before)
        report["masked_id_before"] = masked_before
        active = yh_account_from_masked(masked_before)
        report["from_account"] = active
        if masked_before and active is None:
            report["status"] = "identity_gate_unknown_account"
            report["rc"] = 4
            report["reason"] = (f"顶栏脱敏账号 {masked_before!r} 不在注册表，"
                                "fail-closed 不切（防误动他人会话）")
            return report
        if active == to:
            _YH_ACCOUNT_STATE["current_used_account"] = to
            report["status"] = "already_on_target"
            report["rc"] = 0
            report["reason"] = (f"当前已是 {to}，幂等不触 GUI "
                                "[vendored EasyTHS account_switch]")
            return report
        funds_pre, _src = _yh_read_funds(panel, read_before["items"])
        funds_before = funds_pre["funds"].get("total_assets")
        cfg = load_yh_menu_config(menu_config)
        menu_xy = cfg.get("trade_menu_xy")
        if menu_xy:
            click = {"target": "trade_menu",
                     "window_xy": [float(menu_xy[0]), float(menu_xy[1])]}
            if dry_run:
                report["would_click"].append(click)
            else:
                report["clicks"].append(
                    {"target": "trade_menu",
                     **_yh_click_window(main["hwnd"], menu_xy)})
        else:
            cap_menu = _yh_capture_window(main["hwnd"], "switch_menu")
            read_menu = _yh_read_image(cap_menu["path"])
            report["captures"].append(cap_menu)
            raw_region = cfg.get("trade_menu_region") or YH_MENU_REGION_DEFAULT
            region = (float(raw_region[0]), float(raw_region[1]),
                      float(raw_region[2]), float(raw_region[3]))
            # 候选按特异性排序：交易菜单 > 当前账号下拉（实机顶栏形态
            # 「广发证券－王*美▼」，横线变体经 yh_locate_text 归一命中）。
            # 不用单字 Owner 名（实机教训：会误中消息流文本）。
            menu_cands = ["交易"]
            if active:
                cur = YH_ACCOUNTS[active]
                menu_cands += [cur["label"], cur["masked_id"]]
            hit = None
            for cand in menu_cands:
                hit = yh_locate_text(read_menu["items"], (cand,), region=region)
                if hit is not None:
                    break
            if hit is None:
                report["status"] = "menu_not_located"
                report["rc"] = 4
                report["reason"] = ("交易菜单/账号下拉定位失败（候选 "
                                    f"{menu_cands!r}；无坐标配置且视觉辅助未"
                                    "命中；绝不盲点 fail-closed）")
                return report
            report["menu_hit"] = hit
            click = {"target": "trade_menu", "window_xy": [hit["x"], hit["y"]],
                     "matched": hit["text"]}
            if dry_run:
                report["would_click"].append(click)
            else:
                report["clicks"].append(
                    {"target": "trade_menu",
                     **_yh_click_window(main["hwnd"], (hit["x"], hit["y"]))})
        if dry_run:
            item_xy_dry = (cfg.get("accounts") or {}).get(to)
            if item_xy_dry:
                report["would_click"].append(
                    {"target": to,
                     "window_xy": [float(item_xy_dry[0]),
                                   float(item_xy_dry[1])]})
            report["status"] = "dry_run_located"
            report["rc"] = 0
            report["reason"] = ("dry-run：定位链验证完毕，零真实点击"
                                + ("" if (cfg.get("accounts") or {}).get(to)
                                   else "（账号项坐标未配置，真实点击阶段"
                                   "将走视觉辅助定位）"))
            return report
        time.sleep(float(cfg.get("menu_wait_sec") or menu_wait_sec))
        item_xy = (cfg.get("accounts") or {}).get(to)
        if item_xy:
            report["clicks"].append(
                {"target": to,
                 **_yh_click_window(main["hwnd"], item_xy)})
        else:
            cap_open = _yh_capture_window(main["hwnd"], "switch_menu_open")
            read_open = _yh_read_image(cap_open["path"])
            report["captures"].append(cap_open)
            cand = (cfg.get("account_labels") or {}).get(to)
            cfg_cands = [cand] if isinstance(cand, str) else list(cand or [])
            # 候选按特异性排序（配置项 > 脱敏账号 > 完整标签）。绝不使用
            # 单字 Owner 名做候选：实机 2026-11-14 单字「曾」误中消息流
            # 「3分钟快速开户Q曾14:50:14」（fail-closed 已拦下未盲点）。
            cands = cfg_cands + [target_meta.get("masked_id"),
                                 target_meta.get("label")]
            hit = None
            for c in cands:
                hit = yh_locate_text(read_open["items"], (c,))
                if hit is not None:
                    break
            if hit is None:
                if int(USER32.GetForegroundWindow() or 0) == int(main["hwnd"]):
                    try:  # 收起菜单恢复原状（ESC 仅在主程序确为前台时发送）
                        USER32.keybd_event(VK_ESCAPE, 0, 0, 0)
                        time.sleep(0.05)
                        USER32.keybd_event(VK_ESCAPE, 0, KEYEVENTF_KEYUP, 0)
                        _yh_note_self_injection()
                        report["steps"].append({"step": "menu_dismissed_esc"})
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    report["steps"].append(
                        {"step": "menu_dismiss_skipped",
                         "reason": "主程序非前台，ESC 可能落在面板上（实机："
                                   "面板收到 ESC 会被隐藏），跳过"})
                report["status"] = "menu_item_not_located"
                report["rc"] = 4
                report["reason"] = (f"目标账号项定位失败（候选 {cands!r}；"
                                    "绝不盲点 fail-closed，菜单已尽力收起）")
                return report
            report["menu_item_hit"] = hit
            report["clicks"].append(
                {"target": to,
                 **_yh_click_window(main["hwnd"], (hit["x"], hit["y"]))})
        # 切换生效缓冲 + 重枚举（面板句柄可能已变）
        time.sleep(settle_sec)
        panels_after = _wait_for(lambda: _yh_enum_panels(), 5.0, 0.5) or []
        if not panels_after:
            report["status"] = "panel_lost_after_switch"
            report["reason"] = "点击账号项后面板枚举为空"
            return report
        panel2 = panels_after[0]
        cap_after = _yh_capture_window(panel2["hwnd"], "switch_after")
        read_after = _yh_read_image(cap_after["path"])
        report["captures"].append(cap_after)
        masked_after = _yh_masked_from_read(read_after)
        report["masked_id_after"] = masked_after
        funds_post, _src2 = _yh_read_funds(panel2, read_after["items"])
        funds_after = funds_post["funds"].get("total_assets")
        verdict, why = yh_switch_verdict(
            masked_before, masked_after, str(target_meta.get("masked_id")),
            funds_before=funds_before, funds_after=funds_after)
        report["evidence"] = {"verdict": verdict, "why": why,
                              "funds_before_total_assets": funds_before,
                              "funds_after_total_assets": funds_after}
        if verdict == "verified":
            _YH_ACCOUNT_STATE["current_used_account"] = to
            report["status"] = "switched"
            report["rc"] = 0
        elif verdict == "unverified_funds_jump":
            report["status"] = "switch_unverified"
            report["rc"] = 2
            report["reason"] = (f"脱敏栏读不到仅总资产跳变（fail-closed 交"
                                f"人工/视觉复核）：{why}")
        else:
            report["status"] = "switch_failed"
            report["reason"] = f"切换未验证：{why}"
        return report
    except ThsXiadanError as exc:
        report["status"] = "failure"
        report["reason"] = f"{type(exc).__name__}: {exc}"
        return report
    except Exception as exc:  # noqa: BLE001 - fail-closed：异常如实入报告
        report["status"] = "failure"
        report["reason"] = f"{type(exc).__name__}: {exc}"
        return report
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["account_state"] = yh_account_state_snapshot()
        try:
            report["report_path"] = str(_write_report(
                report, report_root, prefix="switch_account"))
        except Exception:  # noqa: BLE001
            pass


# ── read-account 主体 ──────────────────────────────────────────────

def _yh_collect_with_adapter(adapter, expected_account, *, with_fills=False):
    """Collect evidence through the six-method read-only adapter protocol.

    This seam does not call the real collector or decide a data verdict. Guard
    every identity/navigation/data read on both sides, and independently reread
    identity on both sides of each navigation/data operation. Payload identity
    fields are never authority. Date sources and row fields pass through without
    aliases or inferred dates; their interpretation belongs to the data verdict.
    """
    def empty_evidence(abort_status=None):
        # Rebuild, rather than clear selected fields: revoke even future metadata.
        return {
            "expected_account": expected_account,
            "identity_before": None, "identity_after": None,
            "stock_page_confirmed": False,
            "funds": {}, "funds_labels": {}, "funds_as_of": None,
            "funds_coverage_complete": False, "funds_evidence": {},
            "holdings": [], "headers": [], "holdings_as_of": None,
            "holdings_evidence": {}, "coverage_evidence": {},
            "coverage_complete": False, "empty_confirmed": False,
            "fills": {
                "attempted": False, "rows": [], "header_columns": [],
                "date_range": None, "date_range_source": None,
                "coverage_complete": False, "coverage_evidence": {},
                "empty_confirmed": False,
            },
            "abort_status": abort_status,
        }

    def copy_evidence(value):
        # Detach plain evidence containers before another adapter call can mutate
        # them, including a mutable fingerprint returned by identity().
        if isinstance(value, dict):
            return {key: copy_evidence(item) for key, item in value.items()}
        if isinstance(value, list):
            return [copy_evidence(item) for item in value]
        if isinstance(value, tuple):
            return tuple(copy_evidence(item) for item in value)
        return value

    ev = empty_evidence()
    state = {"identity": None, "abort_status": None}

    def reject(reason):
        state["abort_status"] = reason
        raise RuntimeError(reason)

    def check_guard():
        guard = adapter.guard()
        if not isinstance(guard, dict):
            reject("guard_unreadable_fail_closed")
        if guard.get("owner_active") is True:
            reject("owner_active_fail_closed")
        if guard.get("ok") is not True or guard.get("owner_active") is not False:
            reject("guard_rejected_fail_closed")

    def check_identity():
        check_guard()
        identity = copy_evidence(adapter.identity())
        check_guard()
        if not isinstance(identity, dict):
            reject("identity_unreadable_fail_closed")
        account = identity.get("account")
        fingerprint = identity.get("fingerprint")
        if not account or not fingerprint:
            reject("identity_unreadable_fail_closed")
        if account != expected_account:
            reject("wrong_account_fail_closed")
        current = (account, fingerprint)
        if state["identity"] is None:
            state["identity"] = current
            ev["identity_before"] = account
        elif current != state["identity"]:
            reject("identity_changed_fail_closed")
        ev["identity_after"] = account

    def guarded_read(method, *args):
        check_identity()
        check_guard()
        payload = copy_evidence(method(*args))
        check_guard()
        check_identity()
        return payload if isinstance(payload, dict) else {}

    def select_query(page):
        # Only these two literal query pages are ever passed to the adapter.
        selection = guarded_read(adapter.select_query, page)
        if (selection.get("ok") is not True
                or selection.get("selected") is not True
                or selection.get("page_confirmed") is not True
                or selection.get("page") != page):
            reject("query_page_unconfirmed_fail_closed")

    try:
        select_query("资金股票")
        ev["stock_page_confirmed"] = True

        funds = guarded_read(adapter.read_funds)
        ev["funds_evidence"] = funds
        funds_data = funds.get("funds")
        if isinstance(funds_data, dict):
            ev["funds"] = funds_data
        ev["funds_labels"] = funds.get("funds_labels")
        ev["funds_as_of"] = funds.get("as_of")
        ev["funds_coverage_complete"] = (
            isinstance(funds_data, dict) and funds.get("coverage_complete") is True)

        holdings = guarded_read(adapter.read_holdings)
        ev["holdings_evidence"] = holdings
        holdings_data = holdings.get("holdings")
        headers = holdings.get("headers")
        if isinstance(holdings_data, list):
            ev["holdings"] = holdings_data
        if isinstance(headers, list):
            ev["headers"] = headers
        ev["holdings_as_of"] = holdings.get("as_of")
        ev["coverage_evidence"] = holdings.get("coverage_evidence")
        ev["coverage_complete"] = (
            ev["funds_coverage_complete"]
            and isinstance(holdings_data, list) and isinstance(headers, list)
            and holdings.get("coverage_complete") is True)
        ev["empty_confirmed"] = (
            ev["coverage_complete"] and not ev["holdings"]
            and holdings.get("empty_confirmed") is True)

        if with_fills:
            select_query("历史成交")
            fills = guarded_read(adapter.read_fills)
            # Keep partial rows, duplicate rows and all supplied evidence. In
            # particular, do not translate synthetic direction/date-source names
            # into the production side/query-controls-readback contract.
            fills.setdefault("attempted", True)
            fills.setdefault("rows", [])
            fills.setdefault("header_columns", [])
            fills.setdefault("date_range", None)
            fills.setdefault("date_range_source", None)
            fills.setdefault("coverage_evidence", {})
            fills["coverage_complete"] = fills.get("coverage_complete") is True
            fills["empty_confirmed"] = (
                fills["coverage_complete"] and fills.get("rows") == []
                and fills.get("empty_confirmed") is True)
            ev["fills"] = fills
        return ev
    except Exception:  # Protocol faults stop the chain; never attempt a fallback.
        # Do not expose adapter exception text (it may contain account data).
        return empty_evidence(state["abort_status"] or "adapter_exception_fail_closed")


# ════════════════════════════════════════════════════════════════════
# 单账号只读入口（2026-11-15 任务书）：会话级命名互斥锁 + 真实六方法
# adapter（guard/identity/select_query/read_funds/read_holdings/read_fills）
# + 纯报告组装。本段不含键鼠/焦点/F12/前台/文件截图/远程视觉/runtime
# 任何路径；新 read-account 链绝不走旧 collector。实机依据：
# ths_navigation_probe_20261115（进程链/桌面/输入节拍/身份锚/查询树/
# 页面锚均实机验证）、ths_history_readprobe_20261115（PrintWindow 内存
# 截图 + 本地 RapidOCR 显式模型、网格 Scroll 模式水平 ViewSize≈53.9%）。
# ════════════════════════════════════════════════════════════════════

_YH_RO_MUTEX_NAME = "Local\\AiStock.ths_client.readonly_entry.v1"
_YH_RO_WAIT_OBJECT_0 = 0x00000000
_YH_RO_WAIT_ABANDONED = 0x00000080
_YH_RO_WAIT_TIMEOUT = 0x00000102
# 探针实机证实的远航版交易安装根（YUANHANG_EXE_DEFAULT 是旧启动路径
# 常量；实际校验以进程镜像路径为准，二者皆为候选）。
_YH_RO_TX_ROOT_DEFAULT = Path(r"D:\同花顺远航\同花顺远航版")
_YH_RO_OWNER_IDLE_MS = 90_000     # [同源] YH_OWNER_ACTIVE_IDLE_S*1000
_YH_RO_QUERY_PAGES = ("资金股票", "历史成交")
_YH_RO_TREE_AID = "129"
_YH_RO_TREE_CLS = "SysTreeView32"
_YH_RO_GRID_AID = "1047"
_YH_RO_GRID_CLS = "CVirtualGridCtrl"
_YH_RO_IDENTITY_AIDS = ("2322", "1003", "1004")
_YH_RO_IDENTITY_PARENT_AID = "59392"
_YH_RO_HISTORY_BUTTON_AIDS = ("5308", "5309", "5310", "5311", "5317")
_YH_RO_QUERY_DONE_AID = "65026"
_YH_RO_QUERY_DONE_TEXT = "查询完毕"
_YH_RO_FUND_WORDS = (
    "资金余额", "可用金额", "可用资金", "可取金额", "可取资金", "股票市值",
    "证券市值", "参考市值", "总资产", "资产总值", "总市值", "资金资产",
    "冻结金额", "币种",
)
_YH_RO_CREDENTIAL_AIDS = ("1001", "1012")   # 登录页资金账号/密码控件，绝不读
_YH_RO_DATE_VALUE_RE = re.compile(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?")
_YH_RO_QUERY_PARENT_RE = re.compile(r"查询\[F\d+\]")
_YH_RO_UIA_CT = {
    "button": 50000, "edit": 50004, "combobox": 50003, "text": 50020,
    "tree": 50023, "treeitem": 50024, "toolbar": 50021, "pane": 50033,
    "window": 50032,
}
_YH_RO_UIA_VALUE_PATTERN = 10002
_YH_RO_UIA_SCROLL_PATTERN = 10004
_YH_RO_UIA_SELECTION_ITEM_PATTERN = 10010
_YH_RO_UIA_CACHE = None
_YH_RO_OCR_ENGINE = None


class _YhReadonlyLockGuard:
    """会话级命名互斥锁（Win32 mutex；Local\\ 命名空间=本会话）。

    互斥域说明（如实、不夸称全局）：只约束本入口（read-account）自身的
    并发重入；不覆盖旧 runtime 文件锁 yuanhang_window.lock，也不与
    services 桥接锁互斥——旧域并轨须另行任务。WAIT_ABANDONED 表示上一
    持有者异常退出、所有权到手：本实现立即释放并拒绝（返回 False），
    绝不当作成功；WAIT_TIMEOUT 同样 False；句柄/等待失败抛
    ThsXiadanError（调用方 fail-closed）。__exit__ 永不吞异常。
    """

    def __init__(self, *, timeout_seconds: float):
        try:
            self._timeout_ms = max(0, int(float(timeout_seconds) * 1000.0))
        except (TypeError, ValueError):
            self._timeout_ms = 0
        self._handle = None
        self._acquired = False
        self.refused_reason: Optional[str] = None

    def _kernel(self):
        if not IS_WINDOWS:
            raise ThsXiadanError("readonly lock requires Windows")
        return ctypes.windll.kernel32

    def __enter__(self) -> bool:
        kernel = self._kernel()
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                        wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel.ReleaseMutex.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.CreateMutexW(None, False, _YH_RO_MUTEX_NAME)
        if not handle or handle == INVALID_HANDLE_VALUE:
            self.refused_reason = "create_failed"
            raise ThsXiadanError("readonly mutex unavailable")
        self._handle = handle
        try:
            result = int(kernel.WaitForSingleObject(handle, self._timeout_ms))
        except Exception:
            self._release_handle()
            raise
        if result == _YH_RO_WAIT_OBJECT_0:
            self._acquired = True
            return True
        if result == _YH_RO_WAIT_ABANDONED:
            kernel.ReleaseMutex(handle)          # 到手也先释放
            self._release_handle()
            self.refused_reason = "abandoned"
            return False
        if result == _YH_RO_WAIT_TIMEOUT:
            self._release_handle()
            self.refused_reason = "busy"
            return False
        self._release_handle()
        self.refused_reason = "wait_failed"
        return False

    def _release_handle(self) -> None:
        if self._handle:
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None

    def __exit__(self, exc_type, exc_value, tb) -> bool:
        try:
            if self._acquired and self._handle:
                ctypes.windll.kernel32.ReleaseMutex(self._handle)
        finally:
            self._release_handle()
            self._acquired = False
        return False


def _yh_readonly_lock(*, timeout_seconds: float = 10.0):
    """read-account 会话级命名互斥锁（contextmanager 形状；见 Guard 类）。"""
    return _YhReadonlyLockGuard(timeout_seconds=timeout_seconds)


# ── _yh_ro_* 只读助手：进程链 / 桌面 / 输入 / 窗口（全部动态取证） ──

def _yh_ro_norm_path(value) -> str:
    return str(value).replace("\\", "/").casefold()


def _yh_ro_session_id(pid: int) -> int:
    kernel = ctypes.windll.kernel32
    kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD)]
    kernel.ProcessIdToSessionId.restype = wintypes.BOOL
    sid = wintypes.DWORD()
    if not kernel.ProcessIdToSessionId(int(pid), ctypes.byref(sid)):
        raise ThsXiadanError("ProcessIdToSessionId failed")
    return int(sid.value)


def _yh_ro_process_image(pid: int) -> str:
    """进程镜像完整路径（QueryFullProcessImageNameW；失败 fail-closed）。"""
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED
    if not handle:
        raise ThsXiadanError("process path access failed")
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buf,
                                                 ctypes.byref(size)):
            raise ThsXiadanError("process path read failed")
        return _yh_ro_norm_path(buf.value)
    finally:
        kernel.CloseHandle(handle)


def _yh_ro_chain_snapshot(happ_exe) -> dict[str, Any]:
    """happ→xiadan→xiadan-plus 实链动态校验（镜像路径/父子/session）。

    happ 期望路径候选 = happ_exe 参数 + 探针证实的 同花顺远航版 安装根
    （YUANHANG_EXE_DEFAULT 是旧启动常量，实际以进程镜像为准）；xiadan/
    xiadan-plus 必须位于 happ 镜像根的 transaction/ 子树，且
    xiadan.ppid==happ.pid、plus.ppid==xiadan.pid，三者 session 与本进程
    一致。任一不满足 → ThsXiadanError（fail-closed，绝不猜测归属）。
    """
    records: dict[int, dict[str, Any]] = {}
    if KERNEL32 is None:
        raise ThsXiadanError("process snapshot requires Windows")
    snap = KERNEL32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        raise ThsXiadanError("CreateToolhelp32Snapshot failed")
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        has = KERNEL32.Process32FirstW(snap, ctypes.byref(entry))
        while has:
            records[int(entry.th32ProcessID)] = {
                "pid": int(entry.th32ProcessID),
                "ppid": int(entry.th32ParentProcessID),
                "name": str(entry.szExeFile),
            }
            has = KERNEL32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        KERNEL32.CloseHandle(snap)

    def pick_unique(name_lower: str, expected_images: list[str]):
        matches: list[tuple[dict[str, Any], str]] = []
        for rec in records.values():
            if rec["name"].lower() != name_lower:
                continue
            try:
                image = _yh_ro_process_image(rec["pid"])
            except ThsXiadanError:
                continue
            if image in expected_images:
                matches.append((rec, image))
        if len(matches) != 1:
            raise ThsXiadanError(
                "process chain not unique on expected path: " + name_lower)
        return matches[0]

    happ_candidates = [_yh_ro_norm_path(Path(str(happ_exe)))]
    alt_happ = _yh_ro_norm_path(_YH_RO_TX_ROOT_DEFAULT / "bin" / "happ.exe")
    if alt_happ not in happ_candidates:
        happ_candidates.append(alt_happ)
    happ, happ_image = pick_unique("happ.exe", happ_candidates)
    if "/bin/" not in happ_image:
        raise ThsXiadanError("happ image path shape unexpected")
    root = happ_image.rsplit("/bin/", 1)[0]
    xiadan, _ = pick_unique("xiadan.exe", [root + "/transaction/xiadan.exe"])
    plus, _ = pick_unique(
        "xiadan-plus.exe",
        [root + "/transaction/xiadan-plus/xiadan-plus.exe"])
    if xiadan["ppid"] != happ["pid"] or plus["ppid"] != xiadan["pid"]:
        raise ThsXiadanError("process parent/child relationship changed")
    our_sid = _yh_ro_session_id(os.getpid())
    chain = {
        "session_id": our_sid,
        "happ": {"pid": happ["pid"], "ppid": happ["ppid"], "image": happ_image},
        "xiadan": {"pid": xiadan["pid"], "ppid": xiadan["ppid"],
                   "image": root + "/transaction/xiadan.exe"},
        "plus": {"pid": plus["pid"], "ppid": plus["ppid"],
                 "image": root + "/transaction/xiadan-plus/xiadan-plus.exe"},
    }
    for name in ("happ", "xiadan", "plus"):
        if _yh_ro_session_id(chain[name]["pid"]) != our_sid:
            raise ThsXiadanError("process chain session mismatch: " + name)
    return chain


def _yh_ro_chain_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    for name in ("happ", "xiadan", "plus"):
        a, b = left.get(name), right.get(name)
        if not isinstance(a, dict) or not isinstance(b, dict):
            return False
        for key in ("pid", "ppid", "image"):
            if a.get(key) != b.get(key):
                return False
    return left.get("session_id") == right.get("session_id")


def _yh_ro_session_state(session_id: int) -> str:
    """本机指定 session 的 WTS 连接态；查询/解析失败一律拒绝。"""
    wts = ctypes.windll.wtsapi32
    wts.WTSQuerySessionInformationW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    wts.WTSQuerySessionInformationW.restype = wintypes.BOOL
    wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    wts.WTSFreeMemory.restype = None
    buffer = ctypes.c_void_p()
    length = wintypes.DWORD()
    states = ("WTSActive", "WTSConnected", "WTSConnectQuery", "WTSShadow",
              "WTSDisconnected", "WTSIdle", "WTSListen", "WTSReset",
              "WTSDown", "WTSInit")
    try:
        if not wts.WTSQuerySessionInformationW(
                None, int(session_id), 8, ctypes.byref(buffer),
                ctypes.byref(length)):
            raise ThsXiadanError("WTSQuerySessionInformationW failed")
        if not buffer.value:
            raise ThsXiadanError("WTS session state buffer unavailable")
        if int(length.value) < ctypes.sizeof(ctypes.c_int):
            raise ThsXiadanError("WTS session state buffer too short")
        state = ctypes.cast(
            buffer, ctypes.POINTER(ctypes.c_int)).contents.value
        if state < 0 or state >= len(states):
            raise ThsXiadanError("WTS session state value invalid")
        return states[state]
    finally:
        if buffer.value:
            wts.WTSFreeMemory(buffer)


def _yh_ro_desktop_state() -> dict[str, Any]:
    """Default 交互桌面 + 线程桌面一致 + 链 session（任一失败抛错）。"""
    user32 = ctypes.windll.user32
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                        wintypes.DWORD]
    user32.OpenInputDesktop.restype = ctypes.c_void_p
    user32.CloseDesktop.argtypes = [ctypes.c_void_p]
    user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user32.GetThreadDesktop.restype = ctypes.c_void_p
    user32.GetUserObjectInformationW.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    user32.GetUserObjectInformationW.restype = wintypes.BOOL

    def desktop_name(handle) -> str:
        buf = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        if not user32.GetUserObjectInformationW(
                handle, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            raise ThsXiadanError("desktop name unavailable")
        return str(buf.value)

    handle = user32.OpenInputDesktop(0, False, 1)   # GENERIC_READ
    if not handle:
        raise ThsXiadanError("input desktop unavailable")
    try:
        input_name = desktop_name(handle)
        thread_name = desktop_name(
            user32.GetThreadDesktop(ctypes.windll.kernel32.GetCurrentThreadId()))
    finally:
        user32.CloseDesktop(handle)
    pid = os.getpid()
    session_id = _yh_ro_session_id(pid)
    return {"input_desktop": input_name, "thread_desktop": thread_name,
            "session_id": session_id,
            "session_state": _yh_ro_session_state(session_id)}


def _yh_ro_input_state() -> dict[str, Any]:
    """GetLastInputInfo + GetTickCount64 → 输入空闲/节拍/前台窗。

    仅本 session 的输入；时钟回绕/异常一律抛错（绝不当 0 放行）。
    """
    if USER32 is None:
        raise ThsXiadanError("input state requires Windows")

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    user32 = ctypes.windll.user32
    kernel = ctypes.windll.kernel32
    kernel.GetTickCount64.restype = ctypes.c_ulonglong
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        raise ThsXiadanError("GetLastInputInfo failed")
    tick64 = int(kernel.GetTickCount64())
    last = int(lii.dwTime)
    delta = ((tick64 & 0xFFFFFFFF) - last) & 0xFFFFFFFF
    if delta >= 0x80000000 or last <= 0:
        raise ThsXiadanError("last-input clock anomaly")
    foreground = int(user32.GetForegroundWindow())
    if not foreground:
        raise ThsXiadanError("foreground window unavailable")
    return {"idle_ms": int(delta), "last_input_tick": last,
            "foreground_hwnd": foreground}


def _yh_ro_visible_windows(chain: dict[str, Any]) -> set[int]:
    """枚举交易面板进程（xiadan/plus）的有效可见顶层窗（排除 happ 行情主窗与无标题微型状态条）。"""
    pids = {chain[name]["pid"] for name in ("xiadan", "plus")}
    out: set[int] = set()
    for hwnd in _top_level_windows():
        if _process_id(hwnd) not in pids or not _is_visible(hwnd):
            continue
        title = _window_text(hwnd).strip()
        left, top, right, bottom = _rect(hwnd)
        width, height = right - left, bottom - top
        # 过滤 xiadan 自带的无标题微型状态条（如 298x29），保留所有具名窗或正常尺寸弹窗
        if not title and (width <= 300 or height < 60):
            continue
        out.add(int(hwnd))
    return out


def _yh_ro_window_state(hwnd: int, owner_pid: int) -> bool:
    """查询窗归属/可见/启用/非最小化复合校验（Win32 只读）。"""
    if USER32 is None:
        return False
    hwnd = int(hwnd)
    return bool(
        USER32.IsWindow(hwnd)
        and _is_visible(hwnd)
        and _process_id(hwnd) == int(owner_pid)
        and USER32.IsWindowEnabled(hwnd)
        and not USER32.IsIconic(hwnd))


# ── _yh_ro_* UIA 只读助手（comtypes；只 GetCurrent*/FindAll/Select 项） ──

def _yh_ro_uia():
    global _YH_RO_UIA_CACHE
    from comtypes.client import CreateObject, GetModule

    GetModule("UIAutomationCore.dll")
    import comtypes.gen.UIAutomationClient as UIAC

    if _YH_RO_UIA_CACHE is None:
        _YH_RO_UIA_CACHE = (
            UIAC, CreateObject(UIAC.CUIAutomation,
                               interface=UIAC.IUIAutomation))
    return _YH_RO_UIA_CACHE


def _yh_ro_uia_scan(hwnd: int, *, limit: int = 4000):
    """一次 Descendants FindAll → (描述列表, 对齐的 COM 元素列表)。只读。

    文本先验门禁：每元素先读 ControlType/IsPassword/IsEnabled/IsOffscreen
    安全元数据，之后仅当 非密码、非 Edit、启用、非离屏 且类型属于
    Name 白名单——查询树导航 TreeItem、资金标签 Text，或历史控件/查询
    状态既有 autoid（_YH_RO_HISTORY_BUTTON_AIDS/_YH_RO_QUERY_DONE_AID）
    的 Button——才读 CurrentName；被禁项与未确认类型 name 置空且绝不
    访问其 CurrentName。条目与 elements 保持一一对齐。安全元数据读取
    异常自然传播（仅矩形保留既有降级）；本函数绝不读任何 Value。
    """
    UIAC, uia = _yh_ro_uia()
    root = uia.ElementFromHandle(int(hwnd))
    found = root.FindAll(4, uia.CreateTrueCondition())
    items: list[dict[str, Any]] = []
    elements: list[Any] = []
    total = int(found.Length)
    for i in range(min(total, limit)):
        el = found.GetElement(i)
        ct = int(el.CurrentControlType)
        pwd = bool(el.CurrentIsPassword)
        enabled = bool(el.CurrentIsEnabled)
        offscreen = bool(el.CurrentIsOffscreen)
        try:
            rect = el.CurrentBoundingRectangle
            box = [int(rect.left), int(rect.top),
                   int(rect.right), int(rect.bottom)]
        except Exception:  # noqa: BLE001 - 描述读取失败按空矩形处理
            box = [0, 0, 0, 0]
        cls = str(el.CurrentClassName or "")
        aid = str(el.CurrentAutomationId or "")
        gate = (not pwd and enabled and not offscreen
                and ct != _YH_RO_UIA_CT["edit"])
        name = ""
        if gate and ct in (_YH_RO_UIA_CT["treeitem"],
                           _YH_RO_UIA_CT["text"]):
            name = str(el.CurrentName or "")
        elif gate and ct == _YH_RO_UIA_CT["button"] and (
                aid in _YH_RO_HISTORY_BUTTON_AIDS
                or aid == _YH_RO_QUERY_DONE_AID):
            name = str(el.CurrentName or "")
        items.append({
            "ct": ct,
            "cls": cls,
            "aid": aid,
            "name": name,
            "rect": box,
            "pwd": pwd,
            "offscreen": offscreen,
            "enabled": enabled,
            "win": int(el.CurrentNativeWindowHandle or 0),
        })
        elements.append(el)
    return UIAC, uia, root, items, elements


def _yh_ro_item_visible(item: dict[str, Any]) -> bool:
    rect = item.get("rect") or [0, 0, 0, 0]
    return (not item.get("offscreen")) and rect[2] > rect[0] and rect[3] > rect[1]


def _yh_ro_parents(uia, el, depth: int = 2) -> list[Any]:
    walker = uia.ControlViewWalker
    out: list[Any] = []
    cur = el
    for _ in range(depth):
        cur = walker.GetParentElement(cur)
        if cur is None:
            break
        out.append(cur)
    return out


def _yh_ro_query_item_valid(uia, item: dict[str, Any], el) -> bool:
    """查询 TreeItem 锚定：父 TreeItem 名 查询[F*]，祖父 SysTreeView32/129。"""
    if item["ct"] != _YH_RO_UIA_CT["treeitem"] or not _yh_ro_item_visible(item):
        return False
    parents = _yh_ro_parents(uia, el, 2)
    if len(parents) < 2:
        return False
    p1, p2 = parents[0], parents[1]
    try:
        if int(p1.CurrentControlType) != _YH_RO_UIA_CT["treeitem"]:
            return False
        if not _YH_RO_QUERY_PARENT_RE.fullmatch(str(p1.CurrentName or "")):
            return False
        if int(p2.CurrentControlType) != _YH_RO_UIA_CT["tree"]:
            return False
        if str(p2.CurrentClassName or "") != _YH_RO_TREE_CLS:
            return False
        if str(p2.CurrentAutomationId or "") != _YH_RO_TREE_AID:
            return False
    except Exception:  # noqa: BLE001 - UIA 易失节点按失配处理
        return False
    return True


def _yh_ro_query_anchor(hwnd: int) -> Optional[dict[str, Any]]:
    """查询树锚探测：两查询叶均唯一锚定 → 锚描述；否则 None。"""
    UIAC, uia, _root, items, elements = _yh_ro_uia_scan(hwnd)
    targets: dict[str, dict[str, Any]] = {}
    for item, el in zip(items, elements):
        name = item["name"]
        if name not in _YH_RO_QUERY_PAGES or name in targets:
            continue
        if _yh_ro_query_item_valid(uia, item, el):
            targets[name] = item
    if set(targets) != set(_YH_RO_QUERY_PAGES):
        return None
    return {"targets": sorted(targets), "tree_aid": _YH_RO_TREE_AID}


def _yh_ro_find_query_window(xiadan_pid: int) -> tuple[int, list[int]]:
    """xiadan.exe 顶层窗中定位查询树锚定的唯一面板（可见+启用+非最小化）。"""
    if USER32 is None:
        raise ThsXiadanError("query window discovery requires Windows")
    anchored: list[tuple[int, list[int]]] = []
    for hwnd in _top_level_windows():
        if _process_id(hwnd) != int(xiadan_pid) or not _is_visible(hwnd):
            continue
        if not USER32.IsWindowEnabled(int(hwnd)) or USER32.IsIconic(int(hwnd)):
            continue
        if _yh_ro_query_anchor(int(hwnd)):
            anchored.append((int(hwnd), list(_rect(int(hwnd)))))
    if len(anchored) != 1:
        raise ThsXiadanError(
            "readonly query panel not uniquely bound (open 资金股票/历史成交 "
            "query panel first; found=" + str(len(anchored)) + ")")
    return anchored[0]


def _yh_ro_identity_values(hwnd: int) -> Optional[dict[str, str]]:
    """身份三锚 Value 读取：ComboBox 2322/1003/1004（class=ComboBox、非
    password、父 ToolBar autoid 59392）。任何锚缺失/非法/读到空 → None。
    原值仅内存返回，绝不写入报告；绝不触碰 Edit/密码控件。"""
    UIAC, uia, _root, items, elements = _yh_ro_uia_scan(hwnd)
    found: dict[str, tuple[dict[str, Any], Any]] = {}
    for item, el in zip(items, elements):
        aid = item["aid"]
        if (item["ct"] != _YH_RO_UIA_CT["combobox"]
                or aid not in _YH_RO_IDENTITY_AIDS
                or item["cls"] != "ComboBox"
                or item["pwd"] or not _yh_ro_item_visible(item)):
            continue
        if aid in found:
            return None                       # 锚不唯一 → fail-closed
        found[aid] = (item, el)
    if set(found) != set(_YH_RO_IDENTITY_AIDS):
        return None
    values: dict[str, str] = {}
    for aid, (_item, el) in found.items():
        parents = _yh_ro_parents(uia, el, 1)
        if not parents:
            return None
        parent = parents[0]
        try:
            if int(parent.CurrentControlType) != _YH_RO_UIA_CT["toolbar"]:
                return None
            if str(parent.CurrentAutomationId or "") != \
                    _YH_RO_IDENTITY_PARENT_AID:
                return None
            pattern = el.GetCurrentPattern(UIAC.UIA_ValuePatternId)
            if pattern is None:
                return None
            value = str(pattern.QueryInterface(
                UIAC.IUIAutomationValuePattern).CurrentValue or "").strip()
        except Exception:  # noqa: BLE001 - 任一锚读取失败整体不可读
            return None
        if not value:
            return None
        values[aid] = value
    return values


def _yh_ro_resolve_account(label_text: Optional[str]) -> tuple[Optional[str],
                                                                Optional[str],
                                                                dict[str, bool]]:
    """多锚归属（绝不单凭姓）：脱敏签名锚 + 注册表 label 锚 + 券商前缀/
    姓氏确认锚；锚冲突或确认失败 → (None, None, ...)。"""
    text = str(label_text or "")
    flat = re.sub(r"[－—\s]+", "-", text)
    masked = yh_normalize_masked(text)
    by_masked = yh_account_from_masked(masked)
    by_label: Optional[str] = None
    for key, meta in YH_ACCOUNTS.items():
        label_flat = re.sub(r"[－—\s]+", "-", str(meta.get("label") or ""))
        if label_flat and label_flat == flat:
            by_label = key
            break
    if by_masked and by_label and by_masked != by_label:
        return None, None, {"masked_signature": True, "label": True,
                            "consistent": False}
    account = by_masked or by_label
    if account is None:
        return None, None, {"masked_signature": False, "label": False,
                            "consistent": False}
    meta = YH_ACCOUNTS[account]
    broker_prefix = re.sub(r"[－—\s]+", "-",
                           str(meta.get("label") or ""))[:5]
    confirmed = broker_prefix in flat and str(meta.get("owner") or "") in text
    if not confirmed:
        return None, None, {"masked_signature": by_masked is not None,
                            "label": by_label is not None,
                            "consistent": False}
    return account, masked, {"masked_signature": by_masked is not None,
                             "label": by_label is not None,
                             "consistent": True}


def _yh_ro_identity_fingerprint(values: dict[str, str]) -> str:
    """身份指纹（内存原值的 sha256 前 16 位；不输出原值/全账号）。"""
    import hashlib

    joined = "|".join(str(values.get(aid) or "")
                      for aid in _YH_RO_IDENTITY_AIDS)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _yh_ro_page_state(hwnd: int) -> dict[str, Any]:
    """查询面板页面证据（纯读）：选中查询叶 + 网格 + 资金标签/历史控件锚。"""
    UIAC, uia, _root, items, elements = _yh_ro_uia_scan(hwnd)
    selected: Optional[str] = None
    selected_count = 0
    for item, el in zip(items, elements):
        if item["name"] not in _YH_RO_QUERY_PAGES:
            continue
        if not _yh_ro_query_item_valid(uia, item, el):
            continue
        try:
            pattern = el.GetCurrentPattern(UIAC.UIA_SelectionItemPatternId)
            if pattern is None:
                continue
            sel = pattern.QueryInterface(
                UIAC.IUIAutomationSelectionItemPattern)
            if bool(sel.CurrentIsSelected):
                selected = item["name"]
                selected_count += 1
        except Exception:  # noqa: BLE001 - 易失节点跳过
            continue
    if selected_count != 1:
        selected = None
    grids = [(item, el) for item, el in zip(items, elements)
             if item["ct"] == _YH_RO_UIA_CT["pane"]
             and item["cls"] == _YH_RO_GRID_CLS
             and item["aid"] == _YH_RO_GRID_AID
             and _yh_ro_item_visible(item)]
    grid: Optional[dict[str, Any]] = None
    scroll: Optional[dict[str, Any]] = None
    if len(grids) == 1:
        item, el = grids[0]
        grid = {"hwnd": item["win"], "rect": list(item["rect"])}
        try:
            pattern = el.GetCurrentPattern(UIAC.UIA_ScrollPatternId)
            if pattern is not None:
                sp = pattern.QueryInterface(UIAC.IUIAutomationScrollPattern)
                scroll = {
                    "vert_scrollable": bool(sp.CurrentVerticallyScrollable),
                    "vert_view_size": float(sp.CurrentVerticalViewSize),
                    "horz_scrollable": bool(sp.CurrentHorizontallyScrollable),
                    "horz_view_size": float(sp.CurrentHorizontalViewSize),
                }
        except Exception:  # noqa: BLE001 - Scroll 缺席 → 覆盖证据未知
            scroll = None
    texts = {re.sub(r"\s+", "", item["name"]).rstrip(":：")
             for item in items
             if item["ct"] == _YH_RO_UIA_CT["text"]
             and _yh_ro_item_visible(item) and item["name"]}
    fund_labels = sorted(texts & set(_YH_RO_FUND_WORDS))
    buttons = {item["aid"]: item["name"] for item, _el in zip(items, elements)
               if item["ct"] == _YH_RO_UIA_CT["button"]
               and _yh_ro_item_visible(item)}
    history_controls = [{"autoid": aid, "name": buttons.get(aid, "")}
                        for aid in _YH_RO_HISTORY_BUTTON_AIDS
                        if aid in buttons]
    query_status = buttons.get(_YH_RO_QUERY_DONE_AID)
    history_ok = (len(history_controls) == len(_YH_RO_HISTORY_BUTTON_AIDS)
                  and query_status == _YH_RO_QUERY_DONE_TEXT and grid)
    funds_ok = (len(fund_labels) >= 3 and grid and not history_controls)
    if selected == "历史成交":
        marker = bool(history_ok)
    elif selected == "资金股票":
        marker = bool(funds_ok)
    else:
        marker = False
    return {
        "selected_page": selected,
        "grid": grid,
        "scroll": scroll,
        "fund_labels": fund_labels,
        "history_controls": history_controls,
        "query_status": query_status if selected == "历史成交" else None,
        "page_marker_confirmed": marker,
    }


def _yh_ro_select_query_item(hwnd: int, page: str) -> dict[str, Any]:
    """唯一许可的状态变更调用：查询 TreeItem SelectionItem.Select()。
    无键鼠/焦点/F12 兜底；不支持/拒绝 → ok=False（调用方 fail-closed）。"""
    UIAC, uia, _root, items, elements = _yh_ro_uia_scan(hwnd)
    for item, el in zip(items, elements):
        if item["name"] != page or not _yh_ro_query_item_valid(uia, item, el):
            continue
        if not item["enabled"]:
            return {"ok": False, "reason": "query_item_disabled"}
        try:
            pattern = el.GetCurrentPattern(UIAC.UIA_SelectionItemPatternId)
            if pattern is None:
                return {"ok": False, "reason": "selection_pattern_unsupported"}
            sel = pattern.QueryInterface(
                UIAC.IUIAutomationSelectionItemPattern)
            sel.Select()
        except ThsXiadanError:
            raise
        except Exception as exc:  # noqa: BLE001 - 拒绝即拒绝，绝不兜底
            return {"ok": False,
                    "reason": "selection_refused:" + type(exc).__name__}
        return {"ok": True, "reason": None}
    return {"ok": False, "reason": "query_item_not_found"}


def _yh_ro_child_dialogs(hwnd: int) -> list[dict[str, Any]]:
    """查询窗内可见子 Window 对话框（未知模态拒绝用；仅计数入报告）。
    属性读取异常必须传播：静默 continue 会隐藏未知模态（fail-closed）。"""
    UIAC, uia = _yh_ro_uia()
    root = uia.ElementFromHandle(int(hwnd))
    cond = uia.CreatePropertyCondition(
        UIAC.UIA_ControlTypePropertyId, _YH_RO_UIA_CT["window"])
    found = root.FindAll(4, cond)
    dialogs: list[dict[str, Any]] = []
    for i in range(int(found.Length)):
        el = found.GetElement(i)
        if bool(el.CurrentIsOffscreen):
            continue
        dialogs.append({"cls": str(el.CurrentClassName or ""),
                        "aid": str(el.CurrentAutomationId or "")})
    return dialogs


# ── _yh_ro_* 截图/OCR（PrintWindow 仅内存 + 本地 RapidOCR 显式模型） ──

def _yh_ro_print_window_pixels(hwnd: int):
    """PrintWindow(PW_RENDERFULLCONTENT) → 内存 numpy 像素（绝不落盘）。"""
    import numpy as np
    import win32gui
    import win32ui

    user32 = ctypes.windll.user32
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    prior = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    hdc = source = mem = bitmap = old = None
    try:
        rect = win32gui.GetWindowRect(int(hwnd))
        width, height = int(rect[2] - rect[0]), int(rect[3] - rect[1])
        if width <= 0 or height <= 0:
            raise ThsXiadanError("empty capture rect")
        hdc = win32gui.GetWindowDC(int(hwnd))
        source = win32ui.CreateDCFromHandle(hdc)
        mem = source.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source, width, height)
        old = mem.SelectObject(bitmap)
        if not user32.PrintWindow(int(hwnd), mem.GetSafeHdc(), 2):
            raise ThsXiadanError("PrintWindow failed")
        bits = bitmap.GetBitmapBits(True)
        pixels = np.frombuffer(bits, dtype=np.uint8).reshape(
            height, width, 4)[:, :, :3].copy()
        meta = {"method": "PrintWindow/PW_RENDERFULLCONTENT",
                "width": width, "height": height,
                "rect": [int(v) for v in rect]}
        return pixels, meta
    finally:
        if mem is not None and old is not None:
            mem.SelectObject(old)
        if bitmap is not None:
            win32gui.DeleteObject(bitmap.GetHandle())
        if mem is not None:
            mem.DeleteDC()
        if source is not None:
            source.DeleteDC()
        if hdc is not None:
            win32gui.ReleaseDC(int(hwnd), hdc)
        if prior:
            user32.SetThreadDpiAwarenessContext(prior)


def _yh_ro_ocr_engine():
    """本地 RapidOCR（显式安装内模型 ch_PP-OCRv4 det/rec + cls；绝不下载、
    绝不外发；模型缺失 → ThsXiadanError，调用方如实 ocr_unavailable）。"""
    global _YH_RO_OCR_ENGINE
    import rapidocr_onnxruntime as pkg
    from rapidocr_onnxruntime import RapidOCR

    module_file = str(getattr(pkg, "__file__", "") or "")
    if not module_file:
        raise ThsXiadanError("rapidocr package location unknown")
    models = Path(module_file).parent / "models"
    names = {"det": "ch_PP-OCRv4_det_infer.onnx",
             "rec": "ch_PP-OCRv4_rec_infer.onnx",
             "cls": "ch_ppocr_mobile_v2.0_cls_infer.onnx"}
    paths = {kind: models / name for kind, name in names.items()}
    if not all(p.is_file() for p in paths.values()):
        raise ThsXiadanError("local OCR models missing")
    meta = {"engine": "rapidocr_onnxruntime",
            "models": sorted(p.name for p in paths.values())}
    if _YH_RO_OCR_ENGINE is None:
        _YH_RO_OCR_ENGINE = RapidOCR(
            det_model_path=str(paths["det"]),
            rec_model_path=str(paths["rec"]),
            cls_model_path=str(paths["cls"]),
            use_cuda=False, use_dml=False,
            intra_op_num_threads=2, inter_op_num_threads=1)
    return _YH_RO_OCR_ENGINE, meta


def _yh_ro_grid_items(grid_hwnd: int):
    """网格内存截图 → 本地 OCR → 网格相对坐标文本项 + 行分组容差 + 元数据。"""
    import cv2
    import numpy as np

    pixels, capture = _yh_ro_print_window_pixels(grid_hwnd)
    engine, engine_meta = _yh_ro_ocr_engine()
    scale = 2.0
    image = cv2.resize(pixels, None, fx=scale, fy=scale,
                       interpolation=cv2.INTER_CUBIC)
    raw, _elapsed = engine(image, use_cls=False)
    items: list[dict[str, Any]] = []
    heights: list[float] = []
    for box, text, score in (raw or []):
        pts = np.asarray(box, dtype=float) / scale
        left, top = float(pts[:, 0].min()), float(pts[:, 1].min())
        right, bottom = float(pts[:, 0].max()), float(pts[:, 1].max())
        items.append({"text": str(text), "x": (left + right) / 2.0,
                      "y": (top + bottom) / 2.0, "left": left,
                      "right": right, "top": top, "bottom": bottom,
                      "score": float(score)})
        heights.append(max(bottom - top, 0.0))
    heights.sort()
    median_h = heights[len(heights) // 2] if heights else 0.0
    row_delta = max(3.0, median_h * 0.45)
    meta = {"capture": capture, "ocr": engine_meta, "scale": scale,
            "box_count": len(items), "row_delta": round(float(row_delta), 2),
            "min_confidence": min((it["score"] for it in items), default=None)}
    return items, row_delta, meta


def _yh_ro_header_band(items: list[dict[str, Any]], aliases: dict[str, str],
                       row_delta: float) -> list[str]:
    """真实表头取证：表头带（≥2 个已知列名命中的最上分组）→ 规范键 x 序。"""
    canon_of: dict[str, str] = {}
    for raw_key, canon in aliases.items():
        canon_of.setdefault(_yh_norm_colkey(raw_key), canon)
    for group in yh_group_rows(items, row_delta=row_delta):
        hits: list[tuple[float, str]] = []
        seen: set[str] = set()
        for cell in sorted(group, key=lambda o: float(o.get("x") or 0.0)):
            token = str(cell.get("text") or "").strip()
            canon = aliases.get(token) or canon_of.get(_yh_norm_colkey(token))
            if canon and canon not in seen:
                seen.add(canon)
                hits.append((float(cell.get("x") or 0.0), canon))
        if len(hits) >= 2:
            hits.sort()
            return [canon for _x, canon in hits]
    return []


def _yh_ro_bound_date_range(bound_values):
    """纯解析已绑定角色的日期控件读回值：仅接受明确 start/end 各一个。

    反向（end < start）、多组/重复/未知角色或非法日期一律返回 (None, None)，
    绝不排序修补或推算。同日单日（start == end）合法。
    """
    if bound_values is None:
        return None, None
    parsed = {}
    for pair in bound_values:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            return None, None
        role, raw = pair[0], pair[1]
        if role not in ("start", "end") or role in parsed:
            return None, None
        text = str(raw or "").strip()
        match = _YH_RO_DATE_VALUE_RE.fullmatch(text)
        if match is None:
            return None, None
        try:
            parsed[role] = date(int(match.group(1)), int(match.group(2)),
                                int(match.group(3)))
        except ValueError:
            return None, None
    if "start" not in parsed or "end" not in parsed:
        return None, None
    if parsed["end"] < parsed["start"]:
        return None, None
    return (
        {"start": parsed["start"].isoformat(),
         "end": parsed["end"].isoformat()},
        "query_controls_readback",
    )


def _yh_ro_query_date_range(hwnd: int):
    """查询日期控件读回：仅从已验证角色绑定的控件读取；未绑定角色控件
    绝不读取 Value，反向/歧义一律返回 (None, None)。"""
    if USER32 is None:
        raise ThsXiadanError("date controls require Windows")
    # 当前面板尚未建立 start/end 控件自动化角色绑定，按 fail-closed 契约
    # 不对未绑定 Edit/ComboBox/SysDateTimePick32 控件读取 Value。
    _ = int(hwnd)
    return None, None


# ── 真实六方法 adapter（绑定即校验；绝不启动/恢复/切号/碰订单页） ──

class _YhReadonlyAdapter:
    """远航版单账号只读 adapter（六方法协议；2026-11-15 只读入口）。

    绑定条件全部满足否则构造失败（fail-closed）：happ→xiadan→xiadan-plus
    进程链路径/父子/session 动态校验；Default 交互桌面；输入空闲 ≥90s；
    xiadan.exe 唯一可见、启用、非最小化且查询树锚定的面板；当前选中页 ∈
    {资金股票, 历史成交}（买卖/委托页一律不绑定）；身份三锚可读。
    基线（进程链/可见窗集合/输入节拍/前台窗/身份原值）仅内存保存。
    """

    def __init__(self, *, happ_exe=YUANHANG_EXE_DEFAULT):
        self._happ_exe = happ_exe
        self._chain = _yh_ro_chain_snapshot(happ_exe)
        desktop = _yh_ro_desktop_state()
        if (desktop.get("input_desktop") != "Default"
                or desktop.get("thread_desktop") != "Default"
                or desktop.get("session_id") != self._chain["session_id"]
                or desktop.get("session_state") != "WTSActive"):
            raise ThsXiadanError(
                "readonly adapter: interactive desktop/session not confirmed")
        activity = _yh_ro_input_state()
        if activity["idle_ms"] < _YH_RO_OWNER_IDLE_MS:
            raise ThsXiadanError(
                "readonly adapter: owner input active at bind time")
        self._initial_tick = activity["last_input_tick"]
        self._initial_foreground = activity["foreground_hwnd"]
        self._hwnd, self._panel_rect = _yh_ro_find_query_window(
            self._chain["xiadan"]["pid"])
        self._baseline_windows = _yh_ro_visible_windows(self._chain)
        if self._baseline_windows != {self._hwnd}:
            raise ThsXiadanError(
                "readonly adapter: unexpected visible window at bind time")
        page = _yh_ro_page_state(self._hwnd)
        if page["selected_page"] not in _YH_RO_QUERY_PAGES:
            raise ThsXiadanError(
                "readonly adapter: current page outside allowed query pages")
        if not page["page_marker_confirmed"]:
            raise ThsXiadanError("readonly adapter: page markers unconfirmed")
        values = _yh_ro_identity_values(self._hwnd)
        if values is None:
            raise ThsXiadanError("readonly adapter: identity anchors unreadable")
        self._identity_values = values
        self._page = page["selected_page"]

    def _guard(self) -> dict[str, Any]:
        try:
            chain = _yh_ro_chain_snapshot(self._happ_exe)
        except ThsXiadanError:
            return {"ok": False, "owner_active": False,
                    "reason": "process_chain_invalid"}
        if not _yh_ro_chain_equal(chain, self._chain):
            return {"ok": False, "owner_active": False,
                    "reason": "process_chain_changed"}
        try:
            desktop = _yh_ro_desktop_state()
        except ThsXiadanError:
            return {"ok": False, "owner_active": True,
                    "reason": "desktop_state_unavailable"}
        if (desktop.get("input_desktop") != "Default"
                or desktop.get("thread_desktop") != "Default"
                or desktop.get("session_id") != chain["session_id"]
                or desktop.get("session_state") != "WTSActive"):
            return {"ok": False, "owner_active": False,
                    "reason": "desktop_session_invalid"}
        if not _yh_ro_window_state(self._hwnd, chain["xiadan"]["pid"]):
            return {"ok": False, "owner_active": False,
                    "reason": "query_window_changed"}
        try:
            activity = _yh_ro_input_state()
        except ThsXiadanError:
            return {"ok": False, "owner_active": True,
                    "reason": "input_state_unavailable"}
        if activity["last_input_tick"] != self._initial_tick \
                or activity["idle_ms"] < _YH_RO_OWNER_IDLE_MS:
            return {"ok": False, "owner_active": True,
                    "reason": "owner_input_activity"}
        if activity["foreground_hwnd"] != self._initial_foreground:
            return {"ok": False, "owner_active": True,
                    "reason": "foreground_changed"}
        current_windows = _yh_ro_visible_windows(chain)
        if current_windows != self._baseline_windows \
                or current_windows != {self._hwnd}:
            return {"ok": False, "owner_active": False,
                    "reason": "unknown_modal_or_window_change"}
        dialogs = _yh_ro_child_dialogs(self._hwnd)
        if dialogs:
            return {"ok": False, "owner_active": False,
                    "reason": "visible_child_dialog",
                    "detail": {"dialog_count": len(dialogs)}}
        return {"ok": True, "owner_active": False, "reason": None,
                "detail": {"visible_windows": len(current_windows),
                           "dialog_count": 0}}

    def guard(self) -> dict[str, Any]:
        """独立守卫：桌面/链/窗/输入节拍/前台/未知模态（任一失败 fail-closed）。"""
        return self._guard()

    def identity(self) -> dict[str, Any]:
        """新鲜读取身份三锚（每次独立读，绝不用页面载荷携带值）；
        原值仅内存，输出账号键+指纹。不可读 → account/fingerprint=None。"""
        try:
            values = _yh_ro_identity_values(self._hwnd)
        except ThsXiadanError:
            values = None
        if values is None:
            return {"account": None, "fingerprint": None,
                    "masked_id": None, "anchors": {"read": False}}
        account, _masked, anchors = _yh_ro_resolve_account(
            values.get("2322"))
        fingerprint = _yh_ro_identity_fingerprint(values)
        masked_out = (YH_ACCOUNTS.get(account, {}).get("masked_id")
                      if account else None)
        return {"account": account,
                "fingerprint": fingerprint if account else None,
                "masked_id": masked_out, "anchors": anchors}

    def select_query(self, page: str) -> dict[str, Any]:
        """查询页导航：仅限 资金股票/历史成交，仅 SelectionItem.Select()；
        选前守卫+身份原值复核，选后重建 UIA 读回选中+页面锚独立确认；
        长确认循环前后同样守卫。无任何键鼠/焦点/F12 兜底。"""
        if page not in _YH_RO_QUERY_PAGES:
            return {"ok": False, "selected": False, "page_confirmed": False,
                    "page": page, "reason": "page_not_allowed"}
        pre = self._guard()
        if pre.get("ok") is not True or pre.get("owner_active") is True:
            return {"ok": False, "selected": False, "page_confirmed": False,
                    "page": page, "reason": pre.get("reason") or "guard_rejected"}
        try:
            values = _yh_ro_identity_values(self._hwnd)
        except ThsXiadanError:
            values = None
        if values is None or values != self._identity_values:
            return {"ok": False, "selected": False, "page_confirmed": False,
                    "page": page, "reason": "identity_changed_before_selection"}
        result = _yh_ro_select_query_item(self._hwnd, page)
        if result.get("ok") is not True:
            return {"ok": False, "selected": False, "page_confirmed": False,
                    "page": page,
                    "reason": result.get("reason") or "selection_refused"}
        deadline = time.monotonic() + 5.0
        while True:
            mid = self._guard()
            if mid.get("ok") is not True or mid.get("owner_active") is True:
                return {"ok": False, "selected": True, "page_confirmed": False,
                        "page": page,
                        "reason": mid.get("reason") or "guard_rejected"}
            state = _yh_ro_page_state(self._hwnd)
            if state["selected_page"] == page \
                    and state["page_marker_confirmed"]:
                try:
                    after = _yh_ro_identity_values(self._hwnd)
                except ThsXiadanError:
                    after = None
                if after is None or after != self._identity_values:
                    return {"ok": False, "selected": True,
                            "page_confirmed": True, "page": page,
                            "reason": "identity_changed_after_selection"}
                self._page = page
                return {"ok": True, "selected": True,
                        "page_confirmed": True, "page": page}
            if time.monotonic() >= deadline:
                return {"ok": False, "selected": True,
                        "page_confirmed": False, "page": page,
                        "reason": "selection_page_readback_failed"}
            time.sleep(0.25)

    def _require_page(self, page: str) -> dict[str, Any]:
        state = _yh_ro_page_state(self._hwnd)
        if state["selected_page"] != page or not state["page_marker_confirmed"]:
            raise ThsXiadanError("page unconfirmed at read time: " + page)
        if not state["grid"]:
            raise ThsXiadanError("query grid unavailable: " + page)
        return state

    def read_funds(self) -> dict[str, Any]:
        """资金股票页真实 UIA 标签值读取（绝不用 OCR/截图兜底资金）。"""
        state = self._require_page("资金股票")
        del state
        texts = _yh_uia_texts(self._hwnd)
        items = yh_uia_texts_to_items(texts, self._panel_rect)
        parsed = yh_parse_funds_texts(items)
        source = "uia_text"
        if not parsed["funds"] and items:
            parsed = yh_parse_funds_texts(items, region=None, strict=True)
            source = "uia_text_full"
        funds = parsed["funds"] if isinstance(parsed.get("funds"), dict) else {}
        coverage = ("balance" in funds) and ("total_assets" in funds)
        return {"funds": funds, "funds_labels": parsed.get("labels") or {},
                "as_of": None, "coverage_complete": coverage,
                "reader": source, "matched": parsed.get("matched") or 0}

    def read_holdings(self) -> dict[str, Any]:
        """持仓：网格 PrintWindow（仅内存）→ 本地 RapidOCR → 当前视口真实
        partial；无可靠横纵/分页证据不标 complete；表头取自真实表头带。"""
        state = self._require_page("资金股票")
        scroll = state.get("scroll")
        coverage = {"rows_complete": False, "columns_complete": False,
                    "pages_complete": False}
        if scroll:
            coverage["rows_complete"] = (
                not scroll.get("vert_scrollable")
                or scroll.get("vert_view_size") == 100.0)
            coverage["columns_complete"] = (
                not scroll.get("horz_scrollable")
                or scroll.get("horz_view_size") == 100.0)
        try:
            items, row_delta, meta = _yh_ro_grid_items(
                state["grid"]["hwnd"])
        except ThsXiadanError as exc:
            # 仅 OCR 能力缺失（reader 契约的固定消息）如实 partial；
            # PrintWindow/COM 等采集异常必须传播，交 collector 全链撤销。
            if str(exc) not in ("rapidocr package location unknown",
                                "local OCR models missing"):
                raise
            return {"holdings": [], "headers": [], "as_of": None,
                    "coverage_complete": False, "coverage_evidence": coverage,
                    "empty_confirmed": False, "ocr_available": False,
                    "reader": "ocr_unavailable"}
        page_tokens = yh_fills_page_evidence(items)
        # 无分页文字 ≠ 单页：当前第1页且共1页须为非 bool 整数证据。
        page_current = page_tokens.get("page_current")
        page_total = page_tokens.get("page_total")
        coverage["pages_complete"] = (
            isinstance(page_current,int) and not isinstance(page_current,bool) and page_current == 1
            and isinstance(page_total,int) and not isinstance(page_total,bool) and page_total == 1
            and not page_tokens.get("next_page_present"))
        rows = yh_holdings_from_items(items, row_delta=row_delta,
                                      min_code_x=0.0)
        headers = _yh_ro_header_band(items, YH_HOLDING_COLUMN_ALIASES,
                                     row_delta)
        count_total = page_tokens.get("count_total")
        count_matches = (isinstance(count_total, int)
                         and not isinstance(count_total, bool)
                         and count_total >= 0 and count_total == len(rows))
        headers_proven = {"code", "quantity"} <= set(headers)
        complete = (coverage["rows_complete"] and coverage["columns_complete"]
                    and coverage["pages_complete"] and headers_proven
                    and count_matches)
        empty_confirmed = complete and count_total == 0 and not rows and bool(headers)
        evidence = {"rows_complete": coverage["rows_complete"],
                    "columns_complete": coverage["columns_complete"],
                    "pages_complete": coverage["pages_complete"],
                    "viewport_rows": len(rows)}
        return {"holdings": rows, "headers": headers, "as_of": None,
                "coverage_complete": complete, "coverage_evidence": evidence,
                "empty_confirmed": empty_confirmed, "ocr_available": True,
                "reader": "rapidocr_onnxruntime", "scroll": scroll,
                "ocr_meta": meta}

    def read_fills(self) -> dict[str, Any]:
        """历史成交：网格 OCR 视口行（重复逐笔原样保留）；日期仅接受查询
        控件读回；计数/分页/汇总取页面证据；覆盖不完整如实 partial。"""
        state = self._require_page("历史成交")
        scroll = state.get("scroll")
        coverage = {"rows_complete": False, "columns_complete": False,
                    "pages_complete": False}
        if scroll:
            coverage["rows_complete"] = (
                not scroll.get("vert_scrollable")
                or scroll.get("vert_view_size") == 100.0)
            coverage["columns_complete"] = (
                not scroll.get("horz_scrollable")
                or scroll.get("horz_view_size") == 100.0)
        base = {"attempted": True, "rows": [], "header_columns": [],
                "date_range": None, "date_range_source": None,
                "coverage_complete": False, "coverage_evidence": coverage,
                "empty_confirmed": False, "row_count_read": 0,
                "count_total": None, "page_current": None, "page_total": None,
                "next_page_present": False, "summary": {"present": False},
                "observed_record_date_extent": None, "ocr_available": False,
                "reader": "ocr_unavailable",
                "wrong_page": False}
        try:
            items, row_delta, meta = _yh_ro_grid_items(
                state["grid"]["hwnd"])
        except ThsXiadanError as exc:
            # 仅 OCR 能力缺失如实 partial；采集异常必须传播（同 read_holdings）。
            if str(exc) not in ("rapidocr package location unknown",
                                "local OCR models missing"):
                raise
            return base
        date_range, date_source = _yh_ro_query_date_range(self._hwnd)
        page_tokens = yh_fills_page_evidence(items)
        coverage["pages_complete"] = (
            page_tokens.get("page_total") == 1
            and not page_tokens.get("next_page_present"))
        rows = yh_fills_from_items(items, row_delta=row_delta, min_code_x=0.0)
        headers = _yh_ro_header_band(items, YH_FILLS_COLUMN_ALIASES,
                                     row_delta)
        required = {"date", "time", "side_raw", "code", "quantity",
                    "price", "amount"}
        coverage["columns_complete"] = (
            coverage["columns_complete"] and required <= set(headers))
        summary = yh_fills_summary_evidence(items, row_delta=row_delta,
                                            min_code_x=0.0)
        extent: Optional[dict[str, str]] = None
        row_dates = sorted({str(row.get("date") or "").replace("-", "")
                            for row in rows if row.get("date")})
        if len(row_dates) >= 1:
            extent = {"start": row_dates[0], "end": row_dates[-1]}
        count_total = page_tokens.get("count_total")
        complete = (coverage["rows_complete"] and coverage["columns_complete"]
                    and coverage["pages_complete"]
                    and isinstance(count_total, int)
                    and count_total == len(rows))
        base.update({
            "rows": rows, "header_columns": headers,
            "date_range": date_range, "date_range_source": date_source,
            "coverage_complete": complete,
            "empty_confirmed": complete and not rows,
            "row_count_read": len(rows),
            "count_total": count_total,
            "page_current": page_tokens.get("page_current"),
            "page_total": page_tokens.get("page_total"),
            "next_page_present": page_tokens.get("next_page_present"),
            "summary": summary,
            "observed_record_date_extent": extent,
            "ocr_available": True, "reader": "rapidocr_onnxruntime",
            "wrong_page": bool(page_tokens.get("orders_like_page")),
            "scroll": scroll, "ocr_meta": meta,
        })
        return base


def _yh_readonly_report(evidence, *, with_fills=False):
    """纯报告组装：只调用现有纯裁决（yh_single_snapshot / yh_fills_verdict），
    不采集、不获锁、不落盘。安全拒绝（abort/身份不一致/fills 身份拒绝）→
    全链撤销 rc4；字段或覆盖不足 → partial rc0 保留已知行。"""
    def base(status, rc, reason):
        return {
            "schema": SCHEMA, "command": "read-account", "yuanhang": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "credentials_touched": False, "status": status, "rc": rc,
            "reason": reason, "account": None, "masked_id": None,
            "funds": None, "funds_labels": None, "funds_read_ok": False,
            "funds_as_of": None, "holdings": None, "holdings_headers": None,
            "holdings_as_of": None, "funds_evidence": None,
            "holdings_evidence": None, "coverage_evidence": None,
            "empty_confirmed": False,
            "identity": {"before": None, "after": None},
            "snapshot_decision": {"status": "unavailable", "reason": reason,
                                  "rc": 4},
            "fills": {
                "attempted": False, "status": "unavailable", "reason": reason,
                "rows": [], "header_columns": [], "date_range": None,
                "date_range_source": None, "date_range_proven": False,
                "coverage_complete": False, "coverage_evidence": None,
                "empty_confirmed": False, "row_count": 0,
                "row_count_read": 0, "count_total": None,
                "page_current": None, "page_total": None,
                "next_page_present": False, "summary": None,
                "observed_record_date_extent": None},
            "fills_decision": {"status": "unavailable", "reason": reason},
            "coverage": {"holdings_complete": False,
                         "fills_complete": False},
            "with_fills": bool(with_fills),
        }

    ev = evidence if isinstance(evidence, dict) else {}
    account = ev.get("expected_account")
    abort = ev.get("abort_status")
    if abort:
        return base(abort, 4, abort)
    before = ev.get("identity_before")
    after = ev.get("identity_after")
    if not account or not before or before != account:
        return base("identity_unverified_fail_closed", 4,
                    "identity_before_unverified")
    if after is None:
        return base("identity_after_unverified", 4,
                    "identity_after_unverified")
    if after != account:
        return base("identity_changed_fail_closed", 4, "identity_changed")
    fills_ev = ev.get("fills")
    if not isinstance(fills_ev, dict):
        fills_ev = {}
    attempted = fills_ev.get("attempted") is True
    if attempted and fills_ev.get("identity_reject"):
        return base("fills_identity_unverified", 4,
                    "fills_identity_unverified")
    proof = dict(ev)
    holdings_ev = ev.get("holdings_evidence")
    ocr_available = (isinstance(holdings_ev, dict)
                     and holdings_ev.get("ocr_available") is True)
    proof["ocr_available"] = bool(ocr_available)
    snapshot = yh_single_snapshot(proof)
    if attempted:
        verdict = yh_fills_verdict(fills_ev)
    else:
        verdict = {"status": "unavailable", "reason": "fills_not_attempted",
                   "coverage_complete": False, "date_range_proven": False,
                   "row_count": 0}
    funds = ev.get("funds")
    holdings = ev.get("holdings")
    headers = ev.get("headers")
    stock_ok = snapshot.get("status") == "ok"
    fills_ok = (not attempted) or verdict.get("status") == "ok"
    status = "ok" if stock_ok and fills_ok else "partial"
    report = base(status, 0, status)
    report["account"] = account
    report["identity"] = {"before": before, "after": after}
    report["funds"] = funds if isinstance(funds, dict) else {}
    report["funds_labels"] = ev.get("funds_labels") or {}
    report["funds_read_ok"] = bool(isinstance(funds, dict) and funds)
    report["funds_as_of"] = ev.get("funds_as_of")
    report["holdings"] = holdings if isinstance(holdings, list) else []
    report["holdings_headers"] = headers if isinstance(headers, list) else []
    report["holdings_as_of"] = ev.get("holdings_as_of")
    report["funds_evidence"] = dict(ev.get("funds_evidence") or {})
    report["holdings_evidence"] = dict(holdings_ev or {})
    report["coverage_evidence"] = dict(ev.get("coverage_evidence") or {})
    report["empty_confirmed"] = ev.get("empty_confirmed") is True
    report["snapshot_decision"] = dict(snapshot)
    report["fills_decision"] = dict(verdict)
    report["coverage"] = {
        "holdings_complete": ev.get("coverage_complete") is True and stock_ok,
        "fills_complete": verdict.get("coverage_complete") is True,
    }
    fills_report = dict(fills_ev)
    fills_report.update({
        "status": verdict.get("status"), "reason": verdict.get("reason"),
        "row_count": verdict.get("row_count"),
        "date_range_proven": verdict.get("date_range_proven"),
        "coverage_complete": verdict.get("coverage_complete") is True,
    })
    report["fills"] = fills_report
    return report


def _yh_collect_single_snapshot(*, account: Optional[str] = None,
                                happ_exe: Path = YUANHANG_EXE_DEFAULT,
                                with_fills: bool = False,
                                f12_wait: float = 8.0,
                                main_wait: float = 20.0,
                                report: Optional[dict[str, Any]] = None,
                                ) -> dict[str, Any]:
    """单账号证据采集器（**绝不切号**）：面板/身份/资金股票页/资金汇总/
    持仓行/（显式请求且 Owner 空闲时）历史逐笔成交页证据。全部真机原语
    复用既有远航版段（_yh_ensure_main/_yh_ensure_panel/_yh_capture_window/
    _yh_read_funds/视觉读取）；任何一步证据不足即如实降级（abort_status/
    部分 evidence 缺省），绝不造数。产物供 yh_single_snapshot 与
    yh_fills_verdict 两级纯函数裁决。"""
    rep = report if report is not None else {}
    ev: dict[str, Any] = {
        "expected_account": account,
        "identity_before": None, "identity_after": None,
        "masked_before": None, "masked_after": None,
        "stock_page_confirmed": False, "ocr_available": False,
        "reader": None, "headers": [], "coverage_complete": False,
        "empty_confirmed": False, "funds": {}, "funds_labels": {},
        "holdings": [], "holdings_source": "not_read", "funds_source": None,
        "owner_active": False, "page_switch": None, "vision_errors": [],
        "wrong_account_abort": False, "abort_status": None,
        "fills": {"attempted": False, "reason": "fills_not_requested"},
    }
    rep.setdefault("steps", []).append(
        {"step": "main_ensure", **_yh_ensure_main(happ_exe, main_wait)})
    main = _wait_for(_yh_main_window, 5.0, 0.5)
    if main is None:
        ev["abort_status"] = "main_window_not_found"
        return ev
    guard_hwnds = [main["hwnd"]] + [p["hwnd"] for p in _yh_enum_panels()]
    owner_active = any(_yh_owner_using(h) for h in guard_hwnds)
    ev["owner_active"] = owner_active
    if owner_active:
        rep["steps"].append(
            {"step": "owner_active_interactive_skipped",
             "note": "Owner 在用：零交互模式（不键击不点击，仅截图+UIA 读）"})
    panel, steps = _yh_ensure_panel(main["hwnd"], f12_wait,
                                    allow_f12=not owner_active
                                    and _yh_injection_idle_ok(rep))
    rep["steps"].extend(steps)
    if panel is None:
        ev["abort_status"] = "panel_unavailable_fail_closed"
        return ev
    if _yh_uia_login_page(panel["hwnd"]):
        rep["captures"].append(
            _yh_capture_window(panel["hwnd"], "read_login_page"))
        ev["abort_status"] = "not_logged_in_fail_closed"
        return ev

    def injection_allowed() -> bool:
        """每次注入（F12/树选择/点击兜底）前复核：Owner 不在 THS 窗上
        且全局输入空闲≥90s（run3 实机教训：仅查 THS 前台会在 Owner 用
        其他窗口时抢焦点）。任一不满足 → False 让位零输入。"""
        if owner_active:
            return False
        return _yh_injection_idle_ok(rep)

    def capture_and_read(prefix: str) -> tuple[dict[str, Any], dict[str, Any]]:
        cap = _yh_capture_window(panel["hwnd"], prefix)
        rep["captures"].append(cap)
        rep["capture_path"] = cap["path"]
        return cap, _yh_read_image(cap["path"])

    cap, read = capture_and_read("read_account")   # noqa: F841 - cap 留证
    ev["reader"] = read["reader"]
    if read["errors"]:
        ev["vision_errors"] = list(read["errors"])
    masked = _yh_masked_from_read(read)
    ev["masked_before"] = masked
    active = yh_account_from_masked(masked)
    funds_probe, probe_src = _yh_read_funds(panel, read["items"])
    ev["funds_source_probe"] = probe_src
    if active is None and masked is None \
            and not funds_probe["funds"] and not owner_active:
        # 身份+资金双盲：可能只是停在其他页 → 先翻资金页再交叉验证一次
        if injection_allowed():
            page_try = _yh_uia_select_tree_item(panel["hwnd"], "资金股票")
            rep["steps"].append({"step": "page_retry_for_identity",
                                 **page_try})
            if page_try.get("ok"):
                time.sleep(0.8)
                funds_probe, probe_src = _yh_read_funds(panel, [])
                ev["funds_source_probe"] = probe_src
    ev["identity_before"] = active
    if active is None:
        _acct, why = yh_identity_gate(masked, bool(funds_probe["funds"]))
        ev["identity_gate"] = why
        ev["abort_status"] = {
            "not_logged_in": "not_logged_in_fail_closed",
            "unknown_masked_id": "identity_gate_unknown_account",
            "unreadable_funds_alive": "identity_gate_unreadable",
        }.get(why, "identity_gate_unreadable")
        return ev
    if account is not None and active != account:
        # 单账号链绝不切号：活动≠目标立即中止，绝不在他人面板上翻页/读数
        ev["wrong_account_abort"] = True
        ev["abort_status"] = "wrong_account_fail_closed"
        return ev
    if account is None:
        # 「读当前活动账号」模式：期望=已核实的活动账号（前后一致才可 ok）
        ev["expected_account"] = active
    _YH_ACCOUNT_STATE["current_used_account"] = active

    # e 资金股票页（Owner 活跃或全局输入空闲不足 → 零输入不翻页）
    if not injection_allowed():
        ev["page_switch"] = {"attempted": False,
                             "reason": "owner_active_zero_input"}
    else:
        ev["page_switch"] = {"attempted": True,
                             **_yh_uia_select_tree_item(panel["hwnd"],
                                                        "资金股票")}
        if ev["page_switch"].get("ok"):
            time.sleep(0.8)            # 页面渲染缓冲
            _cap2, read = capture_and_read("read_account")
            ev["reader"] = read["reader"]
            if read["errors"]:
                ev["vision_errors"] = ev["vision_errors"] + list(read["errors"])
            ev["stock_page_confirmed"] = True

    # f 资金汇总（UIA 直读优先，既有 _yh_read_funds 双路径原样复用）
    funds, funds_src = _yh_read_funds(panel, read["items"])
    ev["funds"] = funds.get("funds") or {}
    ev["funds_labels"] = funds.get("labels") or {}
    ev["funds_source"] = funds_src

    # g 持仓行（视觉模型/本地 OCR；均无 → ocr_unavailable 如实标记）
    if read["reader"] is None:
        ev["holdings"] = []
        ev["holdings_source"] = "ocr_unavailable"
    else:
        ev["ocr_available"] = True
        rows = (yh_rows_from_vision(read["rows"]) if read["rows"]
                else yh_holdings_from_items(read["items"]))
        ev["holdings"] = rows
        ev["holdings_source"] = read["reader"]
        ev["headers"] = yh_holdings_header_canons(rows, read["items"])
        ev["coverage_complete"] = (bool(rows)
                                   and read["reader"] == "vision_model"
                                   and _yh_holdings_no_pagination(read["items"]))
        if not rows:
            ev["holdings_note"] = ("reader 有输出但未解析出持仓行"
                                   "（空仓或解析失败，空仓未证实）")

    # 交互后身份复核（翻页/读取之后、fills 之前）
    ev["masked_after"] = _yh_masked_from_read(read)
    ev["identity_after"] = yh_account_from_masked(ev["masked_after"])

    # h 历史逐笔成交页（显式请求且可注入；查询页导航，非委托界面）
    if not with_fills:
        ev["fills"] = {"attempted": False, "reason": "fills_not_requested"}
    elif not injection_allowed():
        ev["fills"] = {"attempted": False,
                       "reason": "owner_active_zero_input"}
    else:
        fev: dict[str, Any] = {"attempted": True, "reason": None,
                               "tried_tokens": [], "tree_token": None,
                               "collected_at":
                                   datetime.now(timezone.utc).isoformat()}
        sel: dict[str, Any] = {"ok": False}
        for token in YH_FILLS_TREE_CANDIDATES:
            if not injection_allowed():    # 每次注入前复核全局输入空闲
                fev["reason"] = "owner_active_zero_input"
                break
            fev["tried_tokens"].append(token)
            sel = _yh_uia_select_tree_item(panel["hwnd"], token)
            rep["steps"].append(
                {"step": "fills_page_switch", "token": token,
                 "ok": bool(sel.get("ok")), "method": sel.get("method"),
                 "detail": sel.get("error") or sel.get("item") or ""})
            if sel.get("ok"):
                fev["tree_token"] = token
                break
        if not sel.get("ok"):
            fev["reason"] = fev.get("reason") or "fills_page_not_located"
            fev.update(rows=[], row_count=0)
            ev["fills"] = fev
        else:
            time.sleep(0.8)
            capf, readf = capture_and_read("fills_page")
            fev["capture_path"] = capf["path"]
            fev["reader"] = readf["reader"]
            items_f = readf["items"]
            page_ev = yh_fills_page_evidence(items_f)
            fev.update(page_ev)
            fev["wrong_page"] = bool(page_ev["orders_like_page"])
            fev["summary"] = yh_fills_summary_evidence(items_f)
            fev["header_columns"] = sorted(
                _yh_fills_header_map(items_f).keys())
            date_tokens = yh_fills_date_tokens(items_f)
            isos = sorted({d["iso"] for d in date_tokens})
            fev["date_tokens"] = date_tokens
            fev["observed_record_date_extent"] = (
                {"start": isos[0], "end": isos[-1]} if isos else None)
            rows_f = (yh_fills_rows_from_vision(readf["rows"])
                      if readf["rows"] else yh_fills_from_items(items_f))
            fev["rows"] = rows_f          # 原样保留（含真实重复，绝不去重）
            fev["row_count_read"] = len(rows_f)
            masked_after_f = _yh_masked_from_read(readf)
            acct_after_f = yh_account_from_masked(masked_after_f)
            fev["masked_after"] = masked_after_f
            fev["identity_after"] = acct_after_f
            if acct_after_f is not None and acct_after_f != active:
                fev["identity_reject"] = True      # 身份变化 → 全链 fail-closed
            elif acct_after_f is None:
                fev["identity_reject"] = "unreadable"   # fills 段不归属
            ev["fills"] = fev
    return ev


def run_read_account(*, account: Optional[str] = None,
                     happ_exe: Path = YUANHANG_EXE_DEFAULT,
                     allow_switch: bool = False,
                     menu_config: Optional[Path] = None,
                     menu_wait_sec: float = 1.5, settle_sec: float = 3.0,
                     f12_wait: float = 8.0, main_wait: float = 20.0,
                     with_fills: bool = False,
                     report_root: Optional[Path] = None,
                     lock_timeout: float = 10.0) -> dict[str, Any]:
    """远航版**单账号**只读入口（2026-11-15 任务书重构：新锁+新 adapter）。

    链路：_yh_readonly_lock（会话级 Win32 命名互斥；锁在 adapter 构造前
    取得、持有至采集器返回含其最终 identity/guard 核验；不触 runtime 文件
    锁，也不与旧文件锁互斥）→ _YhReadonlyAdapter（六方法只读协议，绝不
    启动客户端/恢复窗口/切号/碰订单页）→ _yh_collect_with_adapter（现有
    采集器，安全异常撤销全部证据）→ _yh_readonly_report（现有纯裁决组装）。
    account 省略时锁内 guard/identity/guard 先确定期望账号（读不出 →
    fail-closed）。默认不落盘：只返回 JSON 可序列化 dict（report_root 仅
    兼容保留，本链不写文件）。allow_switch/menu_* /f12_wait/main_wait 仅
    保留 API 形状（旧等待参数新链不使用）。安全拒绝/身份不明变化/异常 →
    全链撤销 rc4 且不泄露异常文本；字段或覆盖不足 → partial rc0 保留已知
    数据。旧 collector（_yh_collect_single_snapshot）与 run_read_account_
    switchable 原样保留，但新链绝不调用。
    """
    def refused(status):
        return _yh_readonly_report({"abort_status": status},
                                   with_fills=with_fills)

    evidence = None
    try:
        with _yh_readonly_lock(timeout_seconds=lock_timeout) as acquired:
            if acquired is not True:
                return refused("lock_busy")
            adapter = _YhReadonlyAdapter(happ_exe=happ_exe)
            expected = account
            if expected is None:
                pre = adapter.guard()
                if not (isinstance(pre, dict) and pre.get("ok") is True
                        and pre.get("owner_active") is False):
                    return refused("guard_fail_closed")
                ident = adapter.identity()
                post = adapter.guard()
                if not (isinstance(post, dict) and post.get("ok") is True
                        and post.get("owner_active") is False):
                    return refused("guard_fail_closed")
                expected = (ident.get("account")
                            if isinstance(ident, dict) else None)
                if not expected or not (isinstance(ident, dict)
                                        and ident.get("fingerprint")):
                    return refused("identity_unreadable_fail_closed")
            evidence = _yh_collect_with_adapter(
                adapter, expected, with_fills=with_fills)
        if not isinstance(evidence, dict):
            return refused("collector_invalid_fail_closed")
        return _yh_readonly_report(evidence, with_fills=with_fills)
    except ThsXiadanError:
        return refused("exception_fail_closed")
    except Exception:  # noqa: BLE001 - fail-closed：异常文本绝不入报告
        return refused("exception_fail_closed")


def run_read_account_switchable(*, account: Optional[str] = None,
                                happ_exe: Path = YUANHANG_EXE_DEFAULT,
                                allow_switch: bool = True,
                                menu_config: Optional[Path] = None,
                                menu_wait_sec: float = 1.5, settle_sec: float = 3.0,
                                f12_wait: float = 8.0, main_wait: float = 20.0,
                                report_root: Optional[Path] = None) -> dict[str, Any]:
    """远航版资金/持仓只读读取（旧版可切换主体；2026-11-15 起 read-account
    CLI 改走单账号不切号链 run_read_account，本函数原样保留自动切换能力，
    供双账号阶段复用，行为与 2026-11-14 版本一致）。

    a 确保 happ 主程序运行（未运行 Popen 带 cwd）；
    b F12 拉起面板或枚举已开面板（每次重枚举防句柄失效）；
    c 判定活动账号：PrintWindow 截图+视觉读脱敏账号栏，UIA 资金区交叉验证
      （fail-closed：读不到不归属任何数据）；
    d 活动账号≠目标 → 先执行账号切换（失败即 fail-closed 不读数据）；
    e 切资金股票页（UIA TreeItem，Owner 活跃时绝不点击）；
    f UIA 读资金汇总（实测区 y150-320 x150-950）；
    g 持仓行：PrintWindow 离屏截图 + 视觉模型读取（无视觉环境时
      holdings_source=ocr_unavailable 如实标记，绝不造数）。
    rc：0 ok · 1 失败/切号失败 · 4 门禁 fail-closed（未登录/身份不可读）。
    """
    started = datetime.now(timezone.utc)
    report: dict[str, Any] = {
        "schema": SCHEMA, "command": "read-account", "yuanhang": True,
        "status": "failure", "rc": 1,
        "account_requested": account, "account": None, "masked_id": None,
        "funds": {}, "funds_labels": {}, "funds_read_ok": False,
        "funds_source": None, "holdings": [], "holdings_source": "not_read",
        "capture_path": None, "page_switch": None, "switch": None,
        "steps": [], "captures": [], "credentials_touched": False,
        "started_at": started.isoformat(),
    }
    try:
        report["steps"].append({"step": "main_ensure",
                                **_yh_ensure_main(happ_exe, main_wait)})
        main = _wait_for(_yh_main_window, 5.0, 0.5)
        if main is None:
            report["status"] = "main_window_not_found"
            report["reason"] = "happ 运行但无可见顶层窗"
            return report
        guard_hwnds = [main["hwnd"]] + [p["hwnd"] for p in _yh_enum_panels()]
        owner_active = any(_yh_owner_using(h) for h in guard_hwnds)
        if owner_active:
            report["steps"].append(
                {"step": "owner_active_interactive_skipped",
                 "note": "Owner 在用：零交互模式（不键击不点击，仅截图+UIA 读）"})
        panel, steps = _yh_ensure_panel(main["hwnd"], f12_wait,
                                        allow_f12=not owner_active)
        report["steps"].extend(steps)
        if panel is None:
            report["status"] = "panel_unavailable_fail_closed"
            report["rc"] = 4
            report["reason"] = ("无已开面板且（Owner 活跃或 F12 未生效）拉不起："
                                "未登录或 Owner 使用中（fail-closed）")
            return report
        if _yh_uia_login_page(panel["hwnd"]):
            report["captures"].append(
                _yh_capture_window(panel["hwnd"], "read_login_page"))
            report["status"] = "not_logged_in_fail_closed"
            report["rc"] = 4
            report["reason"] = ("面板停在登录页（UIA Edit1001/Edit1012/"
                                "Button1006）：未登录，fail-closed")
            return report

        def capture_and_read() -> tuple[dict[str, Any], dict[str, Any]]:
            cap = _yh_capture_window(panel["hwnd"], "read_account")
            report["captures"].append(cap)
            report["capture_path"] = cap["path"]
            return cap, _yh_read_image(cap["path"])

        _cap, read = capture_and_read()
        report["vision_reader"] = read["reader"]
        if read["errors"]:
            report["vision_errors"] = read["errors"]
        masked = _yh_masked_from_read(read)
        report["masked_id"] = masked
        active = yh_account_from_masked(masked)
        funds_pre, funds_src = _yh_read_funds(panel, read["items"])
        if active is None and masked is None \
                and not funds_pre["funds"] and not owner_active:
            # 身份+资金双盲：可能只是停在其他页 → 先翻资金页再交叉验证一次
            page_try = _yh_uia_select_tree_item(panel["hwnd"], "资金股票")
            report["steps"].append(
                {"step": "page_retry_for_identity", **page_try})
            if page_try.get("ok"):
                time.sleep(0.8)
                funds_pre, funds_src = _yh_read_funds(panel, [])
        report["steps"].append({
            "step": "identity_gate", "masked_id": masked,
            "active_account": active,
            "funds_cross_check": bool(funds_pre["funds"])})
        report["funds_source_probe"] = funds_src
        if active is None:
            gate, why = yh_identity_gate(masked, bool(funds_pre["funds"]))
            report["rc"] = 4
            report["holdings_source"] = "identity_gate_blocked"
            report["reason"] = f"身份门禁 fail-closed（{why}）：数据不归属账号"
            report["status"] = {
                "not_logged_in": "not_logged_in_fail_closed",
                "unknown_masked_id": "identity_gate_unknown_account",
                "unreadable_funds_alive": "identity_gate_unreadable",
            }.get(why, "identity_gate_unreadable")
            return report
        _YH_ACCOUNT_STATE["current_used_account"] = active
        report["account"] = active
        if account and account != active:
            if not allow_switch:
                report["status"] = "wrong_account_fail_closed"
                report["rc"] = 4
                report["reason"] = (f"活动 {active}≠目标 {account} 且 "
                                    "--no-switch（fail-closed）")
                return report
            sw = run_switch_account(
                to=account, happ_exe=happ_exe, menu_config=menu_config,
                menu_wait_sec=menu_wait_sec, settle_sec=settle_sec,
                f12_wait=f12_wait, main_wait=main_wait,
                report_root=report_root)
            report["switch"] = {"status": sw.get("status"),
                                "rc": sw.get("rc"),
                                "from_account": sw.get("from_account"),
                                "masked_id_after": sw.get("masked_id_after"),
                                "evidence": sw.get("evidence")}
            if sw.get("rc") != 0:
                report["status"] = "switch_failed"
                report["rc"] = int(sw.get("rc") or 1)
                report["reason"] = (f"活动 {active}≠目标 {account}，先切账号"
                                    f"失败：{sw.get('status')} "
                                    f"{sw.get('reason') or ''}")
                return report
            panels = _wait_for(lambda: _yh_enum_panels(), 5.0, 0.5) or []
            if not panels:
                report["status"] = "panel_lost_after_switch"
                report["reason"] = "切换后面板枚举为空"
                return report
            panel = panels[0]
            _cap, read = capture_and_read()
            masked = _yh_masked_from_read(read)
            active = yh_account_from_masked(masked)
            report["masked_id"] = masked
            report["account"] = active
            if active != account:
                report["status"] = "identity_gate_unreadable"
                report["rc"] = 4
                report["holdings_source"] = "identity_gate_blocked"
                report["reason"] = (f"切换后身份复核失败 masked={masked!r}"
                                    f"≠{account}（fail-closed 不归属）")
                return report
            report["steps"].append({"step": "switched_then_verified",
                                    "account": active})
        # e 切资金股票页（Owner 活跃时绝不点击）
        if owner_active:
            report["page_switch"] = {"attempted": False,
                                     "reason": "owner_active_zero_input"}
        else:
            report["page_switch"] = {"attempted": True,
                                     **_yh_uia_select_tree_item(
                                         panel["hwnd"], "资金股票")}
            if report["page_switch"].get("ok"):
                time.sleep(0.8)            # 页面渲染缓冲
                _cap, read = capture_and_read()   # 翻页后重新截图读持仓
        # f 资金汇总（UIA 直读优先）
        funds, funds_src = _yh_read_funds(panel, read["items"])
        report["funds"] = funds.get("funds") or {}
        report["funds_labels"] = funds.get("labels") or {}
        report["funds_source"] = funds_src
        report["funds_read_ok"] = bool(report["funds"])
        # g 持仓行（视觉模型/本地 OCR；均无 → ocr_unavailable 如实标记）
        if read["reader"] is None:
            report["holdings"] = []
            report["holdings_source"] = "ocr_unavailable"
        else:
            rows = yh_rows_from_vision(read["rows"]) if read["rows"] \
                else yh_holdings_from_items(read["items"])
            report["holdings"] = rows
            report["holdings_source"] = read["reader"]
            if not rows:
                report["holdings_note"] = ("reader 有输出但未解析出持仓行"
                                           "（空仓或解析失败，如实保留空表）")
        report["status"] = "ok"
        report["rc"] = 0
        return report
    except ThsXiadanError as exc:
        report["status"] = "failure"
        report["reason"] = f"{type(exc).__name__}: {exc}"
        return report
    except Exception as exc:  # noqa: BLE001 - fail-closed：异常如实入报告
        report["status"] = "failure"
        report["reason"] = f"{type(exc).__name__}: {exc}"
        return report
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["account_state"] = yh_account_state_snapshot()
        try:
            report["report_path"] = str(_write_report(
                report, report_root, prefix="read_account"))
        except Exception:  # noqa: BLE001
            pass


def cmd_read_account(args: argparse.Namespace) -> int:
    """read-account CLI：调用真实 run_read_account，仅打印 JSON（默认不落盘）。"""
    report = run_read_account(
        account=getattr(args, "account", None),
        happ_exe=getattr(args, "happ_exe", None) or YUANHANG_EXE_DEFAULT,
        with_fills=getattr(args, "fills", False) is True,
        allow_switch=not getattr(args, "no_switch", False),
        lock_timeout=getattr(args, "lock_timeout", 10.0),
        report_root=getattr(args, "report_root", None))
    print(json.dumps(report, ensure_ascii=False))
    return int(report.get("rc", 1))


def cmd_switch_account(args: argparse.Namespace) -> int:
    report = run_switch_account(
        to=args.to, happ_exe=args.happ_exe, menu_config=args.menu_config,
        menu_wait_sec=args.menu_wait_sec, settle_sec=args.settle_sec,
        f12_wait=args.f12_wait, main_wait=args.main_wait,
        dry_run=args.dry_run,
        report_root=Path(args.report_root) if args.report_root else None)
    print(json.dumps(report, ensure_ascii=False, default=str))
    return int(report.get("rc", 1))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ths_client",
        description="同花顺统一客户端（login/export-fills/poll-holdings/"
                    "survey/status/daily/read-account/switch-account；"
                    "fail-closed，凭证零接触，绝不触碰委托下单界面）")
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

    p = sub.add_parser("read-account", help="远航版单账号资金/持仓/历史逐笔"
                                            "成交只读（会话级命名互斥锁；"
                                            "绝不切号；--fills 显式触发"
                                            "成交段）")
    p.add_argument("--account", choices=sorted(YH_ACCOUNTS), default=None,
                   help="目标账号 acct1|acct2（缺省=锁内 guard/identity/guard"
                        " 确定当前活动账号，读不出 fail-closed rc4；活动≠"
                        "目标 fail-closed 绝不切号——切号走 switch-account）")
    p.add_argument("--fills", action="store_true",
                   help="附加历史逐笔成交段采集（查询页证据驱动；当前视口"
                        "partial 如实降级，绝不按当天/近一月推断日期）")
    p.add_argument("--happ-exe", type=Path, default=YUANHANG_EXE_DEFAULT,
                   help="远航版主程序路径（默认 D:/同花顺远航/bin/happ.exe；"
                        "实际以进程镜像动态校验，含探针证实的 同花顺远航版"
                        " 安装根候选）")
    p.add_argument("--lock-timeout", type=float, default=10.0,
                   help="只读命名互斥锁等待秒数（默认 10；锁忙 lock_busy "
                        "rc4）")
    p.add_argument("--no-switch", action="store_true",
                   help="（兼容保留）新链默认即绝不切号，本参数无行为差异")
    p.add_argument("--menu-config", type=Path, default=None,
                   help="（兼容保留，新只读链不使用）")
    p.add_argument("--f12-wait", type=float, default=8.0,
                   help="（兼容保留，新只读链不使用）")
    p.add_argument("--main-wait", type=float, default=20.0,
                   help="（兼容保留，新只读链不使用）")
    p.add_argument("--settle-sec", type=float, default=3.0,
                   help="（兼容保留，新只读链不使用）")
    p.add_argument("--menu-wait-sec", type=float, default=1.5,
                   help="（兼容保留，新只读链不使用）")
    p.add_argument("--report-root", help="（兼容保留）新链默认不落盘，仅"
                                         "打印 JSON")
    p.set_defaults(func=cmd_read_account)

    p = sub.add_parser("switch-account", help="远航版账号切换（主程序交易菜单"
                                              "真实点击；Owner 活跃守卫+切换"
                                              "后验证 fail-closed）")
    p.add_argument("--to", required=True, choices=sorted(YH_ACCOUNTS),
                   help="目标账号 acct1（曾）| acct2（王）")
    p.add_argument("--happ-exe", type=Path, default=YUANHANG_EXE_DEFAULT,
                   help="远航版主程序路径（默认 D:/同花顺远航/bin/happ.exe）")
    p.add_argument("--menu-config", type=Path, default=None,
                   help="菜单坐标配置 JSON（默认 env "
                        "AISTOCK_THS_YUANHANG_MENU_JSON 或 "
                        "runtime/ths_client/yuanhang_menu.json；缺失走视觉"
                        "辅助定位，绝不盲点）")
    p.add_argument("--dry-run", action="store_true",
                   help="只跑定位链（交易菜单/账号项定位）报 would_click "
                        "坐标，零真实点击")
    p.add_argument("--f12-wait", type=float, default=8.0)
    p.add_argument("--main-wait", type=float, default=20.0)
    p.add_argument("--settle-sec", type=float, default=3.0)
    p.add_argument("--menu-wait-sec", type=float, default=1.5)
    p.add_argument("--report-root", help="覆盖报告目录（测试用）")
    p.set_defaults(func=cmd_switch_account)

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


def simulate_order_pure(symbol, side, quantity, price):
    return {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
        "status": "simulated",
        "submitted": False,
        "broker_order_id": None,
    }


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

OUT_DIR = Path(r"F:\Aistock\runtime\l2_r50")
PANEL_MAP = OUT_DIR / "panel_map.json"
JOURNAL = OUT_DIR / "journal_r50_phase1.md"
PANELS_JSON = Path(r"F:\Aistock\runtime\gen3_state_alpha\ths_panels.json")
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
                # R52：通道链 + 目标账号热键（曾=1/王=2，注册表规则携带）
                target_hotkey = ""
                for _acct, _rule in sorted(rule_by_acct.items()):
                    if (_rule or {}).get("identity_hint") == target_hint:
                        target_hotkey = str(_rule.get("hotkey") or "")
                        break
                cfg = {"target_hint": target_hint,
                       "known_hints": known_hints,
                       "channels": [hotswitch.CHANNEL_HOTKEY,
                                    hotswitch.CHANNEL_COMBOBOX],
                       "target_hotkey": target_hotkey}
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
