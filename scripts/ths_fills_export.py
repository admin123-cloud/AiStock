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


def _capture_ths_views(evidence_dir: Optional[Path] = None,
                       panel: Optional[dict[str, Any]] = None,
                       account: str = "acct1",
                       identity_hint: Optional[str] = None) -> dict[str, Any]:
    """真机采集（仅 host / F:\\python\\python3.10）：锁内依次导出当日/历史成交视图。

    R47：panel 指定目标面板（panel_scope 内 _find_xiadan_window 连到该面板 pid），
    锁按账号分把（acct1=legacy ths_window.lock 与旧部署互斥，其他=分把锁）。
    只触 `_click_query_menu_item` 查询子项（查询菜单），绝不打开下单界面；
    窗口若原为最小化，采集后还原为最小化。两视图截图与 OCR 行归档 evidence_dir。
    R48 身份门禁：首个视图 OCR 后校验顶栏「券商-姓名」含 identity_hint；
    不符（窗口换登他人账号）→ 抛 PANEL_IDENTITY_MISMATCH → run_export 落
    unavailable 标记（fail-closed，绝不把账号2成交写成 acct1）。
    """
    import shutil

    from services.ths_account_bridge import (  # noqa: WPS433
        _click_query_menu_item,
        _find_xiadan_window,
        _ocr_xiadan_window,
        _parse_trades_from_ocr_items,
        _ths_window_operation_lock,
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
    with panel_scope(panel), _ths_window_operation_lock(panel=account):
        try:
            window = _find_xiadan_window()
            try:
                was_minimized = bool(window.is_minimized())
            except Exception:
                was_minimized = False
            _click_query_menu_item("today_trades")
            _today_items, today_lines, shot_today = _ocr_xiadan_window()
            kept_today = _keep(shot_today, "today_view.png")
            if identity_hint:  # R48 身份门禁（换登 fail-closed）
                assert_panel_identity(today_lines, identity_hint, account)
            _click_query_menu_item("history_trades")
            history_items, history_lines, shot_history = _ocr_xiadan_window()
            kept_history = _keep(shot_history, "history_view.png")
            history_rows = _parse_trades_from_ocr_items(history_items)
            if was_minimized:
                try:
                    window.minimize()
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001 - R48：块内捕获，不依赖
            # generator CM 的 throw 传播（2026-11-11 14:46 换登扰动期实证：
            # 经 contextlib 传播会变形为 "generator didn't stop after throw()"）
            inner_error = exc
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
    return {
        "today_lines": today_lines,
        "history_rows": history_rows,
        "history_header_seen": history_header_seen,
        "window_state": {"minimized_before": was_minimized, "restored_minimized": was_minimized},
        "screenshots": {"today": kept_today, "history": kept_history},
    }


def _parse_for_date(text: Optional[str]) -> date:
    if not text:
        return datetime.now().date()
    return date.fromisoformat(text)


# ── R47 多账号：采集计划 + 账号循环 ───────────────────────────

KNOWN_ACCOUNTS = ("acct1", "acct2")


def build_capture_plan(live_panels: list[dict[str, Any]],
                       registry: Any) -> dict[str, dict[str, Any]]:
    """live 面板 × 注册表规则 → 采集计划 {account: {"panel": ...}}。

    acct1 无绑定面板时仍入计划（panel=None → 默认 connect 路径，legacy 行为）；
    其他账号仅有绑定面板才入计划（fail-closed：未绑定面板绝采集）。
    """
    resolved = registry.resolve(live_panels)
    plan: dict[str, dict[str, Any]] = {}
    hint_by_account = {str(r.get("account")): r.get("identity_hint")
                       for r in resolved.rules if isinstance(r, dict)}
    for account in sorted(resolved.by_account):
        plan[account] = {"panel": resolved.by_account[account],
                         "identity_hint": hint_by_account.get(account)}
    if "acct1" not in plan:
        plan["acct1"] = {"panel": None,
                         "identity_hint": hint_by_account.get("acct1")}
    plan["_resolution"] = {  # 上报面（不参与采集）
        "unbound_panels": resolved.unbound,
        "ambiguous_accounts": resolved.ambiguous_accounts,
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
        live_panels = enumerate_trade_panels()
    except Exception:  # 非 host/无窗口环境 → 空面板表，acct1 走默认 connect
        live_panels = []
    plan = build_capture_plan(live_panels, load_panel_registry())
    resolution = plan.pop("_resolution")
    for account, entry in plan.items():
        if args.account and account != args.account:
            continue
        panel = entry.get("panel")
        entry["capture"] = (lambda ev, p=panel, a=account,
                            h=entry.get("identity_hint"):
                            _capture_ths_views(ev, panel=p, account=a,
                                               identity_hint=h))
    plan["_resolution"] = resolution
    result = run_export_multi(for_date=for_date, now=now,
                              out_root=Path(args.out_dir) if args.out_dir else None,
                              plan=plan, force=args.force,
                              known_accounts=list(KNOWN_ACCOUNTS))
    result["trade_date"] = for_date.isoformat()
    print(json.dumps(result, ensure_ascii=False, default=str))
    return int(result.get("rc", 0))


if __name__ == "__main__":
    raise SystemExit(main())
