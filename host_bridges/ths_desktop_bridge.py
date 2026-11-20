# -*- coding: utf-8 -*-
"""AiStock 宿主机常驻桌面桥（运行于 Windows 交互桌面 WinSta0\\Default）。

职责：
1. 周期刷新 `runtime/ths_read_account/bridge_heartbeat.json`，供 AiStock 后端与前端感知桌面桥在线态；
2. 消费 `runtime/ths_read_account/commands/req_*.json` 只读同步指令，串行调用
   `scripts/ths_read_account_task.py::run_multi_account_task` 完成单/双账号采集；
3. 采集成功后，自动将各账号当日真实成交幂等回放至 L2 做T 闭环状态机（`services/l2_fill_closure.py`）；
4. 严守安全铁律：仅执行 `action == "read_accounts"` 且 `allow_order is False`，绝不下单/绝不触碰凭证。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import ths_read_account_task as ths_task  # noqa: E402
from services import l2_fill_closure  # noqa: E402
from services import t_trade_signal as tts  # noqa: E402

BJ_TZ = timezone(timedelta(hours=8))
DEFAULT_ROOT = Path(r"F:\Aistock\runtime\ths_read_account")
DEFAULT_HAPP_EXE = Path(r"D:\同花顺远航\同花顺远航版\bin\happ.exe")


def _atomic_write_json(path: Path, payload: dict[str, Any], *, retries: int = 5, retry_delay: float = 0.05) -> None:
    """原子写 JSON；在 Windows 下对并发读锁引发的瞬时 PermissionError/OSError 做短重试。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    last_exc: Optional[OSError] = None
    for attempt in range(max(1, retries)):
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
            return
        except OSError as exc:
            last_exc = exc
            if attempt + 1 < max(1, retries):
                time.sleep(retry_delay)
    if last_exc is not None:
        raise last_exc


def write_heartbeat(
    root: Path,
    *,
    state: str = "idle",
    active_request_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> None:
    stamp = (now.astimezone(BJ_TZ) if (now and now.tzinfo) else (now.replace(tzinfo=BJ_TZ) if now else datetime.now(BJ_TZ)))
    _atomic_write_json(
        root / "bridge_heartbeat.json",
        {
            "schema": "ths_desktop_bridge_heartbeat/1",
            "state": state,
            "active_request_id": active_request_id,
            "pid": os.getpid(),
            "heartbeat_at": stamp.isoformat(),
        },
    )


def _sync_fills_to_l2_closure(
    multi_report: dict[str, Any],
    *,
    for_date: date,
    t_tracker_root: Optional[Path] = None,
) -> dict[str, Any]:
    """将双账号采集结果中的当日真实成交自动同步至 L2 做T 闭环状态机（幂等安全）。"""
    summary: dict[str, Any] = {}
    root_dir = tts.tracker_dir(t_tracker_root)
    accounts_map = multi_report.get("accounts") or {}
    for acct, entry in accounts_map.items():
        if not isinstance(entry, dict):
            continue
        ra = entry.get("read_account")
        if not isinstance(ra, dict):
            continue
        fills_obj = ra.get("fills")
        rows = list((fills_obj or {}).get("rows") or []) if isinstance(fills_obj, dict) else []
        today_obj = ra.get("today_fills")
        today_rows = list((today_obj or {}).get("rows") or []) if isinstance(today_obj, dict) else []
        # 优先使用完整「当日成交」原表（保留全部同秒同价拆单），无当日成交表时回退 fills.rows
        combined_rows = today_rows if today_rows else rows
        if not combined_rows:
            summary[acct] = {"status": "no_fills"}
            continue

        # 统计同批次内基础指纹出现次数，确保：
        # 1）OCR 截断的 fill_no（如 "010300."）或同委托多笔拆单各自拥有唯一 fill_id（#1/#2/#3），不被误吞；
        # 2）下一次采集到相同拆单列表时，生成的 fill_id 集合完全一致，保持 100% 幂等。
        base_sig_counts: dict[tuple[Any, ...], int] = {}
        fill_no_counts: dict[str, int] = {}
        for row in combined_rows:
            if not isinstance(row, dict):
                continue
            fno = str(row.get("fill_no") or "").strip()
            if fno:
                fill_no_counts[fno] = fill_no_counts.get(fno, 0) + 1

        normalized_inputs = []
        for idx, row in enumerate(combined_rows):
            if not isinstance(row, dict):
                continue
            r_copy = dict(row)
            r_copy.setdefault("account", acct)
            if r_copy.get("qty") is None and r_copy.get("quantity") is not None:
                r_copy["qty"] = r_copy["quantity"]
            if r_copy.get("px") is None and r_copy.get("price") is not None:
                r_copy["px"] = r_copy["price"]

            raw_fno = str(row.get("fill_no") or "").strip()
            raw_cno = str(row.get("contract_no") or "").strip()
            is_truncated_or_dup_fno = (
                not raw_fno
                or raw_fno.endswith(".")
                or raw_fno.endswith("…")
                or fill_no_counts.get(raw_fno, 0) > 1
            )
            if not is_truncated_or_dup_fno:
                stable_fid = raw_fno
            else:
                sig = (
                    acct,
                    str(row.get("date") or "").replace("-", "").strip(),
                    str(row.get("time") or "").strip(),
                    str(row.get("code") or "").strip(),
                    str(row.get("side") or "").strip().lower(),
                    raw_cno,
                    raw_fno,
                    str(r_copy.get("px") or ""),
                    str(r_copy.get("qty") or ""),
                )
                occ = base_sig_counts.get(sig, 0) + 1
                base_sig_counts[sig] = occ
                stable_fid = "|".join(sig) + f"#{occ}"
            r_copy["fill_id"] = stable_fid
            normalized_inputs.append(r_copy)
        try:
            rep = l2_fill_closure.reconcile_account_fills_closure(
                root=root_dir,
                for_date=for_date,
                target_account=acct,
                fills=normalized_inputs,
                dry_run=False,
            )
            summary[acct] = {
                "status": "synced",
                "applied_buys": rep.get("applied_buys", 0),
                "applied_sells": rep.get("applied_sells", 0),
                "idempotent_skipped": rep.get("idempotent_skipped", 0),
            }
        except Exception as exc:  # noqa: BLE001 - L2 同步独立异常面，不拖垮账号采集
            summary[acct] = {"status": "error", "reason": type(exc).__name__}
    return summary


def _notify_sync_completed(result: dict[str, Any]) -> None:
    """在宿主机交互桌面弹出非阻塞完成气泡提醒（失败静默忽略，绝不阻断主流程）。"""
    if os.name != "nt":
        return
    try:
        rc = int(result.get("rc", 1))
        accts = ",".join(str(a) for a in (result.get("accounts") or []))
        title = "AiStock 资金账号同步完成" if rc == 0 else "AiStock 资金账号同步结束"
        body = (
            f"双账号（{accts}）已同步完成，同花顺交易界面已自动最小化。"
            if rc == 0
            else f"账号（{accts}）同步结束（状态：{result.get('status')}），请查看资金账号管理页。"
        )
        # 转义单引号防止 PowerShell 字符串中断
        t_safe = title.replace("'", "''")
        b_safe = body.replace("'", "''")
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -AssemblyName System.Drawing; "
            "$n = New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon = [System.Drawing.SystemIcons]::Information; "
            "$n.Visible = $true; "
            f"$n.ShowBalloonTip(5000, '{t_safe}', '{b_safe}', [System.Windows.Forms.ToolTipIcon]::Info); "
            "Start-Sleep -Milliseconds 5500; "
            "$n.Dispose();"
        )
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except Exception:  # noqa: BLE001
        pass


def process_one_command(
    root: Path,
    *,
    happ_exe: Path = DEFAULT_HAPP_EXE,
    runner: Optional[Callable[..., dict[str, Any]]] = None,
    t_tracker_root: Optional[Path] = None,
    wait_idle_after_bootstrap: float = 180.0,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """消费 commands/ 目录下最早一条待执行指令；无指令返回 None。"""
    cmd_dir = root / "commands"
    if not cmd_dir.is_dir():
        return None
    pending = sorted(cmd_dir.glob("req_*.json"))
    if not pending:
        return None

    req_path = pending[0]
    try:
        req = json.loads(req_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        req = {}

    req_id = str(req.get("request_id") or req_path.stem)
    action = str(req.get("action") or "")
    allow_order = bool(req.get("allow_order", False))
    done_dir = cmd_dir / "done"
    done_dir.mkdir(parents=True, exist_ok=True)

    # 安全红线：仅允许只读采集指令，绝不执行任何下单请求
    if action != "read_accounts" or allow_order:
        rejected = {
            **req,
            "request_id": req_id,
            "status": "rejected_unsafe_action",
            "rc": 4,
            "credentials_touched": False,
            "finished_at": datetime.now(BJ_TZ).isoformat(),
        }
        _atomic_write_json(done_dir / req_path.name, rejected)
        req_path.unlink(missing_ok=True)
        return rejected

    accounts = [str(a).strip() for a in (req.get("accounts") or ["acct1", "acct2"]) if str(a).strip()]
    with_fills = bool(req.get("with_fills", True))
    trigger = str(req.get("trigger") or "manual_ui").strip().lower()
    # 网页主动点击指令（commands/ 队列）允许 5s 短空闲守卫并在遇鼠键操作时自动延后 5s 重试；
    # 若显式标记为 scheduled 则严守 90s 后台静默门禁。
    min_idle_seconds: Optional[float] = None
    if trigger != "scheduled":
        try:
            raw_idle = float(req.get("idle_guard_seconds") or 5.0)
        except (TypeError, ValueError):
            raw_idle = 5.0
        min_idle_seconds = max(5.0, min(90.0, raw_idle))

    write_heartbeat(root, state="running", active_request_id=req_id, now=now)
    exec_fn = runner or ths_task.run_multi_account_task
    exec_kwargs: dict[str, Any] = {
        "accounts": accounts,
        "happ_exe": happ_exe,
        "with_fills": with_fills,
        "cold_start": True,
        "wait_idle_after_bootstrap": wait_idle_after_bootstrap,
        "out_dir": root,
    }
    if runner is None:
        exec_kwargs["min_idle_seconds"] = min_idle_seconds
        exec_kwargs["max_owner_retries"] = 3
        exec_kwargs["minimize_after_sync"] = True
        exec_kwargs["on_heartbeat"] = lambda: write_heartbeat(
            root, state="running", active_request_id=req_id
        )
    try:
        multi_rep = exec_fn(**exec_kwargs)
        today_bj = (now.astimezone(BJ_TZ).date() if (now and now.tzinfo) else datetime.now(BJ_TZ).date())
        l2_sync = _sync_fills_to_l2_closure(multi_rep, for_date=today_bj, t_tracker_root=t_tracker_root)
        result = {
            "schema": "ths_desktop_bridge_result/1",
            "request_id": req_id,
            "accounts": accounts,
            "status": multi_rep.get("status"),
            "rc": int(multi_rep.get("rc", 1)),
            "l2_closure_sync": l2_sync,
            "post_sync_minimize": multi_rep.get("post_sync_minimize"),
            "credentials_touched": False,
            "finished_at": datetime.now(BJ_TZ).isoformat(),
        }
    except Exception as exc:  # noqa: BLE001
        result = {
            "schema": "ths_desktop_bridge_result/1",
            "request_id": req_id,
            "accounts": accounts,
            "status": "bridge_error",
            "rc": 4,
            "reason": f"{type(exc).__name__}: {exc}",
            "credentials_touched": False,
            "finished_at": datetime.now(BJ_TZ).isoformat(),
        }
    finally:
        write_heartbeat(root, state="idle", active_request_id=None, now=now)

    _atomic_write_json(done_dir / req_path.name, result)
    req_path.unlink(missing_ok=True)
    if runner is None:
        _notify_sync_completed(result)
    return result


def _append_bridge_error_log(root: Path, exc: BaseException) -> None:
    """将主循环未预期异常追加落盘，便于 pythonw 无控制台模式下排障。"""
    try:
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(BJ_TZ).isoformat()
        line = f"[{stamp}] {type(exc).__name__}: {exc}\n"
        with (root / "bridge_error.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001
        pass


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AiStock 宿主机常驻桌面桥（仅只读采集，绝不下单）")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="同步产物与指令队列根目录")
    parser.add_argument("--happ-exe", type=Path, default=DEFAULT_HAPP_EXE, help="同花顺远航版主程序路径")
    parser.add_argument("--poll-seconds", type=float, default=5.0, help="轮询间隔秒数（默认 5s）")
    parser.add_argument("--once", action="store_true", help="单轮刷新心跳并消费一条指令后退出")
    args = parser.parse_args(argv)

    write_heartbeat(args.root, state="idle")
    if args.once:
        process_one_command(args.root, happ_exe=args.happ_exe)
        return 0

    while True:
        try:
            write_heartbeat(args.root, state="idle")
            process_one_command(args.root, happ_exe=args.happ_exe)
        except Exception as exc:  # noqa: BLE001 - 常驻守护进程禁止因瞬时 IO/文件锁冲突崩溃退出
            _append_bridge_error_log(args.root, exc)
        time.sleep(max(1.0, float(args.poll_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
