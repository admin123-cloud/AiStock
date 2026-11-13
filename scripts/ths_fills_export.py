# -*- coding: utf-8 -*-
"""R37b host 侧 THS 当日成交导出器：15:05 填充 runtime/t_tracker/fills/YYYYMMDD.json。

复用 R32 `services/ths_account_bridge` 的窗口锁 + 查询菜单驱动 + OCR 路径
（证据 runtime/ths_holdings_r32/）。流程：

  窗口锁 ths_window.lock → 查询菜单「当日成交」→ 窗口截图 OCR → 解析当日成交
  → 查询菜单「历史成交」→ OCR → bridge 历史行解析（近 N 日，跨日 T 配对）
  → 合并为 R37 fills 协议 JSON → 原子写 runtime/t_tracker/fills/<date>.json

铁律（R32/R37 一致）：
  * 只触查询菜单（`_click_query_menu_item` 的查询子项坐标），绝不打开下单界面；
  * THS 未登录/窗口不可达/视图无法确认 → 写 `<date>.unavailable.json` 标记，不造数；
  * 当日行解析出现残行（价格/数量缺）→ fail-closed 写 unavailable，不写部分成交；
  * 当日零成交（表头在、无数据行）= 合法零，写空 fills 列表的 ok 文件；
  * 零交易动作、零 formal 表写。

用法（PM 挂每交易日 15:05 cron）：
  F:/python/python3.10/python.exe F:/Aistock/app/scripts/ths_fills_export.py --once

  --date YYYY-MM-DD      覆盖导出日（默认今天，仅支持当日窗口实测）
  --once                 默认模式：单次导出（幂等：盘后 ok 文件已存在则跳过）
  --force                忽略幂等跳过，强制重导
  --out-dir PATH         覆盖 t_tracker 根目录（测试用）
  --history-window-days  历史成交配对窗口天数（默认 10）

退出码：0=ok/zero_trades/skip；2=unavailable（未写 fills 文件）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SCHEMA = "ths_fills_export/1"
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
        "schema": SCHEMA,
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


def _tracker_root(out_dir: Optional[Path]) -> Path:
    if out_dir is not None:
        return Path(out_dir)
    try:
        from utils.paths import runtime_path
        return runtime_path("t_tracker")
    except Exception:  # pragma: no cover - 无 utils.paths 时退回默认数据根
        return Path(r"F:\AiStock\data\data\runtime\t_tracker")


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
    return {"schema": "ths_fills_export_multi/1", "trade_date": for_date.isoformat(),
            "accounts": accounts, "resolution": resolution, "rc": rc}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="R37b/R47 THS 当日成交导出器（host 侧，15:05 填充 t_tracker fills 源，多账号）")
    parser.add_argument("--once", action="store_true", default=True,
                        help="单次导出（默认模式，幂等）")
    parser.add_argument("--date", help="YYYY-MM-DD（默认今天）")
    parser.add_argument("--force", action="store_true", help="忽略幂等跳过，强制重导")
    parser.add_argument("--out-dir", help="覆盖 t_tracker 根目录（测试用）")
    parser.add_argument("--history-window-days", type=int,
                        default=DEFAULT_HISTORY_WINDOW_DAYS,
                        help="历史成交配对窗口天数（默认 10）")
    parser.add_argument("--account", help="只导出指定账号（默认全部绑定账号循环）")
    args = parser.parse_args(argv)

    for_date = _parse_for_date(args.date)
    now = datetime.now()

    from services.ths_account_bridge import load_panel_registry

    try:
        from services.ths_account_bridge import enumerate_trade_panels
        # include_minimized：15:05 面板常最小化——解析须见窗（导出既有语义
        # 会恢复→导出→复最小化），否则 acct2 永远 panel_absent。
        live_panels = enumerate_trade_panels(include_minimized=True)
    except Exception:  # 非 host/无窗口环境 → 空面板表，acct1 走默认 connect
        live_panels = []
    plan = build_capture_plan(live_panels, load_panel_registry(),
                              account_filter=args.account)
    resolution = plan.pop("_resolution")
    # R51 ③：单账号采集（--account 唯一或计划仅一账号）才允许 ComboBox
    # 备用锚降级验证身份；双账号（热切换共享面板）保持 fail-closed。
    to_capture = [a for a in plan
                  if not args.account or a == args.account]
    combo_fallback = len(to_capture) == 1
    for account, entry in plan.items():
        if args.account and account != args.account:
            continue
        panel = entry.get("panel")
        entry["capture"] = (lambda ev, p=panel, a=account,
                            h=entry.get("identity_hint"),
                            sw=entry.get("hotswitch"),
                            cf=combo_fallback:
                            _capture_ths_views(ev, panel=p, account=a,
                                               identity_hint=h,
                                               hotswitch=sw,
                                               combo_identity_fallback=cf))
    plan["_resolution"] = resolution
    result = run_export_multi(for_date=for_date, now=now,
                              out_root=Path(args.out_dir) if args.out_dir else None,
                              plan=plan, force=args.force,
                              known_accounts=[args.account] if args.account
                              else list(KNOWN_ACCOUNTS))
    result["trade_date"] = for_date.isoformat()
    print(json.dumps(result, ensure_ascii=False, default=str))
    return int(result.get("rc", 0))


if __name__ == "__main__":
    raise SystemExit(main())
