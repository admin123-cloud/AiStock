# -*- coding: utf-8 -*-
"""R48 盘中持仓巡读（host 侧，零交互铁律）。

Owner 原话（2026-11-11）："后续盘面中每5分钟自动读取同花顺的持仓，不要再让我
手动写做T记录"。任务书：task_briefs/20261111_holdings_poller_r48.md。

调度面：host schtasks 每 5min 触发 scripts/ths_holdings_poll.py --once，
脚本内窗口守卫收口 09:35~15:00（交易日=工作日，宿主无 CH 依赖），秒错峰 47s
（beat=+1min / fast=+23s / fills=15:05:00，巡读取 :47s 让开整分拥塞）。

零交互铁律（R48 核心，区别于 15:05 盘后 fills 导出允许 set_focus）：
  1. 面板窗口在前台（Owner 正用）→ 本轮跳过记 skipped_foreground，绝不抢焦点；
  2. 窗口最小化 → skipped_minimized（绝不自动恢复——恢复即抢焦点）；
  3. 窗口锁忙（PM fills 导出/其他巡读在跑）→ skipped_lock（锁超时 2s 即让）；
  4. 只读路径：PrintWindow 截图（capture_as_image，不吃焦点）+ UIA 资金区只读
     + RapidOCR 离屏识别；**绝不调用 _click_query_menu_item / set_focus /
     mouse.click**，绝不触任何交易界面；
  5. 面板显示的不是持仓视图 → view_not_holdings 如实跳过（不点菜单切换）。

产物（零交易零 formal 写）：
  runtime/t_tracker/holdings_snapshots/YYYYMMDD/HHMMSS_acctN.json(+png)
    —— 每轮快照（含 skipped/error 轮，如实留痕）；poller_state.json 轮次状态；
  runtime/t_tracker/trades_inferred/YYYYMMDD.jsonl
    —— 快照 diff 实时台账：{ts, account, code, delta_qty, price_ref=现价, ...}；
    delta=0 不记；15:05 fills 导出后由 15:10 reconcile 以真实成交价/费用覆盖
    （services/t_trade_coaching.override_inferred_with_fills，容差同码同向±10min）。
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, time as dt_time
from pathlib import Path
from typing import Any, Callable, Optional

SCHEMA_SNAPSHOT = "ths_holdings_snapshot/1"
SCHEMA_INFERRED = "t_tracker_trades_inferred/1"
SCHEMA_POLLER_STATE = "ths_holdings_poller_state/1"

POLL_START = dt_time(9, 35)
POLL_END = dt_time(15, 0)
STAGGER_SECOND = 47          # 秒错峰：R48 巡读 :47s（避开 fast :23s / 整分 0s）
LOCK_TIMEOUT_S = 2           # 窗口锁忙→跳过（绝不等待 Owner/PM 释放）
CONSECUTIVE_FAIL_ALERT = 3   # 连续 3 轮失败 → 告警字段
MATCH_WINDOW_MIN = 10        # 15:05 fills 覆盖匹配容差：同码同向同窗 ±10min

SKIP_STATUSES = (
    "skipped_foreground",   # 面板在前台（Owner 正用）——防打架，绝不抢焦点
    "skipped_minimized",    # 面板最小化——不自动恢复（恢复即抢焦点）
    "skipped_lock",         # 窗口锁忙（fills 导出/巡读互斥让位）
    "view_not_holdings",    # 面板不在持仓视图——不点菜单切换（零交互）
    "panel_absent",         # 无已登录面板（如 acct2 现状）——非失败
    "identity_mismatch",    # 面板登录身份≠账号绑定（换登）——fail-closed 拒绝
)


# --------------------------------------------------------------------------- #
# 目录/路径
# --------------------------------------------------------------------------- #

def snapshots_dir(root: Path, day: date) -> Path:
    return root / "holdings_snapshots" / day.strftime("%Y%m%d")


def snapshot_path(root: Path, day: date, ts: datetime, account: str) -> Path:
    return snapshots_dir(root, day) / f"{ts.strftime('%H%M%S')}_{account}.json"


def inferred_path(root: Path, day: date) -> Path:
    return root / "trades_inferred" / f"{day.strftime('%Y%m%d')}.jsonl"


def poller_state_path(root: Path, day: date) -> Path:
    return snapshots_dir(root, day) / "poller_state.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# --------------------------------------------------------------------------- #
# 窗口守卫（纯函数，可测）
# --------------------------------------------------------------------------- #

def should_poll(now: datetime) -> tuple[bool, str]:
    """交易日 09:35~15:00（含端点）。host 侧无 CH 依赖：工作日近似交易日，
    节假日面板常闭→panel_absent 非失败，零副作用。返回 (是否巡, 原因)。"""
    if now.weekday() >= 5:
        return False, "weekend"
    t = now.time()
    if t < POLL_START:
        return False, "before_window"
    if t > POLL_END:
        return False, "after_window"
    return True, "in_window"


def stagger_sleep_seconds(now: datetime) -> float:
    """到 :47s 的对齐秒数（schtasks 整分触发后等待错峰）。"""
    delta = STAGGER_SECOND - now.second - now.microsecond / 1e6
    if delta < 0:
        delta += 60.0
    return delta


# --------------------------------------------------------------------------- #
# 快照 diff → trades_inferred（纯函数，核心可测面）
# --------------------------------------------------------------------------- #

def _row_key(row: dict[str, Any]) -> str:
    return str(row.get("code") or "")


def diff_holdings(prev_rows: list[dict[str, Any]],
                  cur_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """相邻两轮 OK 快照 diff → 变动行。delta=0 不记。

    T+1 语义：买入→shares +N 且 available 不变（次日才可用）；卖出→两者同减。
    avail_delta 单独记录，供 coaching 区分"新买入冻结中"与"卖出释放"。
    """
    prev_map = {_row_key(r): r for r in prev_rows if _row_key(r)}
    cur_map = {_row_key(r): r for r in cur_rows if _row_key(r)}
    out: list[dict[str, Any]] = []
    for code in sorted(set(prev_map) | set(cur_map)):
        prev, cur = prev_map.get(code), cur_map.get(code)
        prev_shares = int((prev or {}).get("shares") or 0)
        cur_shares = int((cur or {}).get("shares") or 0)
        delta = cur_shares - prev_shares
        if delta == 0:
            continue
        avail_prev = (prev or {}).get("available_shares")
        avail_cur = (cur or {}).get("available_shares")
        if avail_prev is None or avail_cur is None:
            avail_delta = None
        else:
            avail_delta = int(avail_cur) - int(avail_prev)
        out.append({
            "code": code,
            "name": str((cur or prev or {}).get("name") or ""),
            "delta_qty": delta,
            "side": "buy" if delta > 0 else "sell",
            "prev_shares": prev_shares,
            "cur_shares": cur_shares,
            "avail_delta": avail_delta,
            "price_ref": (cur or {}).get("current_price"),
            "cost_price": (cur or {}).get("cost_price"),
        })
    return out


def build_inferred_rows(account: str, ts: datetime, changes: list[dict[str, Any]],
                        prev_snapshot: str, cur_snapshot: str) -> list[dict[str, Any]]:
    rows = []
    for ch in changes:
        rows.append({
            "schema": SCHEMA_INFERRED, "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "account": account, "code": ch["code"], "name": ch.get("name") or "",
            "side": ch["side"], "delta_qty": ch["delta_qty"],
            "prev_shares": ch.get("prev_shares"), "cur_shares": ch.get("cur_shares"),
            "avail_delta": ch.get("avail_delta"),
            "price_ref": ch.get("price_ref"), "cost_price": ch.get("cost_price"),
            "prev_snapshot": prev_snapshot, "cur_snapshot": cur_snapshot,
            "source": "snapshot_diff",
            "fill_override": None,   # 15:05 fills 后由 reconcile 覆盖（±10min 同码同向）
        })
    return rows


def append_inferred(root: Path, day: date, new_rows: list[dict[str, Any]]) -> int:
    """幂等追加：同 (account, code, ts) 已存在不重记（轮次重放安全）。"""
    path = inferred_path(root, day)
    existing = {(r.get("account"), r.get("code"), r.get("ts"))
                for r in read_jsonl(path)}
    fresh = [r for r in new_rows
             if (r.get("account"), r.get("code"), r.get("ts")) not in existing]
    _append_jsonl(path, fresh)
    return len(fresh)


# --------------------------------------------------------------------------- #
# 轮次编排（capture 可注入 → 单测零 THS 依赖）
# --------------------------------------------------------------------------- #

def _load_state(root: Path, day: date) -> dict[str, Any]:
    path = poller_state_path(root, day)
    if not path.is_file():
        return {"schema": SCHEMA_POLLER_STATE, "date": day.isoformat(),
                "rounds": 0, "accounts": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {"schema": SCHEMA_POLLER_STATE, "date": day.isoformat(),
                "rounds": 0, "accounts": {}}


def _load_snapshot_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return []
    if payload.get("status") != "ok":
        return []
    return list(payload.get("holdings") or [])


def run_poll_round(*, root: Path, now: datetime, plan: dict[str, dict[str, Any]],
                   capture: Callable[[str, dict[str, Any]], dict[str, Any]],
                   day: Optional[date] = None) -> dict[str, Any]:
    """单轮巡读：逐绑定面板 capture → 快照落盘 → diff → inferred。

    plan: {account: {"panel": {...}}}（scripts 层用 bridge 注册表构建，见
    build_capture_plan 语义）；capture(account, panel) 返回
    {status, holdings?, capital?, ocr_lines?, screenshot?, error?}。
    """
    day = day or now.date()
    state = _load_state(root, day)
    state.setdefault("accounts", {})
    report: dict[str, Any] = {"schema": "ths_holdings_poll_round/1",
                              "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                              "accounts": {}}
    for account in sorted(plan):
        entry = plan[account] or {}
        if not isinstance(entry, dict):
            entry = {"panel": entry}
        panel = entry.get("panel")
        hint = entry.get("identity_hint")
        acct_state = state["accounts"].setdefault(
            account, {"rounds": 0, "ok": 0, "skips": {}, "failures": 0,
                      "consecutive_failures": 0, "alert": None,
                      "last_ok_snapshot": None, "last_ok_ts": None})
        acct_state["rounds"] += 1
        try:
            cap = capture(account, panel, hint)
        except Exception as exc:  # noqa: BLE001 - 采集异常如实记录不中断他账
            cap = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        status = str(cap.get("status") or "error")
        snap_file = snapshot_path(root, day, now, account)
        snapshot_payload: dict[str, Any] = {
            "schema": SCHEMA_SNAPSHOT, "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "date": day.isoformat(), "account": account,
            "status": status,
            "identity_label": cap.get("identity_label"),
            "identity_hint": hint,
            "panel": {"pid": (panel or {}).get("pid"),
                      "title": (panel or {}).get("title")},
            "holdings": cap.get("holdings") or [],
            "capital": cap.get("capital") or {},
            "ocr_line_count": len(cap.get("ocr_lines") or []),
            "ocr_lines": (cap.get("ocr_lines") or [])[:40],
            "screenshot": cap.get("screenshot"),
            "error": cap.get("error"),
        }
        _atomic_write_json(snap_file, snapshot_payload)
        acct_result: dict[str, Any] = {"status": status,
                                       "snapshot": str(snap_file)}
        if status == "ok":
            prev_file = acct_state.get("last_ok_snapshot")
            if prev_file:
                changes = diff_holdings(_load_snapshot_rows(Path(prev_file)),
                                        cap.get("holdings") or [])
            else:
                changes = []  # 首轮=基线（对空 diff 会把存量持仓误记为买入）
            new_rows = build_inferred_rows(
                account, now, changes,
                prev_file or "", snap_file.name)
            added = append_inferred(root, day, new_rows)
            acct_state["consecutive_failures"] = 0
            acct_state["alert"] = None
            acct_state["last_ok_snapshot"] = str(snap_file)
            acct_state["last_ok_ts"] = snapshot_payload["ts"]
            acct_state["ok"] += 1
            acct_result.update({"changes": len(changes), "inferred_added": added,
                                "diff": [{k: ch.get(k) for k in
                                          ("code", "side", "delta_qty", "price_ref",
                                           "avail_delta")} for ch in changes]})
        elif status in SKIP_STATUSES:
            key = "skipped_foreground" if status == "skipped_foreground" else status
            acct_state["skips"][key] = int(acct_state["skips"].get(key, 0)) + 1
        else:
            acct_state["failures"] += 1
            acct_state["consecutive_failures"] += 1
            if acct_state["consecutive_failures"] >= CONSECUTIVE_FAIL_ALERT:
                acct_state["alert"] = (
                    f"consecutive_failures={acct_state['consecutive_failures']}")
                acct_result["alert"] = acct_state["alert"]
        report["accounts"][account] = acct_result
    state["rounds"] += 1
    _atomic_write_json(poller_state_path(root, day), state)
    return report


# --------------------------------------------------------------------------- #
# host 真机采集（仅 F:/python/python3.10；零交互路径）
# --------------------------------------------------------------------------- #

_HOLDINGS_HEADER_HINTS = ("股票余额", "证券代码", "股票市值", "参考市值")
_OTHER_VIEW_HINTS = ("成交日期", "委托日期", "买卖", "摘要", "发生金额", "委托股数")


def _is_foreground(hwnd: int) -> bool:
    import ctypes
    return bool(hwnd) and ctypes.windll.user32.GetForegroundWindow() == int(hwnd)


def print_window_image(hwnd: int) -> "Any":
    """PrintWindow 离屏渲染目标窗口（含非客户区，坐标系与 bridge 屏幕截取
    的 target.rectangle() 一致——OCR 列区间 400~990 依赖该校准）。

    不吃焦点、不置顶、不重绘打扰（WM_PRINT 到窗口线程）。PrintWindow 失败
    （返回 0）抛错——绝不退回屏幕截取（被遮挡窗口会截到别的内容=脏数据）。
    """
    import ctypes
    from ctypes import wintypes

    from PIL import Image

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("GetWindowRect_failed")
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise RuntimeError(f"invalid_window_rect {width}x{height}")

    hwnd_dc = user32.GetWindowDC(hwnd)
    if not hwnd_dc:
        raise RuntimeError("GetWindowDC_failed")
    try:
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
        old = gdi32.SelectObject(mem_dc, bitmap)
        try:
            # PW_RENDERFULLCONTENT=2：DirectComposition 内容也可抓；老 GDI 窗口
            # （THS xiadan 5.0 即是）两种 flag 都行，2 兼容面最大。
            ok = user32.PrintWindow(hwnd, mem_dc, 2)
            if not ok:
                ok = user32.PrintWindow(hwnd, mem_dc, 0)
            if not ok:
                raise RuntimeError("PrintWindow_failed")

            class _BMI(ctypes.Structure):
                _fields_ = [
                    ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD),
                ]

            bmi = _BMI()
            bmi.biSize = ctypes.sizeof(_BMI)
            bmi.biWidth = width
            bmi.biHeight = -height  # top-down
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = 0  # BI_RGB
            buf = ctypes.create_string_buffer(width * height * 4)
            if not gdi32.GetDIBits(hwnd_dc, bitmap, 0, height, buf,
                                   ctypes.byref(bmi), 0):
                raise RuntimeError("GetDIBits_failed")
            return Image.frombuffer(
                "RGBA", (width, height), buf.raw, "raw", "BGRA", 0, 1).copy()
        finally:
            gdi32.SelectObject(mem_dc, old)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(mem_dc)
    finally:
        user32.ReleaseDC(hwnd, hwnd_dc)


def _looks_like_holdings_view(lines: list[str]) -> bool:
    return any(hint in line for line in lines for hint in _HOLDINGS_HEADER_HINTS)


def _looks_like_other_view(lines: list[str]) -> bool:
    return any(hint in line for line in lines for hint in _OTHER_VIEW_HINTS)


def capture_panel_zero_touch(account: str, panel: Optional[dict[str, Any]],
                             *, evidence_dir: Optional[Path] = None,
                             lock_timeout_s: int = LOCK_TIMEOUT_S,
                             identity_hint: Optional[str] = None,
                             ) -> dict[str, Any]:
    """零交互采集一个面板（真机路径）。

    决策序（铁律）：
      1. 无面板窗口 → panel_absent；
      2. 窗口在前台 → skipped_foreground（绝不抢焦点）；
      3. 窗口锁忙（timeout=2s）→ skipped_lock；
      4. 窗口最小化 → skipped_minimized（绝不恢复）；
      5. PrintWindow 截图（capture_as_image，不吃焦点/不点菜单）→ RapidOCR
         → 持仓表头在 → 解析；否则 view_not_holdings（绝不点菜单切换视图）。
      UIA 资金区只读读取尽力而为（失败不影响持仓行）。
    """
    from services.ths_account_bridge import (  # noqa: WPS433
        _find_window_for_panel,
        _ocr_items_to_lines,
        _parse_holdings_from_ocr_items,
        _read_from_ths_uia_snapshot,
        _ths_window_operation_lock,
        panel_scope,
    )

    if panel is None:
        # acct1 未绑定面板 → 默认 connect 路径（bridge 语义）
        try:
            from services.ths_account_bridge import _find_xiadan_window
            with panel_scope(None):
                window = _find_xiadan_window()
        except Exception as exc:  # noqa: BLE001
            return {"status": "panel_absent", "error": f"{type(exc).__name__}: {exc}"}
    else:
        try:
            window = _find_window_for_panel(panel)
        except Exception as exc:  # noqa: BLE001
            return {"status": "panel_absent", "error": f"{type(exc).__name__}: {exc}"}

    hwnd = int(getattr(window, "handle", 0) or 0)
    if _is_foreground(hwnd):
        return {"status": "skipped_foreground",
                "note": "面板在前台（Owner 使用中），本轮让位"}

    try:
        with _ths_window_operation_lock(timeout_seconds=lock_timeout_s,
                                        panel=account):
            try:
                if _is_foreground(hwnd):  # 拿锁瞬间再查一次（竞态防线）
                    return {"status": "skipped_foreground",
                            "note": "面板在前台（Owner 使用中），本轮让位"}
                try:
                    minimized = bool(window.is_minimized())
                except Exception:  # noqa: BLE001
                    minimized = False
                if minimized:
                    return {"status": "skipped_minimized",
                            "note": "面板最小化；不自动恢复（恢复即抢焦点）"}

                img = print_window_image(hwnd)  # 离屏渲染，不吃焦点不点菜单
                screenshot: Optional[str] = None
                png: Path
                if evidence_dir is not None:
                    evidence_dir.mkdir(parents=True, exist_ok=True)
                    png = evidence_dir / f"{datetime.now().strftime('%H%M%S')}_{account}.png"
                    img.save(png)
                    screenshot = str(png)
                else:
                    png = Path(os.environ.get("TMPDIR") or
                               os.environ.get("TEMP") or ".") / "ths_poll_capture.png"
                    try:
                        img.save(png)
                        screenshot = str(png)
                    except Exception:  # noqa: BLE001
                        screenshot = None

                items, lines = _ocr_image(png)
                # R48 身份门禁：顶栏「券商-姓名」OCR 标签 vs 期望（fail-closed）。
                # 同一窗口换登另一账号（2026-11-11 实况 曾*→王*美）时拒绝错标。
                from services.ths_account_bridge import (
                    identity_matches, parse_identity_label)
                identity_label = parse_identity_label(lines)
                gate = identity_matches(identity_label, identity_hint)
                capital: dict[str, Any] = {}
                try:
                    with panel_scope(panel):
                        uia = _read_from_ths_uia_snapshot()
                    cap = uia.get("capital") if isinstance(uia, dict) else None
                    if isinstance(cap, dict):
                        capital = cap
                except Exception:  # noqa: BLE001 - UIA 尽力而为
                    capital = {}
                if gate is False:
                    return {"status": "identity_mismatch",
                            "identity_label": identity_label,
                            "identity_hint": identity_hint,
                            "note": "面板登录身份与账号绑定不符（fail-closed 拒绝采集）",
                            "ocr_lines": lines[:12], "screenshot": screenshot}
                holdings = _parse_holdings_from_ocr_items(items, capital=capital or None)
                if holdings:
                    return {"status": "ok", "holdings": holdings, "capital": capital,
                            "ocr_lines": lines, "screenshot": screenshot,
                            "identity_label": identity_label}
                if _looks_like_other_view(lines) and not _looks_like_holdings_view(lines):
                    return {"status": "view_not_holdings",
                            "note": "面板当前视图非持仓（零交互不切换）",
                            "ocr_lines": lines[:20], "screenshot": screenshot,
                            "identity_label": identity_label}
                return {"status": "error", "error": "EMPTY_HOLDINGS_PARSE",
                        "ocr_lines": lines[:20], "screenshot": screenshot,
                        "identity_label": identity_label}
            except Exception as exc:  # noqa: BLE001 - R48：块内捕获为状态 dict，
                # 不经 generator CM 的 throw 传播（换登扰动期会变形，14:46 实证）
                return {"status": "error",
                        "error": f"{type(exc).__name__}: {exc}"}
    except TimeoutError:
        return {"status": "skipped_lock",
                "note": "窗口锁忙（fills 导出/巡读互斥），本轮让位"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def _ocr_image(png: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """RapidOCR 离屏识别（对 PrintWindow 位图，无任何窗口交互）。"""
    from rapidocr_onnxruntime import RapidOCR

    from services.ths_account_bridge import _ocr_items_to_lines

    result, _ = RapidOCR()(str(png))
    items: list[dict[str, Any]] = []
    for row in result or []:
        if len(row) < 3:
            continue
        box, text, score = row[0], str(row[1] or "").strip(), float(row[2] or 0.0)
        if not text:
            continue
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        items.append({"x": sum(xs) / len(xs), "y": sum(ys) / len(ys),
                      "text": text, "score": score})
    return items, _ocr_items_to_lines(items)
