"""Automate the TDX post-close download window without handling credentials.

The installed TongdaXin client keeps the username/password in its own UI.  This
script deliberately never reads or writes either field.  It only:

1. launches ``TdxW.exe`` when no client window exists;
2. clicks the already-populated login button once;
3. closes the QR-login web dialog;
4. clicks the login button a second time;
5. opens ``选项 -> 盘后数据下载`` by resolving the menu command from its text;
6. selects daily and 5-minute data and starts the download.

The client is a custom-drawn Windows application, so the automation uses only
small, deterministic Win32 calls.  It is fail-closed: if a window or expected
control is missing, it reports failure and does not guess a coordinate or click
again.  Runtime reports are written outside the repository via ``utils.paths``.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:  # pragma: no cover - exercised on the Windows host
    USER32 = ctypes.windll.user32
else:  # pragma: no cover - import guard for Linux CI/static tests
    USER32 = None


# Win32 constants used by the narrow automation surface.
GW_CHILD = 5
GW_HWNDNEXT = 2
GW_OWNER = 4
GA_ROOT = 2
SW_RESTORE = 9
SW_SHOW = 5
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
HWND_TOP = 0
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001
BM_CLICK = 0x00F5
BM_GETCHECK = 0x00F0
TCM_GETITEMCOUNT = 0x1304
MN_GETHMENU = 0x01E1
PBM_GETPOS = 0x0408
PBM_GETRANGE = 0x0407
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

TDX_MAIN_CLASS = "TdxW_MainFrame_Class"
TDX_LOGIN_CLASS = "#32770"
TDX_EXE_DEFAULT = Path(r"D:\TDX\TdxW.exe")
TDX_ROOT_DEFAULT = Path(r"D:\TDX")
OPTION_MENU_X_96DPI = 600
OPTION_MENU_Y_96DPI = 12
DOWNLOAD_TIMEOUT_SEC = 30 * 60


class TdxAutomationError(RuntimeError):
    """Expected UI contract was not present; caller must stop and inspect."""


def _require_windows() -> None:
    if not IS_WINDOWS or USER32 is None:
        raise TdxAutomationError("TDX client automation requires Windows")


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
    if USER32 is None:
        raise TdxAutomationError("Win32 API unavailable")
    value = wintypes.RECT()
    if not USER32.GetWindowRect(hwnd, ctypes.byref(value)):
        raise TdxAutomationError(f"GetWindowRect failed hwnd={hwnd}")
    return int(value.left), int(value.top), int(value.right), int(value.bottom)


def _top_level_windows(limit: int = 4096) -> list[int]:
    """Enumerate desktop children without an EnumWindows callback.

    A previous probe on this host hung inside an EnumWindows ctypes callback.
    The GetWindow sibling chain is synchronous and bounded instead.
    """
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


def _normal_label(value: str) -> str:
    return " ".join(value.replace("&", "").split()).strip()


def _find_button(parent: int, label: str, *, visible_only: bool = False) -> Optional[int]:
    wanted = _normal_label(label)
    for hwnd in _children(parent):
        if _class_name(hwnd) != "Button":
            continue
        if visible_only and not _is_visible(hwnd):
            continue
        if _normal_label(_window_text(hwnd)) == wanted:
            return hwnd
    return None


def _find_visible_child_class(parent: int, cls: str) -> Optional[int]:
    for hwnd in _children(parent):
        if _class_name(hwnd) == cls and _is_visible(hwnd):
            return hwnd
    return None


def _find_main_window() -> Optional[int]:
    for hwnd in _top_level_windows():
        if not _is_visible(hwnd):
            continue
        if _class_name(hwnd) == TDX_MAIN_CLASS:
            return hwnd
    return None


def _find_login_window() -> Optional[int]:
    for hwnd in _top_level_windows():
        if not _is_visible(hwnd) or _class_name(hwnd) != TDX_LOGIN_CLASS:
            continue
        edits = sum(1 for child in _children(hwnd) if _class_name(child) == "SafeEdit")
        if edits >= 2 and _find_button(hwnd, "登录") is not None:
            return hwnd
    return None


def _find_owned_windows(owner: int, *, cls: Optional[str] = None) -> list[int]:
    out: list[int] = []
    for hwnd in _top_level_windows():
        if hwnd == owner or not _is_visible(hwnd):
            continue
        if int(USER32.GetWindow(hwnd, GW_OWNER)) != owner:
            continue
        if cls and _class_name(hwnd) != cls:
            continue
        out.append(hwnd)
    return out


def _bring_to_front(hwnd: int) -> None:
    _require_windows()
    root = _root(hwnd)
    USER32.ShowWindow(root, SW_RESTORE)
    USER32.ShowWindow(root, SW_SHOW)
    USER32.SetWindowPos(
        root,
        HWND_TOP,
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
    )
    USER32.BringWindowToTop(root)
    USER32.SetForegroundWindow(root)


def _click_window(hwnd: int) -> None:
    """Real mouse click; ControlClick/BM_CLICK is unreliable in TDX login UI."""
    _require_windows()
    _bring_to_front(hwnd)
    left, top, right, bottom = _rect(hwnd)
    x = (left + right) // 2
    y = (top + bottom) // 2
    if not USER32.SetCursorPos(x, y):
        raise TdxAutomationError(f"SetCursorPos failed for hwnd={hwnd}")
    time.sleep(0.08)
    USER32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    USER32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def _click_point(x: int, y: int) -> None:
    _require_windows()
    if not USER32.SetCursorPos(int(x), int(y)):
        raise TdxAutomationError(f"SetCursorPos failed for ({x},{y})")
    time.sleep(0.08)
    USER32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    USER32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def _wait_for(predicate: Callable[[], Any], timeout: float, interval: float = 0.25) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def _launch_client(exe: Path) -> None:
    if not exe.is_file():
        raise TdxAutomationError(f"TDX executable not found: {exe}")
    subprocess.Popen([str(exe)], cwd=str(exe.parent), close_fds=True)


def _login_if_needed(exe: Path, timeout: float) -> tuple[int, str]:
    """Return the main window and a state label; never touches credentials."""
    main = _find_main_window()
    if main:
        return main, "already_logged_in"

    login = _find_login_window()
    if not login:
        _launch_client(exe)
        login = _wait_for(_find_login_window, timeout)
        main = _find_main_window()
        if main and not login:
            return main, "already_logged_in_after_launch"
    if not login:
        raise TdxAutomationError("TDX login window not found")

    _bring_to_front(login)
    login_button = _find_button(login, "登录")
    if not login_button:
        raise TdxAutomationError("TDX login button not found")

    _click_window(login_button)

    def find_qr() -> Optional[int]:
        last = int(USER32.GetLastActivePopup(login))
        if last and last != login and _is_visible(last):
            title = _normal_label(_window_text(last))
            if "扫码" in title or "二维码" in title:
                return last
        for child in _find_owned_windows(login, cls=TDX_LOGIN_CLASS):
            title = _normal_label(_window_text(child))
            if "扫码" in title or "二维码" in title:
                return child
        return None

    qr = _wait_for(find_qr, min(timeout, 20.0))
    if not qr:
        main = _find_main_window()
        if main:
            return main, "logged_in_without_qr"
        if _find_login_window():
            raise TdxAutomationError("QR-login dialog did not appear; refusing a blind retry")
        raise TdxAutomationError("TDX login did not reach a known state")

    USER32.SendMessageW(qr, WM_CLOSE, 0, 0)
    if not _wait_for(lambda: not _is_visible(qr), 5.0):
        raise TdxAutomationError("QR-login dialog did not close")

    login = _wait_for(_find_login_window, 5.0)
    if not login:
        raise TdxAutomationError("Login window missing after QR dialog close")
    _bring_to_front(login)
    login_button = _find_button(login, "登录")
    if not login_button:
        raise TdxAutomationError("Login button missing after QR dialog close")
    _click_window(login_button)

    main = _wait_for(_find_main_window, timeout)
    if not main:
        raise TdxAutomationError("Second login click did not reach the TDX main page")
    return main, "logged_in_after_qr_close"


def _visible_menu_window(owner: int) -> Optional[int]:
    for hwnd in _top_level_windows():
        if not _is_visible(hwnd) or _class_name(hwnd) != "#32768":
            continue
        if int(USER32.GetWindow(hwnd, GW_OWNER)) in (owner, 0):
            return hwnd
    return None


def _dismiss_main_overlays(main: int) -> None:
    """Close the vendor's transient web/ad dialog before opening the menu."""
    for hwnd in _find_owned_windows(main, cls=TDX_LOGIN_CLASS):
        is_web_overlay = any(
            "webview" in _class_name(child).lower()
            or _normal_label(_window_text(child)) == "SubWebView"
            for child in _children(hwnd)
        )
        if is_web_overlay:
            USER32.SendMessageW(hwnd, WM_CLOSE, 0, 0)
            _wait_for(lambda: not _is_visible(hwnd), 3.0)


def _menu_command_by_text(menu_hwnd: int, wanted: str) -> Optional[int]:
    menu = int(USER32.SendMessageW(menu_hwnd, MN_GETHMENU, 0, 0))
    if not menu:
        return None
    wanted = _normal_label(wanted)
    count = int(USER32.GetMenuItemCount(menu))
    for index in range(max(0, count)):
        buf = ctypes.create_unicode_buffer(512)
        USER32.GetMenuStringW(menu, index, buf, len(buf), 0x0400)
        label = _normal_label(buf.value.split("\t", 1)[0])
        if label == wanted:
            command = int(USER32.GetMenuItemID(menu, index))
            return command or None
    return None


def _open_download_dialog(main: int, timeout: float) -> int:
    _bring_to_front(main)
    _dismiss_main_overlays(main)
    _bring_to_front(main)
    left, top, _right, _bottom = _rect(main)
    dpi = int(USER32.GetDpiForWindow(main)) if hasattr(USER32, "GetDpiForWindow") else 96
    scale = max(1.0, dpi / 96.0)
    x = left + round(OPTION_MENU_X_96DPI * scale)
    y = top + round(OPTION_MENU_Y_96DPI * scale)

    popup = None
    for _ in range(3):
        _click_point(x, y)
        popup = _wait_for(lambda: _visible_menu_window(main), 1.5)
        if popup:
            break
        _bring_to_front(main)
    if not popup:
        raise TdxAutomationError("TDX 选项菜单 did not open")

    command = _menu_command_by_text(popup, "盘后数据下载")
    if command is None:
        USER32.SendMessageW(popup, WM_CLOSE, 0, 0)
        raise TdxAutomationError("菜单中未找到‘盘后数据下载’")
    USER32.SendMessageW(main, WM_COMMAND, command, 0)

    def find_dialog() -> Optional[int]:
        for hwnd in _find_owned_windows(main, cls=TDX_LOGIN_CLASS):
            if _find_button(hwnd, "开始下载") and _find_visible_child_class(hwnd, "SysTabControl32"):
                return hwnd
        return None

    dialog = _wait_for(find_dialog, timeout)
    if not dialog:
        raise TdxAutomationError("盘后数据下载 dialog did not appear")
    return int(dialog)


def _select_tab(dialog: int, index: int) -> None:
    tab = _find_visible_child_class(dialog, "SysTabControl32")
    if not tab:
        raise TdxAutomationError("Download tab control not found")
    left, top, right, bottom = _rect(tab)
    count = int(USER32.SendMessageW(tab, TCM_GETITEMCOUNT, 0, 0)) or 5
    if index < 0 or index >= count:
        raise TdxAutomationError(f"Invalid download tab index: {index}")
    x = left + round((index + 0.5) * (right - left) / count)
    y = top + max(8, min(16, (bottom - top) // 2))
    _click_point(x, y)
    # The tab header is native, but the page body is owner-drawn and repaints
    # asynchronously.  Give the next visibility probe a short settling window.
    time.sleep(0.25)


def _checkbox_state(hwnd: int) -> int:
    return int(USER32.SendMessageW(hwnd, BM_GETCHECK, 0, 0))


def _ensure_checkbox(dialog: int, label: str, checked: bool) -> None:
    wanted = _normal_label(label)
    def find_match() -> list[int]:
        return [
            hwnd
            for hwnd in _children(dialog)
            if _class_name(hwnd) == "Button"
            and _is_visible(hwnd)
            and _normal_label(_window_text(hwnd)) == wanted
        ]

    matches = _wait_for(find_match, 2.0, interval=0.1) or []
    if not matches:
        raise TdxAutomationError(f"Visible checkbox not found: {label}")
    hwnd = matches[0]
    state = _checkbox_state(hwnd)
    should_be = 1 if checked else 0
    if (state != 0) != checked:
        USER32.SendMessageW(hwnd, BM_CLICK, 0, 0)
        time.sleep(0.15)
    if (_checkbox_state(hwnd) != 0) != checked:
        raise TdxAutomationError(f"Could not set checkbox: {label}")


def _configure_download(dialog: int) -> dict[str, bool]:
    _select_tab(dialog, 0)
    _ensure_checkbox(dialog, "日线和实时行情数据", True)
    _select_tab(dialog, 1)
    _ensure_checkbox(dialog, "1分钟线数据", False)
    _ensure_checkbox(dialog, "5分钟线数据", True)
    return {"daily": True, "minute_1": False, "minute_5": True}


def _file_snapshot(tdx_root: Path) -> dict[str, Any]:
    result = {"files": 0, "bytes": 0, "max_mtime_ns": 0}
    for market in ("sh", "sz", "bj"):
        directory = tdx_root / "vipdoc" / market / "fzline"
        if not directory.is_dir():
            continue
        for path in directory.glob("*.lc5"):
            try:
                stat = path.stat()
            except OSError:
                continue
            result["files"] += 1
            result["bytes"] += int(stat.st_size)
            result["max_mtime_ns"] = max(result["max_mtime_ns"], int(stat.st_mtime_ns))
    return result


def _progress_position(hwnd: int) -> int:
    progress = _find_visible_child_class(hwnd, "msctls_progress32")
    return int(USER32.SendMessageW(progress, PBM_GETPOS, 0, 0)) if progress else 0


def _start_and_wait(dialog: int, tdx_root: Path, timeout: float) -> dict[str, Any]:
    start = _find_button(dialog, "开始下载")
    if not start:
        raise TdxAutomationError("开始下载 button not found")
    before = _file_snapshot(tdx_root)
    started_at = time.monotonic()
    _click_window(start)
    saw_disabled = False
    saw_progress = False
    saw_file_change = False
    deadline = started_at + timeout
    while time.monotonic() < deadline:
        if not _is_visible(dialog):
            after = _file_snapshot(tdx_root)
            if after != before:
                return {"before": before, "after": after, "evidence": "dialog_closed_files_changed"}
            raise TdxAutomationError("Download dialog closed without completion evidence")
        enabled = bool(USER32.IsWindowEnabled(start))
        pos = _progress_position(dialog)
        current = _file_snapshot(tdx_root)
        if not enabled:
            saw_disabled = True
        if pos > 0:
            saw_progress = True
        if current != before:
            saw_file_change = True
        if saw_file_change or (saw_disabled and enabled):
            return {
                "before": before,
                "after": current,
                "evidence": "lc5_files_changed" if saw_file_change else "button_reenabled",
                "progress_seen": saw_progress,
            }
        time.sleep(2.0)
    raise TdxAutomationError(
        f"TDX download timeout ({int(timeout)}s); disabled={saw_disabled} "
        f"progress_seen={saw_progress} files_changed={saw_file_change}"
    )


def _write_report(payload: dict[str, Any]) -> Path:
    # Keep runtime artifacts outside the repository as required by AiStock.
    try:
        from utils.paths import runtime_path

        root = runtime_path("tdx_postclose_client")
    except Exception:
        root = Path(os.environ.get("AISTOCK_DATA_ROOT", "F:/Aistock/data")) / "runtime" / "tdx_postclose_client"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    path = root / f"report_{stamp}.json"
    payload["report_path"] = str(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def run(
    *,
    exe: Path = TDX_EXE_DEFAULT,
    tdx_root: Path = TDX_ROOT_DEFAULT,
    timeout: float = DOWNLOAD_TIMEOUT_SEC,
    dry_run: bool = False,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    report: dict[str, Any] = {
        "schema": "tdx_postclose_client/1",
        "status": "failure",
        "started_at": started.isoformat(),
        "exe": str(exe),
        "tdx_root": str(tdx_root),
        "dry_run": dry_run,
        "credentials_touched": False,
    }
    dialog: Optional[int] = None
    try:
        main, login_state = _login_if_needed(exe, min(timeout, 90.0))
        report["login_state"] = login_state
        report["main_hwnd"] = main
        dialog = _open_download_dialog(main, min(timeout, 30.0))
        report["download_dialog_hwnd"] = dialog
        report["selection"] = _configure_download(dialog)
        if dry_run:
            USER32.SendMessageW(dialog, WM_CLOSE, 0, 0)
            report["status"] = "dry_run"
            report["evidence"] = "controls_selected_without_starting_download"
            return report
        result = _start_and_wait(dialog, tdx_root, timeout)
        report.update(result)
        report["status"] = "success"
        USER32.SendMessageW(dialog, WM_CLOSE, 0, 0)
        return report
    except Exception as exc:
        report["reason"] = f"{type(exc).__name__}: {exc}"
        return report
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_report(report)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TDX 登录 + 盘后日线/5分钟线下载自动化")
    parser.add_argument("--tdx-exe", type=Path, default=TDX_EXE_DEFAULT)
    parser.add_argument("--tdx-root", type=Path, default=TDX_ROOT_DEFAULT)
    parser.add_argument("--timeout", type=float, default=DOWNLOAD_TIMEOUT_SEC)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="登录并选择日线/5分钟线，但不点击‘开始下载’",
    )
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    report = run(
        exe=args.tdx_exe,
        tdx_root=args.tdx_root,
        timeout=args.timeout,
        dry_run=args.dry_run,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["status"] in {"success", "dry_run"} else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
