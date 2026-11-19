# -*- coding: utf-8 -*-
r"""同花顺远航单账号冷启动引导 + 只读采集任务（#22：兼容冷启动、凭证零接触、绝不切号/绝不下单）。

安全铁律：
1. **凭证零接触（`credentials_touched` 恒为 `False`）**：
   - 绝不读取、写入、硬编码账号密码文本；
   - 交易面板登录页仅通过 `WM_GETTEXTLENGTH` 检查密码框（`aid=1012`）长度位是否 > 0，
     仅当客户端自身已记住密码且无未填验证码时才点击登录按钮（`aid=1006`）；
   - 若密码框为空、主程序停在「登录」窗未自动登录、或存在未填验证码，一律 fail-closed 返回
     `need_manual_login`，绝不强点或造数。
2. **绝不自动切号 / 绝不触碰下单页**：
   - 引导完成后全部只读采集仍交由 `ths_client.run_read_account(..., allow_switch=False)` 执行。
3. **产物原子落盘**：
   - 完整报告（含 `bootstrap` 引导轨迹与 `read_account` 只读结果）原子写入
     `F:\Aistock\data\data\runtime\ths_read_account\latest.json`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import ths_client as tc  # noqa: E402

BJ_TZ = timezone(timedelta(hours=8))
DEFAULT_HAPP_EXE = Path(r"D:\同花顺远航\同花顺远航版\bin\happ.exe")
DEFAULT_OUT_DIR = Path(r"F:\Aistock\data\data\runtime\ths_read_account")
WM_GETTEXTLENGTH = 0x000E


def _hwnd_text_length(hwnd: int) -> int:
    """仅读取 Win32 控件文本长度位（WM_GETTEXTLENGTH），绝不读取文本内容（凭证零接触）。"""
    if not hwnd or not tc.IS_WINDOWS or tc.USER32 is None:
        return 0
    try:
        return int(tc.USER32.SendMessageW(int(hwnd), WM_GETTEXTLENGTH, 0, 0))
    except Exception:  # noqa: BLE001
        return 0


def _inspect_panel_login_controls(panel_hwnd: int) -> dict[str, Any]:
    """只读探测交易面板登录页控件状态（密码框仅看长度位 > 0，绝不取值）。"""
    info: dict[str, Any] = {
        "is_login_page": False,
        "password_remembered": False,
        "captcha_empty": False,
        "login_button_hwnd": 0,
    }
    try:
        from comtypes.client import CreateObject, GetModule

        GetModule("UIAutomationCore.dll")
        import comtypes.gen.UIAutomationClient as UIAC

        uia = CreateObject(UIAC.CUIAutomation, interface=UIAC.IUIAutomation)
        root = uia.ElementFromHandle(int(panel_hwnd))
        found = root.FindAll(getattr(UIAC, "TreeScope_Descendants", 4), uia.CreateTrueCondition())
        ct_edit = getattr(UIAC, "UIA_EditControlTypeId", 50004)
        ct_button = getattr(UIAC, "UIA_ButtonControlTypeId", 50000)

        saw_account = False
        saw_password = False
        saw_login_btn = False
        for i in range(min(found.Length, 400)):
            el = found.GetElement(i)
            aid = str(getattr(el, "CurrentAutomationId", "") or "")
            ct = int(getattr(el, "CurrentControlType", 0) or 0)
            name = str(getattr(el, "CurrentName", "") or "")
            native_hwnd = int(getattr(el, "CurrentNativeWindowHandle", 0) or 0)
            if aid == "1001" and ct == ct_edit:
                saw_account = True
            elif aid == "1012" and ct == ct_edit:
                saw_password = True
                # 仅看长度位 > 0，绝不读密码文本
                info["password_remembered"] = _hwnd_text_length(native_hwnd) > 0
            elif aid == "1006" and ct == ct_button:
                saw_login_btn = True
                info["login_button_hwnd"] = native_hwnd
            elif ct == ct_edit and ("验证" in name or aid in ("1003", "1015")):
                if _hwnd_text_length(native_hwnd) == 0:
                    info["captcha_empty"] = True

        info["is_login_page"] = sum([saw_account, saw_password, saw_login_btn]) >= 2
    except Exception as exc:  # noqa: BLE001
        info["inspect_error"] = type(exc).__name__
    return info


def _click_panel_login_button(login_button_hwnd: int) -> dict[str, Any]:
    """点击交易面板登录按钮（仅在密码已记住且无空验证码时调用）。"""
    if not login_button_hwnd:
        return {"ok": False, "reason": "missing_login_button_hwnd"}
    left, top, right, bottom = tc._rect(int(login_button_hwnd))
    cx = (right - left) / 2.0
    cy = (bottom - top) / 2.0
    return {"ok": True, **tc._yh_click_window(int(login_button_hwnd), [cx, cy])}


def bootstrap_cold_start(
    *,
    happ_exe: Path = DEFAULT_HAPP_EXE,
    main_wait: float = 25.0,
    main_login_wait: float = 30.0,
    f12_wait: float = 12.0,
    login_settle_wait: float = 15.0,
) -> dict[str, Any]:
    """同花顺远航冷启动安全引导：拉起主程序 → 等待主界面 → F12 拉起面板 → 记住密码自动登录。

    返回值中 `ready=True` 表示客户端与交易面板已处于已登录态，可进入 `run_read_account` 只读采集。
    任何需要人工介入的场景（主程序停在登录窗、交易面板密码未记住或有未填验证码）一律 fail-closed。
    """
    steps: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "ready": False,
        "status": "bootstrap_failed",
        "credentials_touched": False,
        "interacted": False,
        "steps": steps,
    }

    # 1) 确保 happ.exe 主程序运行（冷启动或复用已运行实例）
    try:
        ensure_res = tc._yh_ensure_main(happ_exe=happ_exe, main_wait=main_wait)
        steps.append({"step": "ensure_main", **ensure_res})
        if ensure_res.get("launched"):
            result["interacted"] = True
    except Exception as exc:  # noqa: BLE001
        result["status"] = "main_launch_failed"
        result["reason"] = f"{type(exc).__name__}: {exc}"
        return result

    # 2) 等待主窗口出现；若停留在主程序「登录」框，等待其自动登录跳转，超时则 fail-closed
    main_win = tc._wait_for(tc._yh_main_window, min(main_wait, 15.0), 0.5)
    if main_win is None:
        result["status"] = "main_window_not_found"
        result["reason"] = "happ.exe 运行但未发现可见主窗口"
        return result
    steps.append({"step": "main_window", "hwnd": main_win["hwnd"], "title": main_win.get("title")})

    if "登录" in str(main_win.get("title") or ""):
        deadline = time.monotonic() + main_login_wait
        while time.monotonic() < deadline:
            time.sleep(0.8)
            cur = tc._yh_main_window()
            if cur is not None and "登录" not in str(cur.get("title") or ""):
                main_win = cur
                steps.append({"step": "main_auto_logged_in", "hwnd": cur["hwnd"], "title": cur.get("title")})
                break
        if "登录" in str(main_win.get("title") or ""):
            result["status"] = "need_manual_main_login"
            result["reason"] = (
                "同花顺远航主程序停在「登录」窗口（未开启自动登录），"
                "遵循凭证零接触铁律 fail-closed 退出；请手工登录一次并勾选自动登录"
            )
            return result

    # 3) 优先检查远航版三级只读进程链（happ -> xiadan -> xiadan-plus）是否已就绪；
    #    若已就绪则直接免 F12 进入单账号只读采集（零键鼠注入）。
    ro_chain = None
    try:
        ro_chain = tc._yh_ro_chain_snapshot(happ_exe)
    except Exception:  # noqa: BLE001
        ro_chain = None
    if ro_chain is not None:
        steps.append({
            "step": "ro_chain_ready",
            "happ_pid": ro_chain["happ"]["pid"],
            "xiadan_pid": ro_chain["xiadan"]["pid"],
            "plus_pid": ro_chain["plus"]["pid"],
        })
        result["ready"] = True
        result["status"] = "bootstrap_ready"
        return result

    # 4) 枚举交易面板；未开则前台化主程序并按 F12 拉起
    panels = tc._yh_enum_panels()
    if panels:
        panel = panels[0]
        steps.append({"step": "panel_already_open", "hwnd": panel["hwnd"]})
    else:
        fg = tc._yh_set_foreground(main_win["hwnd"])
        steps.append({"step": "main_foreground", **fg})
        time.sleep(0.3)
        panel, p_steps = tc._yh_ensure_panel(main_win["hwnd"], f12_wait=f12_wait, allow_f12=True)
        steps.extend(p_steps)
        result["interacted"] = True
        if panel is None:
            result["status"] = "panel_not_opened_after_f12"
            result["reason"] = "发送 F12 后仍未出现交易面板（可能主程序未完全就绪或需手动登录）"
            return result

    # 4) 检查交易面板是否停在登录页；仅当记住密码且无空验证码时才点登录
    login_info = _inspect_panel_login_controls(panel["hwnd"])
    steps.append({"step": "panel_login_inspect", **login_info})
    if login_info.get("is_login_page"):
        if not login_info.get("password_remembered"):
            result["status"] = "need_manual_panel_login"
            result["reason"] = "交易面板密码框为空（未记住密码），遵循凭证零接触铁律拒绝自动登录"
            return result
        if login_info.get("captcha_empty"):
            result["status"] = "need_manual_captcha"
            result["reason"] = "交易面板存在未填写验证码，fail-closed 等待人工登录"
            return result

        click_res = _click_panel_login_button(int(login_info.get("login_button_hwnd") or 0))
        steps.append({"step": "panel_login_click", **click_res})
        result["interacted"] = True
        if not click_res.get("ok"):
            result["status"] = "panel_login_click_failed"
            result["reason"] = str(click_res.get("reason") or "login_click_failed")
            return result

        # 等待交易面板离开登录页
        deadline = time.monotonic() + login_settle_wait
        logged_in = False
        while time.monotonic() < deadline:
            time.sleep(0.8)
            cur_panels = tc._yh_enum_panels()
            if cur_panels and not tc._yh_uia_login_page(cur_panels[0]["hwnd"]):
                logged_in = True
                steps.append({"step": "panel_logged_in", "hwnd": cur_panels[0]["hwnd"]})
                break
        if not logged_in:
            result["status"] = "panel_login_timeout"
            result["reason"] = "点击登录后交易面板仍停在登录页（可能需短信/验证码或密码已变更）"
            return result

    result["ready"] = True
    result["status"] = "bootstrap_ready"
    return result


def run_task(
    *,
    account: str = "acct1",
    happ_exe: Path = DEFAULT_HAPP_EXE,
    with_fills: bool = True,
    cold_start: bool = True,
    wait_idle_after_bootstrap: float = 0.0,
    out_dir: Path = DEFAULT_OUT_DIR,
) -> dict[str, Any]:
    """执行冷启动引导（可选）+ 单账号只读采集，并原子落盘报告。"""
    bootstrap: Optional[dict[str, Any]] = None
    if cold_start:
        bootstrap = bootstrap_cold_start(happ_exe=happ_exe)
        if not bootstrap.get("ready"):
            report = {
                "schema": tc.SCHEMA,
                "command": "read-account-task",
                "yuanhang": True,
                "generated_at": datetime.now(BJ_TZ).isoformat(),
                "credentials_touched": False,
                "account_requested": account,
                "status": bootstrap.get("status", "bootstrap_failed"),
                "rc": 4,
                "reason": bootstrap.get("reason"),
                "bootstrap": bootstrap,
                "read_account": None,
            }
            _write_report(out_dir, account, report)
            return report
        if bootstrap.get("interacted") and wait_idle_after_bootstrap > 0:
            time.sleep(wait_idle_after_bootstrap)

    read_rep = tc.run_read_account(
        account=account,
        happ_exe=happ_exe,
        allow_switch=False,
        with_fills=bool(with_fills),
    )
    report = {
        "schema": tc.SCHEMA,
        "command": "read-account-task",
        "yuanhang": True,
        "generated_at": datetime.now(BJ_TZ).isoformat(),
        "credentials_touched": False,
        "account_requested": account,
        "status": read_rep.get("status"),
        "rc": int(read_rep.get("rc", 1)),
        "reason": read_rep.get("reason"),
        "bootstrap": bootstrap,
        "read_account": read_rep,
    }
    _write_report(out_dir, account, report)
    return report


def _write_report(out_dir: Path, account: str, report: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BJ_TZ).strftime("%Y%m%d_%H%M%S")
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    dated = out_dir / f"read_account_{account}_{stamp}.json"
    dated.write_text(payload, encoding="utf-8")
    latest = out_dir / "latest.json"
    tmp = latest.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, latest)
    return latest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="同花顺远航单账号冷启动引导 + 只读采集任务（凭证零接触、绝不切号/绝不下单）"
    )
    parser.add_argument("--account", default="acct1", help="目标单账号（默认 acct1）")
    parser.add_argument("--happ-exe", type=Path, default=DEFAULT_HAPP_EXE, help="远航版主程序路径")
    parser.add_argument("--no-fills", action="store_true", help="跳过历史成交只读采集")
    parser.add_argument("--no-cold-start", action="store_true", help="禁用冷启动引导，仅做纯只读采集")
    parser.add_argument(
        "--wait-idle-after-bootstrap",
        type=float,
        default=92.0,
        help="冷启动交互后等待 Owner 守卫空闲窗口秒数（默认 92s；0 表示不等待）",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="报告输出目录")
    args = parser.parse_args(argv)

    rep = run_task(
        account=args.account,
        happ_exe=args.happ_exe,
        with_fills=not bool(args.no_fills),
        cold_start=not bool(args.no_cold_start),
        wait_idle_after_bootstrap=float(args.wait_idle_after_bootstrap),
        out_dir=args.out_dir,
    )
    return int(rep.get("rc", 1))


if __name__ == "__main__":
    raise SystemExit(main())
