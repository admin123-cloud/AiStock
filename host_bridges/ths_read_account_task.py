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
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import ths_client as tc  # noqa: E402

BJ_TZ = timezone(timedelta(hours=8))
DEFAULT_HAPP_EXE = Path(r"D:\同花顺远航\同花顺远航版\bin\happ.exe")
DEFAULT_OUT_DIR = Path(r"F:\Aistock\runtime\ths_read_account")
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
    #    仅当进程链就绪、存在可见交易面板且未停在登录页时，才免 F12 进入快速通道。
    #    若 xiadan 仅驻留后台但交易面板已关闭/隐藏，必须继续走步骤 4 按 F12 拉起面板。
    ro_chain = None
    try:
        ro_chain = tc._yh_ro_chain_snapshot(happ_exe)
    except Exception:  # noqa: BLE001
        ro_chain = None
    if ro_chain is not None:
        existing_panels = tc._yh_enum_panels()
        if existing_panels and not tc._yh_uia_login_page(existing_panels[0]["hwnd"]):
            steps.append({
                "step": "ro_chain_ready",
                "happ_pid": ro_chain["happ"]["pid"],
                "xiadan_pid": ro_chain["xiadan"]["pid"],
                "plus_pid": ro_chain["plus"]["pid"],
                "panel_hwnd": existing_panels[0]["hwnd"],
            })
            # 面板已可见且不在登录页：仅在面板已在前台或被最小化时按需处理，
            # 不再把主程序窗口误当交易面板强拉前台，避免无谓触发 Owner 活跃守卫。
            target_hwnd = int(existing_panels[0]["hwnd"])
            cur_fg = int(tc.USER32.GetForegroundWindow() or 0) if (tc.IS_WINDOWS and tc.USER32 is not None) else 0
            is_iconic = bool(tc.USER32.IsIconic(target_hwnd)) if (tc.IS_WINDOWS and tc.USER32 is not None) else False
            if cur_fg == target_hwnd:
                steps.append({"step": "already_foreground", "hwnd": target_hwnd})
            elif is_iconic:
                raise_res = _activate_and_raise_window(target_hwnd)
                steps.append({"step": "activate_and_raise", **raise_res})
                result["interacted"] = True
            else:
                steps.append({"step": "panel_visible_no_raise", "hwnd": target_hwnd})
            result["ready"] = True
            result["status"] = "bootstrap_ready"
            return result
        steps.append({
            "step": "ro_chain_panel_not_ready",
            "happ_pid": ro_chain["happ"]["pid"],
            "xiadan_pid": ro_chain["xiadan"]["pid"],
            "plus_pid": ro_chain["plus"]["pid"],
            "panels_found": len(existing_panels),
        })

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


def _effective_idle_threshold(min_idle_seconds: Optional[float] = None) -> float:
    if min_idle_seconds is None:
        return float(tc.YH_OWNER_ACTIVE_IDLE_S)
    return max(1.0, float(min_idle_seconds))


def _owner_guard_active(
    *,
    min_idle_seconds: Optional[float] = None,
    require_global_idle: bool = False,
) -> bool:
    """检查同花顺或系统是否正处于 Owner 活跃守卫态。

    - require_global_idle=True：与 `_YhReadonlyAdapter` 绑定条件对齐，要求全系统键鼠空闲 >= 阈值；
    - require_global_idle=False：检查同花顺主窗/交易面板是否在前台且输入空闲 < 阈值。
    """
    if not tc.IS_WINDOWS:
        return False
    threshold = _effective_idle_threshold(min_idle_seconds)
    if require_global_idle:
        try:
            idle = float(tc._yh_owner_idle_seconds())
            if not (0.0 <= idle < float("inf")) or idle < threshold:
                return True
        except Exception:  # noqa: BLE001
            return True
    guard_hwnds: list[int] = []
    try:
        main = tc._yh_main_window()
        if isinstance(main, dict) and main.get("hwnd"):
            guard_hwnds.append(int(main["hwnd"]))
    except Exception:  # noqa: BLE001
        pass
    try:
        for panel in tc._yh_enum_panels():
            if isinstance(panel, dict) and panel.get("hwnd"):
                guard_hwnds.append(int(panel["hwnd"]))
    except Exception:  # noqa: BLE001
        pass
    if not guard_hwnds:
        return False
    return any(tc._yh_owner_using(h, min_idle_seconds=threshold) for h in guard_hwnds)


def _wait_for_owner_guard_clear(
    max_wait_seconds: float,
    *,
    poll_interval: Optional[float] = None,
    min_idle_seconds: Optional[float] = None,
    require_global_idle: bool = True,
    on_heartbeat: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """动态轮询等待 Owner 活跃守卫放行，取代固定盲睡。

    - 后台定时任务默认 90s 门禁；网页手动点击触发使用 5s 短门禁；
    - 若用户期间移动鼠标/按键，计时自动重新累计，等待至连续空闲满阈值后立刻放行；
    - 等待期间通过 on_heartbeat 回调持续保活桌面桥心跳，避免前端误判离线。
    """
    budget = float(max_wait_seconds or 0.0)
    if budget <= 0.0 or not tc.IS_WINDOWS:
        return {"cleared": True, "waited_seconds": 0.0, "reason": "disabled_or_non_windows"}
    threshold = _effective_idle_threshold(min_idle_seconds)
    started = time.monotonic()
    deadline = started + budget
    interval = (
        max(0.2, float(poll_interval))
        if poll_interval is not None
        else (0.5 if threshold <= 10.0 else 2.0)
    )
    last_hb = 0.0
    while True:
        if not _owner_guard_active(
            min_idle_seconds=threshold,
            require_global_idle=require_global_idle,
        ):
            return {
                "cleared": True,
                "waited_seconds": round(max(0.0, time.monotonic() - started), 2),
                "threshold_seconds": threshold,
                "reason": "guard_cleared",
            }
        now_m = time.monotonic()
        remaining = deadline - now_m
        if remaining <= 0.0:
            return {
                "cleared": False,
                "waited_seconds": round(max(0.0, now_m - started), 2),
                "threshold_seconds": threshold,
                "reason": "owner_still_active_timeout",
            }
        if on_heartbeat is not None and (now_m - last_hb >= 2.0 or last_hb == 0.0):
            try:
                on_heartbeat()
                last_hb = now_m
            except Exception:  # noqa: BLE001
                pass
        time.sleep(min(interval, remaining))


def run_task(
    *,
    account: str = "acct1",
    happ_exe: Path = DEFAULT_HAPP_EXE,
    with_fills: bool = True,
    cold_start: bool = True,
    wait_idle_after_bootstrap: float = 0.0,
    out_dir: Path = DEFAULT_OUT_DIR,
    on_heartbeat: Optional[Callable[[], None]] = None,
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
            _wait_for_owner_guard_clear(wait_idle_after_bootstrap, on_heartbeat=on_heartbeat)

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
    *,
    min_idle_seconds: Optional[float] = None,
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
        from services import ths_hotswitch as hs

        eff_idle = _effective_idle_threshold(min_idle_seconds)
        # 前置 Owner 离席守卫（与 ths_hotswitch / ths_client 同源：仅当交易面板在前台且输入空闲 < 阈值才让位）
        if tc._yh_owner_using(int(q_hwnd), min_idle_seconds=eff_idle):
            return {
                "schema": tc.SCHEMA,
                "command": "switch-account-combobox",
                "status": "skipped_foreground",
                "rc": 3,
                "to": target_account,
                "reason": f"Owner 在交易面板上（前台+输入空闲<{int(eff_idle)}s），绝不切换",
                "clicks": [],
                "credentials_touched": False,
            }

        sel = hs._uia_select_account(int(q_hwnd), target_hint, min_idle_seconds=eff_idle)
        if not isinstance(sel, dict) or not sel.get("ok"):
            return {
                "schema": tc.SCHEMA,
                "command": "switch-account-combobox",
                "status": (sel.get("reason") if isinstance(sel, dict) else None) or "switch_failed",
                "rc": 4,
                "to": target_account,
                "reason": str((sel or {}).get("reason") or "uia_select_account_failed"),
                "detail": sel,
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
                "detail": sel,
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
            "detail": sel,
            "clicks": [{"target": f"combobox:{target_account}"}],
            "credentials_touched": False,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "schema": tc.SCHEMA,
            "command": "switch-account-combobox",
            "status": "combobox_exception",
            "rc": 4,
            "to": target_account,
            "reason": f"{type(exc).__name__}: {exc}",
            "clicks": [],
            "credentials_touched": False,
        }


def _match_account_from_text(text: str) -> Optional[str]:
    """根据交易面板身份文本（脱敏账号或顶栏标签）唯一匹配 YH_ACCOUNTS 中的账号键。"""
    norm = str(text or "").strip()
    if not norm:
        return None
    hits: list[str] = []
    for acct, meta in tc.YH_ACCOUNTS.items():
        masked = str(meta.get("masked_id") or "")
        label = str(meta.get("label") or "")
        owner = str(meta.get("owner") or "")
        if (masked and masked in norm) or (label and label in norm) or (owner and owner in norm):
            hits.append(acct)
    return hits[0] if len(hits) == 1 else None


def _detect_active_account(happ_exe: Path) -> Optional[str]:
    """只读探测当前交易面板已登录的账号键（acct1/acct2）；不可读返回 None。"""
    if not tc.IS_WINDOWS:
        return None
    try:
        chain = tc._yh_ro_chain_snapshot(happ_exe)
        q_hwnd, _ = tc._yh_ro_find_query_window(chain["xiadan"]["pid"])
    except Exception:  # noqa: BLE001
        return None
    # 1) 优先通过 _yh_ro_identity_values 读顶栏脱敏账号与标签
    try:
        vals = tc._yh_ro_identity_values(q_hwnd)
        if isinstance(vals, dict) and vals:
            joined = " ".join(str(v) for v in vals.values() if v)
            matched = _match_account_from_text(joined)
            if matched:
                return matched
    except Exception:  # noqa: BLE001
        pass
    # 2) 兜底通过 ths_hotswitch 读取交易面板原生 ComboBox 文本
    try:
        from pywinauto import Desktop
        from services import ths_hotswitch as hs

        win = Desktop(backend="win32").window(handle=int(q_hwnd))
        label, _png = hs.read_window_identity(win)
        return _match_account_from_text(label)
    except Exception:  # noqa: BLE001
        return None


def _select_query_tree_node(hwnd: int, page_name: str) -> dict[str, Any]:
    """通过 UIA SelectionItem.Select() 选中左侧查询树（SysTreeView32 aid=129）中的指定只读查询节点。"""
    if page_name not in ("资金股票", "当日成交", "历史成交"):
        return {"ok": False, "reason": "page_not_allowed"}
    UIAC, uia, _root, items, elements = tc._yh_ro_uia_scan(int(hwnd))
    for item, el in zip(items, elements):
        if item.get("ct") != tc._YH_RO_UIA_CT["treeitem"] or item.get("name") != page_name:
            continue
        if not item.get("enabled") or not tc._yh_ro_item_visible(item):
            continue
        parents = tc._yh_ro_parents(uia, el, 3)
        in_query_tree = any(
            int(getattr(p, "CurrentControlType", 0) or 0) == tc._YH_RO_UIA_CT["tree"]
            and str(getattr(p, "CurrentClassName", "") or "") == tc._YH_RO_TREE_CLS
            and str(getattr(p, "CurrentAutomationId", "") or "") == tc._YH_RO_TREE_AID
            for p in parents
        )
        if not in_query_tree:
            continue
        try:
            pattern = el.GetCurrentPattern(UIAC.UIA_SelectionItemPatternId)
            if pattern is None:
                return {"ok": False, "reason": "selection_pattern_unsupported"}
            sel = pattern.QueryInterface(UIAC.IUIAutomationSelectionItemPattern)
            sel.Select()
            return {"ok": True, "reason": None}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"selection_refused:{type(exc).__name__}"}
    return {"ok": False, "reason": "query_item_not_found"}


def _read_today_fills_for_account(
    account: str,
    happ_exe: Path,
    *,
    today_str: Optional[str] = None,
) -> dict[str, Any]:
    """只读采集当前交易面板「当日成交」页（零键鼠注入、身份前后双锚核验、采毕切回「资金股票」页）。"""
    day_compact = (today_str or datetime.now(BJ_TZ).strftime("%Y%m%d")).replace("-", "")
    empty: dict[str, Any] = {
        "attempted": False,
        "status": "skipped",
        "reason": "non_windows",
        "date": day_compact,
        "rows": [],
        "row_count": 0,
    }
    if not tc.IS_WINDOWS:
        return empty
    try:
        chain = tc._yh_ro_chain_snapshot(happ_exe)
        q_hwnd, _ = tc._yh_ro_find_query_window(chain["xiadan"]["pid"])
    except Exception as exc:  # noqa: BLE001
        return {**empty, "attempted": True, "status": "unavailable", "reason": f"panel_bind_failed:{type(exc).__name__}"}

    if _detect_active_account(happ_exe) != account:
        return {**empty, "attempted": True, "status": "identity_mismatch", "reason": "pre_identity_mismatch"}

    sel = _select_query_tree_node(int(q_hwnd), "当日成交")
    if not sel.get("ok"):
        return {**empty, "attempted": True, "status": "unavailable", "reason": str(sel.get("reason") or "select_failed")}

    try:
        time.sleep(0.7)
        _UIAC, _uia, _root, items, _elements = tc._yh_ro_uia_scan(int(q_hwnd))
        grids = [
            it for it in items
            if it.get("ct") == tc._YH_RO_UIA_CT["pane"]
            and it.get("cls") == tc._YH_RO_GRID_CLS
            and it.get("aid") == tc._YH_RO_GRID_AID
            and tc._yh_ro_item_visible(it)
            and it.get("win")
        ]
        if len(grids) != 1:
            return {**empty, "attempted": True, "status": "unavailable", "reason": f"grid_not_unique:{len(grids)}"}
        ocr_items, row_delta, _meta = tc._yh_ro_grid_items(int(grids[0]["win"]))
        raw_rows = tc.yh_fills_from_items(ocr_items, row_delta=row_delta, min_code_x=0.0)

        # 后置身份复验：绝不串账
        if _detect_active_account(happ_exe) != account:
            return {**empty, "attempted": True, "status": "identity_mismatch", "reason": "post_identity_mismatch"}

        rows: list[dict[str, Any]] = []
        for r in raw_rows:
            if not isinstance(r, dict):
                continue
            item = dict(r)
            if not str(item.get("date") or "").strip():
                item["date"] = day_compact
            item["source"] = "today_fills"
            rows.append(item)
        return {
            "attempted": True,
            "status": "ok",
            "reason": None,
            "date": day_compact,
            "rows": rows,
            "row_count": len(rows),
        }
    except Exception as exc:  # noqa: BLE001
        return {**empty, "attempted": True, "status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        # 必须切回「资金股票」安全基线页，确保下一次 _YhReadonlyAdapter 绑定不被拒绝
        try:
            _select_query_tree_node(int(q_hwnd), "资金股票")
            time.sleep(0.3)
        except Exception:  # noqa: BLE001
            pass


def _merge_today_fills_into_read_report(
    read_rep: dict[str, Any],
    today_rep: dict[str, Any],
) -> None:
    """将「当日成交」完整合并至 read_rep['fills']['rows']（当日成交置前，绝不对同价同量拆单误去重）。"""
    read_rep["today_fills"] = today_rep
    today_rows = [
        dict(r) for r in (today_rep.get("rows") or []) if isinstance(r, dict)
    ] if isinstance(today_rep, dict) else []
    if not today_rows:
        return
    fills_obj = read_rep.get("fills")
    if not isinstance(fills_obj, dict):
        fills_obj = {"attempted": True, "rows": [], "status": "partial", "row_count": 0}
        read_rep["fills"] = fills_obj
    existing_rows = list(fills_obj.get("rows") or [])

    # 1) 当日成交页的每一行都是独立撮合（同一秒同价同量拆单如 200+200+100+100+100+1300=2000 必须全部保留）
    today_dates: set[str] = set()
    merged: list[dict[str, Any]] = []
    for seq, row in enumerate(today_rows, start=1):
        d_norm = str(row.get("date") or "").replace("-", "").strip()
        if d_norm:
            today_dates.add(d_norm)
        row.setdefault("row_seq", seq)
        merged.append(row)

    # 2) 历史成交页中只保留非当日的记录，避免跨页重复；同时保留历史页内独立拆单
    for row in existing_rows:
        if not isinstance(row, dict):
            continue
        d_norm = str(row.get("date") or "").replace("-", "").strip()
        if d_norm and d_norm in today_dates:
            continue
        merged.append(row)

    fills_obj["rows"] = merged
    fills_obj["row_count"] = len(merged)
    fills_obj["today_row_count"] = len(today_rows)
    if fills_obj.get("status") in (None, "unavailable"):
        fills_obj["status"] = "partial"


def _minimize_ths_windows() -> dict[str, Any]:
    """同步收尾动作：将同花顺交易面板与主程序窗口安全最小化（SW_MINIMIZE=6，绝不关闭进程）。"""
    if not tc.IS_WINDOWS or tc.USER32 is None:
        return {"ok": False, "minimized_hwnds": [], "reason": "non_windows"}
    minimized: list[int] = []
    seen: set[int] = set()
    targets: list[int] = []
    try:
        for panel in tc._yh_enum_panels():
            h = int((panel or {}).get("hwnd") or 0)
            if h and h not in seen:
                seen.add(h)
                targets.append(h)
    except Exception:  # noqa: BLE001
        pass
    try:
        main = tc._yh_main_window()
        h = int((main or {}).get("hwnd") or 0)
        if h and h not in seen:
            seen.add(h)
            targets.append(h)
    except Exception:  # noqa: BLE001
        pass
    for hwnd in targets:
        try:
            tc.USER32.ShowWindow(int(hwnd), 6)  # SW_MINIMIZE = 6
            minimized.append(int(hwnd))
        except Exception:  # noqa: BLE001
            pass
    return {"ok": bool(minimized), "minimized_hwnds": minimized}


def _write_named_report(out_dir: Path, filename: str, report: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    target = out_dir / filename
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)
    return target


@contextmanager
def _scoped_ro_idle_ms(min_idle_seconds: Optional[float]):
    """在手动触发只读采集期间临时调整 ths_client._YH_RO_OWNER_IDLE_MS，退出时无条件恢复 90s 默认值。"""
    if min_idle_seconds is None:
        yield
        return
    prev_ms = getattr(tc, "_YH_RO_OWNER_IDLE_MS", 90_000)
    tc._YH_RO_OWNER_IDLE_MS = int(round(max(1.0, float(min_idle_seconds)) * 1000))
    try:
        yield
    finally:
        tc._YH_RO_OWNER_IDLE_MS = prev_ms


def _is_owner_interruption(
    status: Any,
    reason: Any = None,
    diag: Optional[dict[str, Any]] = None,
) -> bool:
    """判断本次切号/采集失败是否因用户操作鼠键触发 Owner 守卫中断（可等待空闲后重试）。"""
    st = str(status or "").strip().lower()
    rs = str(reason or "").strip().lower()
    if st in {"owner_active", "skipped_foreground", "guard_fail_closed", "owner_input_activity"}:
        return True
    if "owner_active" in rs or "owner input active" in rs or "owner_input_activity" in rs:
        return True
    if isinstance(diag, dict):
        err = str(diag.get("error") or "").lower()
        if "owner input active" in err or "owner_input_activity" in err:
            return True
    return False


def _is_retryable_switch_glitch(status: Any, reason: Any = None) -> bool:
    """判断切号失败是否属于下拉框失焦/未提交类瞬态抖动（可原地等待后重试，绝不重试未登录/未知账号等硬门禁）。"""
    st = str(status or "").strip().lower()
    rs = str(reason or "").strip().lower()
    transient = {"select_not_committed", "dropdown_not_expanded"}
    return st in transient or rs in transient


def run_multi_account_task(
    *,
    accounts: list[str],
    happ_exe: Path = DEFAULT_HAPP_EXE,
    with_fills: bool = True,
    cold_start: bool = True,
    switch_dry_run: bool = False,
    wait_idle_after_bootstrap: float = 180.0,
    out_dir: Path = DEFAULT_OUT_DIR,
    on_heartbeat: Optional[Callable[[], None]] = None,
    min_idle_seconds: Optional[float] = None,
    max_owner_retries: int = 3,
    minimize_after_sync: bool = False,
) -> dict[str, Any]:
    """双账号/多账号串行安全采集：冷启动引导 → 逐账号身份核验/受控切换 → 单账号只读采集 → 分账落盘。

    安全铁律：
    - 切号失败或未验证（rc != 0）立即 fail-closed，绝不将上一账号数据归属到目标账号；
    - 每次切号均通过原生 ComboBox 或 `ths_client.run_switch_account`（含 Owner 守卫 + 切换后脱敏账号复验）；
    - 后台定时任务默认 90s 守卫；网页手动触发（min_idle_seconds=5s）遇用户操作鼠标时自动等待 5s 连续空闲后重试；
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
            guard_wait = _wait_for_owner_guard_clear(
                wait_idle_after_bootstrap,
                min_idle_seconds=min_idle_seconds,
                require_global_idle=True,
                on_heartbeat=on_heartbeat,
            )
            bootstrap["owner_guard_wait"] = guard_wait

    # 若当前活跃账号在目标列表中，优先采集当前账号，减少一次切号
    active_now = _detect_active_account(happ_exe)
    if active_now in ordered and ordered[0] != active_now:
        ordered = [active_now] + [a for a in ordered if a != active_now]

    per_account: dict[str, dict[str, Any]] = {}
    all_ok = True
    retries_limit = max(0, int(max_owner_retries)) if wait_idle_after_bootstrap > 0 else 0

    for acct in ordered:
        if on_heartbeat is not None:
            try:
                on_heartbeat()
            except Exception:  # noqa: BLE001
                pass

        # ── 阶段 1：账号切换阶段（原地重试，成功后不再重跑切号） ──
        switch_rep: Optional[dict[str, Any]] = None
        switch_retries = 0
        switch_failed_entry: Optional[dict[str, Any]] = None
        skip_read_dry_run = False

        cur = _detect_active_account(happ_exe)
        if cur != acct:
            while True:
                if on_heartbeat is not None:
                    try:
                        on_heartbeat()
                    except Exception:  # noqa: BLE001
                        pass
                # 切号前若 Owner 正在操作，动态等待守卫放行
                if not switch_dry_run and wait_idle_after_bootstrap > 0 and _owner_guard_active(
                    min_idle_seconds=min_idle_seconds,
                    require_global_idle=True,
                ):
                    _wait_for_owner_guard_clear(
                        wait_idle_after_bootstrap,
                        min_idle_seconds=min_idle_seconds,
                        require_global_idle=True,
                        on_heartbeat=on_heartbeat,
                    )
                if not switch_dry_run:
                    if min_idle_seconds is not None:
                        switch_rep = _switch_via_panel_combobox(
                            acct, happ_exe, min_idle_seconds=min_idle_seconds
                        )
                    else:
                        switch_rep = _switch_via_panel_combobox(acct, happ_exe)
                if switch_rep is None:
                    sw_kwargs: dict[str, Any] = {
                        "to": acct,
                        "happ_exe": happ_exe,
                        "dry_run": bool(switch_dry_run),
                    }
                    if min_idle_seconds is not None:
                        sw_kwargs["min_idle_seconds"] = min_idle_seconds
                    switch_rep = tc.run_switch_account(**sw_kwargs)
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
                    steps_list = switch_rep.get("steps") or []
                    if any(isinstance(s, dict) and s.get("step") == "f12_send" for s in steps_list):
                        if wait_idle_after_bootstrap > 0:
                            _wait_for_owner_guard_clear(
                                wait_idle_after_bootstrap,
                                min_idle_seconds=min_idle_seconds,
                                require_global_idle=True,
                                on_heartbeat=on_heartbeat,
                            )
                    skip_read_dry_run = True
                    break
                if sw_rc != 0:
                    can_retry_switch = switch_retries < retries_limit and (
                        _is_owner_interruption(switch_rep.get("status"), switch_rep.get("reason"))
                        or _is_retryable_switch_glitch(switch_rep.get("status"), switch_rep.get("reason"))
                    )
                    if can_retry_switch:
                        switch_retries += 1
                        _wait_for_owner_guard_clear(
                            wait_idle_after_bootstrap,
                            min_idle_seconds=min_idle_seconds,
                            require_global_idle=True,
                            on_heartbeat=on_heartbeat,
                        )
                        # 等待后再核验一次当前账号：若已落位目标账号则直接进入采集阶段
                        if _detect_active_account(happ_exe) == acct:
                            break
                        continue
                    all_ok = False
                    switch_failed_entry = {
                        "account": acct,
                        "status": switch_rep.get("status") or "switch_failed",
                        "rc": sw_rc,
                        "reason": switch_rep.get("reason") or "switch_account_fail_closed",
                        "switch_retries": switch_retries,
                        "read_retries": 0,
                        "owner_retries": switch_retries,
                        "switch": switch_rep,
                        "read_account": None,
                    }
                    per_account[acct] = switch_failed_entry
                    _write_named_report(out_dir, f"latest_{acct}.json", switch_failed_entry)
                    break
                # 切号成功或内部发送了 F12 产生真实键鼠注入后，动态等待空闲达标再进入采集阶段
                sw_steps = switch_rep.get("steps") or []
                did_interact = bool(switch_rep.get("clicks")) or any(
                    isinstance(s, dict) and s.get("step") == "f12_send" for s in sw_steps
                )
                if did_interact and wait_idle_after_bootstrap > 0:
                    _wait_for_owner_guard_clear(
                        wait_idle_after_bootstrap,
                        min_idle_seconds=min_idle_seconds,
                        require_global_idle=True,
                        on_heartbeat=on_heartbeat,
                    )
                break

        if skip_read_dry_run or switch_failed_entry is not None:
            continue

        # ── 阶段 2：单账号只读采集阶段（原地重试，绝不回头重复切号） ──
        read_retries = 0
        while True:
            if on_heartbeat is not None:
                try:
                    on_heartbeat()
                except Exception:  # noqa: BLE001
                    pass
            if not switch_dry_run and wait_idle_after_bootstrap > 0 and _owner_guard_active(
                min_idle_seconds=min_idle_seconds,
                require_global_idle=True,
            ):
                _wait_for_owner_guard_clear(
                    wait_idle_after_bootstrap,
                    min_idle_seconds=min_idle_seconds,
                    require_global_idle=True,
                    on_heartbeat=on_heartbeat,
                )

            with _scoped_ro_idle_ms(min_idle_seconds):
                read_rep = tc.run_read_account(
                    account=acct,
                    happ_exe=happ_exe,
                    allow_switch=False,
                    with_fills=bool(with_fills),
                )
            r_rc = int(read_rep.get("rc", 1))
            diag: Optional[dict[str, Any]] = None
            if read_rep.get("status") == "exception_fail_closed":
                with _scoped_ro_idle_ms(min_idle_seconds):
                    diag = _diagnose_readonly_bind(happ_exe)
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "diagnostic.json").write_text(
                    json.dumps(diag, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            if r_rc != 0 and read_retries < retries_limit and _is_owner_interruption(
                read_rep.get("status"), read_rep.get("reason"), diag=diag
            ):
                read_retries += 1
                _wait_for_owner_guard_clear(
                    wait_idle_after_bootstrap,
                    min_idle_seconds=min_idle_seconds,
                    require_global_idle=True,
                    on_heartbeat=on_heartbeat,
                )
                continue
            if r_rc != 0:
                all_ok = False
            elif with_fills:
                today_rep = _read_today_fills_for_account(acct, happ_exe)
                _merge_today_fills_into_read_report(read_rep, today_rep)
            entry: dict[str, Any] = {
                "account": acct,
                "status": read_rep.get("status"),
                "rc": r_rc,
                "reason": (
                    str(diag.get("error"))
                    if (isinstance(diag, dict) and diag.get("error") and read_rep.get("status") == "exception_fail_closed")
                    else read_rep.get("reason")
                ),
                "switch_retries": switch_retries,
                "read_retries": read_retries,
                "owner_retries": switch_retries + read_retries,
                "switch": switch_rep,
                "read_account": read_rep,
            }
            per_account[acct] = entry
            _write_named_report(out_dir, f"latest_{acct}.json", entry)
            break

    post_sync_minimize: Optional[dict[str, Any]] = None
    if minimize_after_sync and not switch_dry_run:
        post_sync_minimize = _minimize_ths_windows()

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
        "post_sync_minimize": post_sync_minimize,
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
