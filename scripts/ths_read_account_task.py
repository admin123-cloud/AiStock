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


def _activate_and_raise_window(hwnd: int) -> dict[str, Any]:
    """激活目标窗口并短暂置顶后恢复（确保同花顺主窗/交易面板处于桌面最上层）。"""
    if not hwnd or not tc.IS_WINDOWS or tc.USER32 is None:
        return {"ok": False, "reason": "non_windows_or_invalid_hwnd"}
    h = int(hwnd)
    try:
        # SW_RESTORE = 9：若最小化先恢复
        tc.USER32.ShowWindow(h, 9)
        # 临时置顶抬升到桌面最上层再还原 NOTOPMOST（避免永久强占置顶）
        tc.USER32.SetWindowPos(
            h, 0xFFFFFFFF, 0, 0, 0, 0,
            tc.SWP_NOMOVE | tc.SWP_NOSIZE | tc.SWP_SHOWWINDOW,
        )
        time.sleep(0.1)
        fg = tc._yh_set_foreground(h)
        tc.USER32.SetWindowPos(
            h, 0xFFFFFFFE, 0, 0, 0, 0,
            tc.SWP_NOMOVE | tc.SWP_NOSIZE | tc.SWP_SHOWWINDOW,
        )
        return {"ok": True, "hwnd": h, "foreground": fg}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "hwnd": h, "reason": type(exc).__name__}


def _diagnose_readonly_bind(happ_exe: Path) -> dict[str, Any]:
    """当 run_read_account 返回 exception_fail_closed 时，只读诊断适配器绑定的具体拒绝原因。"""
    extra: dict[str, Any] = {}
    try:
        chain = tc._yh_ro_chain_snapshot(happ_exe)
        extra["chain"] = {k: chain[k]["pid"] for k in ("happ", "xiadan", "plus")}
        try:
            q_hwnd, q_rect = tc._yh_ro_find_query_window(chain["xiadan"]["pid"])
            extra["query_window"] = {"hwnd": q_hwnd, "rect": list(q_rect)}
        except Exception as q_exc:  # noqa: BLE001
            extra["query_window_error"] = str(q_exc)
        vis = []
        for h in sorted(tc._yh_ro_visible_windows(chain)):
            vis.append({
                "hwnd": h,
                "pid": tc._process_id(h),
                "rect": list(tc._rect(h)),
                "title": tc._window_text(h),
            })
        extra["visible_windows"] = vis
    except Exception:  # noqa: BLE001
        pass
    try:
        with tc._yh_readonly_lock(timeout_seconds=3.0) as acq:
            if acq is not True:
                return {"stage": "lock", "error": "lock_busy", **extra}
            tc._YhReadonlyAdapter(happ_exe=happ_exe)
            return {"stage": "adapter_init", "error": None, **extra}
    except Exception as exc:  # noqa: BLE001
        return {"stage": "adapter_init", "error_type": type(exc).__name__, "error": str(exc), **extra}


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
        # 确保同花顺交易面板（优先）或主窗口被激活并置于上层；
        # 若已在前台则零操作直接放行，避免无谓切换焦点重置系统空闲计时。
        existing_panels = tc._yh_enum_panels()
        target_hwnd = existing_panels[0]["hwnd"] if existing_panels else main_win["hwnd"]
        cur_fg = int(tc.USER32.GetForegroundWindow() or 0) if (tc.IS_WINDOWS and tc.USER32 is not None) else 0
        if cur_fg == int(target_hwnd):
            steps.append({"step": "already_foreground", "hwnd": int(target_hwnd)})
        else:
            raise_res = _activate_and_raise_window(target_hwnd)
            steps.append({"step": "activate_and_raise", **raise_res})
            result["interacted"] = True
        result["ready"] = True
        result["status"] = "bootstrap_ready"
        return result

    # 4) 枚举交易面板；未开则激活置顶主程序并按 F12 拉起
    panels = tc._yh_enum_panels()
    if panels:
        panel = panels[0]
        steps.append({"step": "panel_already_open", "hwnd": panel["hwnd"]})
        steps.append({"step": "activate_and_raise", **_activate_and_raise_window(panel["hwnd"])})
    else:
        steps.append({"step": "activate_and_raise_main", **_activate_and_raise_window(main_win["hwnd"])})
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
    if read_rep.get("status") == "exception_fail_closed":
        diag = _diagnose_readonly_bind(happ_exe)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "diagnostic.json").write_text(
            json.dumps(diag, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
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


def _switch_via_panel_combobox(
    target_account: str,
    happ_exe: Path,
) -> Optional[dict[str, Any]]:
    """通道 1：通过 xiadan 交易面板原生 ComboBox（services.ths_hotswitch）无盲点切号并双锚复验。

    返回 None 表示当前无法走面板 ComboBox（交由外层回落到 run_switch_account）；
    返回 dict（含 rc=0 或 rc!=0）表示已明确完成或 fail-closed 拒绝。
    """
    if not tc.IS_WINDOWS:
        return None
    meta = tc.YH_ACCOUNTS.get(target_account) or {}
    target_hint = str(meta.get("owner") or "").strip()
    if not target_hint:
        return None
    try:
        chain = tc._yh_ro_chain_snapshot(happ_exe)
        q_hwnd, _ = tc._yh_ro_find_query_window(chain["xiadan"]["pid"])
    except Exception:  # noqa: BLE001
        return None

    try:
        from pywinauto import Desktop
        from services import ths_hotswitch as hs

        win = Desktop(backend="win32").window(handle=int(q_hwnd))
        known_hints = [str(v.get("owner") or "") for v in tc.YH_ACCOUNTS.values() if v.get("owner")]
        err = hs.ensure_target_account(
            win,
            {"target_hint": target_hint, "known_hints": known_hints},
        )
        if err is not None:
            return {
                "schema": tc.SCHEMA,
                "command": "switch-account-combobox",
                "status": err.get("status") or "switch_failed",
                "rc": 3 if err.get("status") == "skipped_foreground" else 4,
                "to": target_account,
                "reason": err.get("note") or err.get("reason") or "combobox_switch_failed",
                "detail": err,
                "clicks": [],
                "credentials_touched": False,
            }
        # 切换成功后必须经只读身份锚点二次独立复验，绝不串账
        time.sleep(1.5)
        verified = _detect_active_account(happ_exe)
        if verified != target_account:
            return {
                "schema": tc.SCHEMA,
                "command": "switch-account-combobox",
                "status": "switch_unverified",
                "rc": 2,
                "to": target_account,
                "verified_account": verified,
                "reason": f"ComboBox 切换后身份复验不匹配: got={verified!r} expect={target_account!r}",
                "clicks": [{"target": f"combobox:{target_account}"}],
                "credentials_touched": False,
            }
        return {
            "schema": tc.SCHEMA,
            "command": "switch-account-combobox",
            "status": "switched",
            "rc": 0,
            "to": target_account,
            "verified_account": verified,
            "clicks": [{"target": f"combobox:{target_account}"}],
            "credentials_touched": False,
        }
    except Exception:  # noqa: BLE001
        return None


def _detect_active_account(happ_exe: Path) -> Optional[str]:
    """只读探测当前交易面板已登录的账号键（acct1/acct2）；不可读返回 None。"""
    try:
        chain = tc._yh_ro_chain_snapshot(happ_exe)
        q_hwnd, _ = tc._yh_ro_find_query_window(chain["xiadan"]["pid"])
        vals = tc._yh_ro_identity_values(q_hwnd)
        if not vals:
            return None
        resolved = tc._yh_ro_resolve_identity(vals)
        if isinstance(resolved, dict):
            return resolved.get("account")
    except Exception:  # noqa: BLE001
        return None
    return None


def _write_named_report(out_dir: Path, filename: str, report: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    target = out_dir / filename
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)
    return target


def run_multi_account_task(
    *,
    accounts: list[str],
    happ_exe: Path = DEFAULT_HAPP_EXE,
    with_fills: bool = True,
    cold_start: bool = True,
    switch_dry_run: bool = False,
    wait_idle_after_bootstrap: float = 92.0,
    out_dir: Path = DEFAULT_OUT_DIR,
) -> dict[str, Any]:
    """双账号/多账号串行安全采集：冷启动引导 → 逐账号身份核验/受控切换 → 单账号只读采集 → 分账落盘。

    安全铁律：
    - 切号失败或未验证（rc != 0）立即 fail-closed，绝不将上一账号数据归属到目标账号；
    - 每次切号均通过 `ths_client.run_switch_account`（含 Owner 守卫 + 切换后脱敏账号复验）；
    - `switch_dry_run=True` 时仅验证菜单定位，零真实点击切号。
    """
    ordered = [a.strip() for a in accounts if a and a.strip()]
    if not ordered:
        raise ValueError("accounts 不能为空")

    bootstrap: Optional[dict[str, Any]] = None
    if cold_start:
        bootstrap = bootstrap_cold_start(happ_exe=happ_exe)
        if not bootstrap.get("ready"):
            summary = {
                "schema": tc.SCHEMA,
                "command": "read-multi-account-task",
                "yuanhang": True,
                "generated_at": datetime.now(BJ_TZ).isoformat(),
                "credentials_touched": False,
                "accounts_requested": ordered,
                "switch_dry_run": bool(switch_dry_run),
                "status": bootstrap.get("status", "bootstrap_failed"),
                "rc": 4,
                "reason": bootstrap.get("reason"),
                "bootstrap": bootstrap,
                "accounts": {},
            }
            _write_named_report(out_dir, "latest_multi.json", summary)
            return summary
        if bootstrap.get("interacted") and wait_idle_after_bootstrap > 0:
            time.sleep(wait_idle_after_bootstrap)

    # 若当前活跃账号在目标列表中，优先采集当前账号，减少一次切号
    active_now = _detect_active_account(happ_exe)
    if active_now in ordered and ordered[0] != active_now:
        ordered = [active_now] + [a for a in ordered if a != active_now]

    per_account: dict[str, dict[str, Any]] = {}
    all_ok = True
    for acct in ordered:
        cur = _detect_active_account(happ_exe)
        switch_rep: Optional[dict[str, Any]] = None
        if cur != acct:
            if not switch_dry_run:
                switch_rep = _switch_via_panel_combobox(acct, happ_exe)
            if switch_rep is None:
                switch_rep = tc.run_switch_account(
                    to=acct,
                    happ_exe=happ_exe,
                    dry_run=bool(switch_dry_run),
                )
            sw_rc = int(switch_rep.get("rc", 1))
            if switch_dry_run:
                per_account[acct] = {
                    "account": acct,
                    "status": switch_rep.get("status"),
                    "rc": sw_rc,
                    "switch": switch_rep,
                    "read_account": None,
                }
                _write_named_report(out_dir, f"latest_{acct}.json", per_account[acct])
                if sw_rc != 0:
                    all_ok = False
                # 若本次 dry-run 内部发送了 F12 拉起面板，避免后续账号立即命中自身造成的 owner_active
                steps_list = switch_rep.get("steps") or []
                if any(isinstance(s, dict) and s.get("step") == "f12_send" for s in steps_list):
                    if wait_idle_after_bootstrap > 0:
                        time.sleep(wait_idle_after_bootstrap)
                continue
            if sw_rc != 0:
                all_ok = False
                per_account[acct] = {
                    "account": acct,
                    "status": switch_rep.get("status") or "switch_failed",
                    "rc": sw_rc,
                    "reason": switch_rep.get("reason") or "switch_account_fail_closed",
                    "switch": switch_rep,
                    "read_account": None,
                }
                _write_named_report(out_dir, f"latest_{acct}.json", per_account[acct])
                continue
            # 切号成功或内部发送了 F12 产生真实键鼠注入后，等待 Owner 守卫空闲窗口再只读采集
            sw_steps = switch_rep.get("steps") or []
            did_interact = bool(switch_rep.get("clicks")) or any(
                isinstance(s, dict) and s.get("step") == "f12_send" for s in sw_steps
            )
            if did_interact and wait_idle_after_bootstrap > 0:
                time.sleep(wait_idle_after_bootstrap)

        read_rep = tc.run_read_account(
            account=acct,
            happ_exe=happ_exe,
            allow_switch=False,
            with_fills=bool(with_fills),
        )
        r_rc = int(read_rep.get("rc", 1))
        if r_rc != 0:
            all_ok = False
        entry = {
            "account": acct,
            "status": read_rep.get("status"),
            "rc": r_rc,
            "reason": read_rep.get("reason"),
            "switch": switch_rep,
            "read_account": read_rep,
        }
        per_account[acct] = entry
        _write_named_report(out_dir, f"latest_{acct}.json", entry)

    summary = {
        "schema": tc.SCHEMA,
        "command": "read-multi-account-task",
        "yuanhang": True,
        "generated_at": datetime.now(BJ_TZ).isoformat(),
        "credentials_touched": False,
        "accounts_requested": ordered,
        "switch_dry_run": bool(switch_dry_run),
        "status": "ok" if all_ok else "partial_or_failed",
        "rc": 0 if all_ok else 4,
        "bootstrap": bootstrap,
        "accounts": per_account,
    }
    stamp = datetime.now(BJ_TZ).strftime("%Y%m%d_%H%M%S")
    _write_named_report(out_dir, f"read_multi_{stamp}.json", summary)
    _write_named_report(out_dir, "latest_multi.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="同花顺远航单/双账号冷启动引导 + 只读采集任务（凭证零接触、绝不下单）"
    )
    parser.add_argument("--account", default="acct1", help="目标单账号（默认 acct1）")
    parser.add_argument(
        "--accounts",
        default=None,
        help="多账号逗号列表（如 acct1,acct2）；指定时启用双账号受控切换采集模式",
    )
    parser.add_argument(
        "--switch-dry-run",
        action="store_true",
        help="多账号模式下切号仅做菜单定位干跑（零真实点击）",
    )
    parser.add_argument("--happ-exe", type=Path, default=DEFAULT_HAPP_EXE, help="远航版主程序路径")
    parser.add_argument("--no-fills", action="store_true", help="跳过历史成交只读采集")
    parser.add_argument("--no-cold-start", action="store_true", help="禁用冷启动引导，仅做纯只读采集")
    parser.add_argument(
        "--wait-idle-after-bootstrap",
        type=float,
        default=92.0,
        help="冷启动/切号交互后等待 Owner 守卫空闲窗口秒数（默认 92s；0 表示不等待）",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="报告输出目录")
    args = parser.parse_args(argv)

    if args.accounts:
        acct_list = [x.strip() for x in str(args.accounts).split(",") if x.strip()]
        rep = run_multi_account_task(
            accounts=acct_list,
            happ_exe=args.happ_exe,
            with_fills=not bool(args.no_fills),
            cold_start=not bool(args.no_cold_start),
            switch_dry_run=bool(args.switch_dry_run),
            wait_idle_after_bootstrap=float(args.wait_idle_after_bootstrap),
            out_dir=args.out_dir,
        )
        return int(rep.get("rc", 1))

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
